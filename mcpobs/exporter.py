"""An OTLP exporter that carries a rotating session token.

`OTLPSpanExporter` takes its
headers at CONSTRUCTION, so a credential that changes every three hours cannot
be passed the ordinary way -- this was flagged in the ADR as the part most
likely to be under-estimated, because from the outside it looks like a config
change and is not.

The header is set per export by mutating the exporter's `requests.Session`,
which is the supported extension point: the SDK builds one and reuses it, so
updating it in place is what "rotate the credential" actually means here.

WHAT HAPPENS BEFORE THE FIRST TOKEN ARRIVES. A server starts and immediately
emits spans -- `initialize`, `tools/list` -- while the session endpoint is still
being called. Three options, and only one is honest:

  * Drop them. Every server would silently lose its startup spans, which are
    exactly the ones that show a client connecting.
  * Block the exporter thread until a token arrives. Turns a slow endpoint into
    a stalled export pipeline, and on stdio into a process that will not exit.
  * BUFFER, with a hard bound. Chosen. An unbounded buffer waiting on an
    endpoint that never answers is a memory leak inside the customer's process,
    so the bound is not optional -- and when it is hit, the loss is reported
    rather than silent.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Sequence
from typing import Any

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult

from mcpobs.session import SessionProvider
from mcpobs.session import default as session_default

log = logging.getLogger("mcpobs.exporter")

#: Spans held while waiting for the first token. Roughly a few seconds of a busy
#: server, which is the window a session fetch takes. Past this the oldest are
#: dropped: a process that has been unable to reach its session endpoint for
#: minutes has a configuration problem, and growing without limit turns that
#: into a second one.
MAX_PENDING_SPANS = 2_000


class SessionSpanExporter(OTLPSpanExporter):
    """OTLP over HTTP, authenticated with a short-lived session token."""

    def __init__(
        self,
        provider: SessionProvider | None = None,
        endpoint: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(endpoint=endpoint, **kwargs)
        # Defaults to whatever `instrument()` configured, so the exporter and
        # the server do not have to be wired to each other by the customer.
        self._provider = provider if provider is not None else session_default()
        self._pending: list[ReadableSpan] = []
        self._pending_lock = threading.Lock()
        self._dropped = 0
        self._warned_no_token = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        session = self._provider.current()

        if session is None:
            self._hold(spans)
            # SUCCESS, because we took responsibility for these spans rather
            # than failing to send them. Reporting FAILURE would make the
            # BatchSpanProcessor log an error per batch for a condition that is
            # expected during startup, and it drops the batch either way.
            return SpanExportResult.SUCCESS

        if self._warned_no_token:
            _diag("session token acquired; buffered spans flushing")
            self._warned_no_token = False

        self._session.headers["x-api-key"] = session.token

        # The mint response names where to export. Honoured so a customer's
        # config carries ONE url -- theirs -- and we can move or region-route
        # ingest without them redeploying.
        if session.endpoint:
            wanted = session.endpoint.rstrip("/") + "/v1/traces"
            if wanted != self._endpoint:
                log.debug("session endpoint -> %s", wanted)
                self._endpoint = wanted

        batch = self._drain(spans)
        result = super().export(batch)

        if result is not SpanExportResult.SUCCESS:
            # A rejected export may mean the token died early -- revoked, or the
            # signing key rotated. Dropping it forces one fresh fetch rather
            # than retrying with a credential the server has already refused.
            self._provider.invalidate()
        return result

    # -- buffering ---------------------------------------------------------
    def _hold(self, spans: Sequence[ReadableSpan]) -> None:
        with self._pending_lock:
            self._pending.extend(spans)
            overflow = len(self._pending) - MAX_PENDING_SPANS
            if overflow > 0:
                # OLDEST dropped, not newest. If a server has been waiting this
                # long, recent spans describe what it is doing now; the startup
                # ones it began with are the least useful thing to keep.
                del self._pending[:overflow]
                self._dropped += overflow
        if not self._warned_no_token:
            self._warned_no_token = True
            _diag("no session token yet; buffering spans")

    def _drain(self, spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
        with self._pending_lock:
            if not self._pending:
                return list(spans)
            held = self._pending
            self._pending = []
            dropped, self._dropped = self._dropped, 0
        if dropped:
            # Said out loud. Silent telemetry loss is the failure this whole
            # product exists to make visible in customers' systems, so hiding
            # our own would be indefensible.
            _diag(
                f"dropped {dropped} span(s) while waiting for a session token "
                f"(buffer limit {MAX_PENDING_SPANS})"
            )
        return held + list(spans)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush anything held, then the parent's own buffers.

        Called on shutdown, which on stdio is when the CLIENT tears the process
        down. Without this, spans held waiting for a token would be lost exactly
        at the moment a session ends -- and stdio sessions end constantly.
        """
        with self._pending_lock:
            held = self._pending
            self._pending = []
        if held:
            session = self._provider.current()
            if session is not None:
                self._session.headers["x-api-key"] = session.token
                try:
                    super().export(held)
                except Exception as exc:  # noqa: BLE001
                    log.debug("flush of held spans failed: %s", exc)
        return super().force_flush(timeout_millis)


def _diag(message: str) -> None:
    """STDERR. On the stdio transport stdout is the JSON-RPC channel."""
    print(f"[mcpobs] {message}", file=sys.stderr)
