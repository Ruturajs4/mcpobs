"""Lite-mode intake: decode -> normalize -> insert, in-process, no Kafka.

Full-mode `ingest` forwards a stamped OTLP payload to the Collector, which
batches it onto Kafka; `normalizer/consumer.py` polls that topic and calls
three things per message: `OtlpDecoder.decode`, `SpanNormalizer.to_row`,
`ClickHouseSink.insert_spans` (normalizer/consumer.py:204,217,266-268). None of
that trio is Kafka-specific -- only the poll loop, offset tracking and DLQ
topic production around it are. This module makes the same three calls
directly from the HTTP handler, synchronously, with no broker in between.

WHAT THIS DELIBERATELY DOES NOT DO. There is no Kafka DLQ topic in lite mode,
so a dropped span's only record is the `ingest_dead_letter` ClickHouse table
and the container log -- logged at WARNING because a self-hoster is far more
likely to be watching `docker logs` than querying that table.

THE ACK BOUNDARY MOVES. In the full architecture, 200 returns once Kafka
durably has the batch, decoupled from ClickHouse. Here there is no queue: 200
returns only once ClickHouse's insert returns, coupling the customer's export
latency to ClickHouse write latency. That is a named trade for a self-host
deployment, not an oversight -- see docs-public/get-started/lite.md.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from normalizer.config import Settings

log = logging.getLogger("ingest.direct_intake")


class IntakeError(Exception):
    """The payload itself is bad -- the caller's fault, maps to 400.

    Kept distinct from any exception `insert_spans` raises (ClickHouse being
    unreachable, the caller's fault to nobody, maps to 503) so `app.py` can
    tell the two apart without inspecting exception internals.
    """


class DirectIntake:
    """The lite-mode substitute for Kafka + the normalizer container.

    EVERY `normalizer.*` IMPORT IN THIS FILE IS LOCAL TO A METHOD, not at
    module top. Measured the hard way: `ingest/app.py` imports this module
    UNCONDITIONALLY (so `IntakeError` is always a valid name to except on),
    but full-mode `ingest/Dockerfile` never copies `normalizer/` into the
    image -- it has no use for it, forwarding to the Collector instead. A
    module-level `from normalizer... import ...` here made the full-mode
    container crash-loop on every startup with `ModuleNotFoundError:
    No module named 'normalizer'`, and because span export failures are
    swallowed by the OTel SDK's BatchSpanProcessor, nothing about a demo run
    surfaced it -- the tool calls all "succeeded" while every span silently
    went nowhere. `DirectIntake()` is only ever constructed when
    `MCPOBS_LITE=1` (ingest/app.py), so importing lazily inside `__init__`
    keeps normalizer entirely out of the full-mode import graph.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        from normalizer.clickhouse_sink import ClickHouseSink
        from normalizer.normalize import SpanNormalizer
        from normalizer.otlp_decode import OtlpDecoder

        self.sink = ClickHouseSink(settings)
        self.decoder = OtlpDecoder()
        self.normalizer = SpanNormalizer()

    def start(self) -> None:
        """Wait for ClickHouse and apply schema migrations.

        Migrations only ever ran from the normalizer container's own startup
        (normalizer/consumer.py:151-152) -- there has never been a standalone
        `make migrate`. Lite mode has no normalizer container, so `ingest`'s
        lifespan owns this instead, mirroring how it already owns the
        Postgres control-plane schema via `control.migrate()`.
        """
        self.sink.wait_ready()
        applied = self.sink.migrate()
        if applied:
            log.info("applied ClickHouse migrations: %s", ", ".join(applied))

    def ingest(self, stamped: bytes) -> int:
        """Decode, normalize and insert one TRUSTED, already-stamped payload.

        `stamped` must be the output of `stamp_parsed()`, never the raw
        request body: decode has to run on the trusted tenant/project/region
        attributes the gateway wrote, not whatever the customer's process
        claimed (ingest/app.py's own security-boundary comment applies here
        unchanged).

        The dedup token is a hash of the exact bytes rather than a Kafka
        partition/offset range (SpanBatch.dedup_token, normalizer/consumer.py)
        -- neither exists here. An identical retry of the same HTTP POST, an
        OTel exporter's own retry behavior, hashes to the same token and gets
        the same insert_deduplication_token no-op property ADR-006 relies on.

        Raises `IntakeError` for a payload that will never decode (maps to
        400 in app.py). ClickHouse errors from `insert_spans` are not caught
        here -- they propagate so the caller can fail closed (503, matching
        the existing "collector unavailable" branch this replaces).
        """
        from normalizer.otlp_decode import DecodeError

        try:
            spans = self.decoder.decode(stamped)
        except DecodeError as exc:
            raise IntakeError(str(exc)) from exc

        rows = []
        for span in spans:
            try:
                rows.append(self.normalizer.to_row(span))
            except Exception as exc:  # noqa: BLE001 - one span must not fail the batch
                self._dead_letter("normalize_error", f"{type(exc).__name__}: {exc}", stamped)

        if not rows:
            return 0

        dedup_token = hashlib.sha256(stamped).hexdigest()
        return self.sink.insert_spans(rows, dedup_token=dedup_token)

    def _dead_letter(self, reason: str, detail: str, payload: bytes) -> None:
        from normalizer.models import DeadLetterRow

        log.warning("dead-lettering span: %s: %s", reason, detail)
        try:
            self.sink.dead_letter(
                DeadLetterRow(
                    reason=reason, detail=detail[:4000], raw_body=payload[:16000].hex()
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.error("dead-letter ClickHouse write failed: %s", exc)
