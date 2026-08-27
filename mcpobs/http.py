"""Downstream HTTP request bodies and headers (revises D60).

WHAT D60 SAID, AND WHAT IS ACTUALLY TRUE
    D60 recorded that HTTP bodies "are not captured" because the OTel HTTP
    instrumentation does not record them. That is true but was the wrong reason
    to stop looking: `HTTPXClientInstrumentor` accepts `request_hook` and
    `response_hook`, which is a real seam. Measuring it split the question in
    two, and the two halves have different answers.

    REQUEST body: capturable. The hook fires with the request already
    serialised into an in-memory `ByteStream`, so reading it costs a buffer copy
    and consumes nothing.

    RESPONSE body: NOT capturable on this span, and not because of a missing
    feature. The instrumentation wraps `handle_async_request`, so its span ENDS
    when the transport returns -- and httpx reads the body afterwards, in
    `Client.send`. By the time a response body exists, the span it belongs to
    has already been exported, and attributes set on an ended span are silently
    dropped.

    Buffering the response inside the hook would close that gap, and is exactly
    what this must not do: it would turn every streaming download into a full
    in-memory read, in the customer's process, to satisfy our telemetry. A
    monitoring tool that changes the memory profile of the thing it monitors has
    stopped being a monitoring tool.

    So: request bodies, request headers, response headers, alongside the
    response status the instrumentation already records. The response body
    stays a documented gap, and the console says so rather than rendering a
    blank box.

WHAT LEAVES THE CUSTOMER'S PROCESS
    Only what a hook is handed, after redaction and truncation by the same
    `PayloadCapture` that handles MCP payloads -- one implementation, so the two
    cannot drift into different redaction rules. Headers are ALLOW-listed:
    `authorization`, `cookie` and friends are never read at all.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Final

from mcpobs.payload import MAX_CHARS, PayloadCapture

log = logging.getLogger(__name__)

REQUEST_BODY_ATTRIBUTE: Final = "mcpobs.http.request.body"
REQUEST_HEADERS_ATTRIBUTE: Final = "mcpobs.http.request.headers"
RESPONSE_HEADERS_ATTRIBUTE: Final = "mcpobs.http.response.headers"

#: An ALLOW-list, not a deny-list. A deny-list is wrong by default: it captures
#: every header nobody thought of, and the one nobody thought of is where the
#: next bespoke `x-acme-session` credential will be.
KEEP_HEADERS: Final[frozenset[str]] = frozenset({
    "content-type", "content-length", "accept", "accept-encoding",
    "user-agent", "x-request-id", "traceparent", "retry-after",
    "cache-control", "location", "server",
})

STREAMING_REQUEST: Final = "[not read: streaming request]"


class HttpBodyCapture:
    """Hooks that record downstream HTTP detail onto the client span."""

    def __init__(
        self, payloads: PayloadCapture | None = None, max_chars: int = MAX_CHARS
    ) -> None:
        self.payloads = payloads or PayloadCapture(max_chars=max_chars)
        self.max_chars = max_chars

    # -- hooks -------------------------------------------------------------
    # Two shapes of each, because the instrumentation AWAITS the async hooks and
    # CALLS the sync ones. One function cannot be both, and passing a sync
    # function where an awaitable is expected raises inside the customer's HTTP
    # call -- the one place an observability library must never raise.
    def on_request(self, span: Any, request: Any) -> None:
        try:
            if span is None or not span.is_recording():
                return
            headers = self._headers(getattr(request, "headers", None))
            if headers:
                span.set_attribute(REQUEST_HEADERS_ATTRIBUTE, headers)
            body = self._request_body(request)
            if body:
                span.set_attribute(REQUEST_BODY_ATTRIBUTE, body)
        except Exception as exc:  # noqa: BLE001
            log.debug("http request capture skipped: %s", exc)

    def on_response(self, span: Any, request: Any, response: Any) -> None:
        # Headers only. See the module docstring: the body does not exist yet,
        # and this span ends before it does.
        try:
            if span is None or not span.is_recording():
                return
            headers = self._headers(getattr(response, "headers", None))
            if headers:
                span.set_attribute(RESPONSE_HEADERS_ATTRIBUTE, headers)
        except Exception as exc:  # noqa: BLE001
            log.debug("http response capture skipped: %s", exc)

    async def on_request_async(self, span: Any, request: Any) -> None:
        self.on_request(span, request)

    async def on_response_async(self, span: Any, request: Any, response: Any) -> None:
        self.on_response(span, request, response)

    # -- internals ---------------------------------------------------------
    def _headers(self, headers: Any) -> str:
        """Allow-listed headers as a redacted, truncated JSON object."""
        if headers is None:
            return ""
        try:
            items = list(headers.items())
        except AttributeError:
            return ""
        kept = {k: v for k, v in items if str(k).lower() in KEEP_HEADERS}
        if not kept:
            return ""
        text, _ = self.payloads.render(kept)
        return text

    def _request_body(self, request: Any) -> str:
        """The request body, when httpx has already buffered it.

        `RequestInfo.stream` is an httpx `ByteStream` for an ordinary request:
        the bytes sit in `_stream` and reading them consumes nothing. For a
        genuinely streaming upload it is an iterator instead, and this reports
        the gap rather than draining the customer's data on its way out.
        """
        stream = getattr(request, "stream", None)
        if stream is None:
            return ""
        raw = getattr(stream, "_stream", None)
        if raw is None:
            return ""
        if not isinstance(raw, (bytes, bytearray)):
            return STREAMING_REQUEST
        return self._decode(bytes(raw))

    def _decode(self, raw: bytes) -> str:
        if not raw:
            return ""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Binary payload: record its size, never its bytes.
            return f"[binary, {len(raw)} bytes]"
        rendered, _ = self.payloads.render(text)
        return rendered


def instrument_httpx(capture: HttpBodyCapture | None = None) -> bool:
    """Attach body capture to the OTel httpx instrumentation.

    Deliberately separate from `instrument(server)`: that instruments the
    customer's MCP server, this instruments their OUTBOUND HTTP client. They are
    different subjects, and conflating them would surprise someone who asked for
    only the first.

    Returns False when httpx instrumentation is not installed, rather than
    raising -- it is an optional dependency and its absence is not an error.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError:
        log.info("httpx instrumentation not installed; body capture unavailable")
        return False

    hooks = capture or HttpBodyCapture()
    instrumentor = HTTPXClientInstrumentor()
    # Hooks are bound at instrument() time, so an already-instrumented process
    # must be re-instrumented to pick them up. Uninstrumenting first is
    # required, not defensive: instrument() is a no-op while already active.
    with contextlib.suppress(Exception):
        instrumentor.uninstrument()
    instrumentor.instrument(
        request_hook=hooks.on_request,
        response_hook=hooks.on_response,
        async_request_hook=hooks.on_request_async,
        async_response_hook=hooks.on_response_async,
    )
    return True
