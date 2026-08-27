"""The normalizer: a stateless Kafka consumer that writes spans to ClickHouse.

The ordering below is the whole point of putting Kafka in on Day 1:

    poll -> decode -> normalize -> INSERT -> *then* commit offsets

Offsets are never committed before the insert returns and never on a timer
(`enable.auto.commit=false`). A crash between insert and commit replays the
batch; the deduplication token makes the replay a no-op in production
(ADR-006 -- see the caveat in ClickHouseSink.insert_spans).
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime
from types import FrameType

from confluent_kafka import Consumer, KafkaError, KafkaException, Message, Producer

from normalizer.clickhouse_sink import ClickHouseSink
from normalizer.config import Settings
from normalizer.config import settings as default_settings
from normalizer.models import DeadLetterRow, SpanRow
from normalizer.normalize import SpanNormalizer
from normalizer.otlp_decode import DecodeError, OtlpDecoder
from normalizer.telemetry import PipelineMetrics

log = logging.getLogger("normalizer")


class SpanBatch:
    """Rows pending insert, plus the offsets they came from.

    Offsets are tracked separately from rows on purpose. A batch can hold
    offsets and NO rows -- when every message in it was dead-lettered -- and
    those offsets still have to be committed, or the partition replays the
    poison message forever. Keeping the two concerns in one object makes that
    case structurally visible instead of an easy-to-miss early return.
    """

    def __init__(self, max_rows: int, max_seconds: float, topic: str) -> None:
        self.max_rows = max_rows
        self.max_seconds = max_seconds
        self.topic = topic
        self.rows: list[SpanRow] = []
        self.offsets: dict[int, list[int]] = {}  # partition -> [min, max]
        self.opened_at = time.monotonic()

    def add_row(self, row: SpanRow) -> None:
        self.rows.append(row)

    def track(self, partition: int, offset: int) -> None:
        bounds = self.offsets.get(partition)
        if bounds is None:
            self.offsets[partition] = [offset, offset]
        else:
            bounds[0] = min(bounds[0], offset)
            bounds[1] = max(bounds[1], offset)

    @property
    def has_pending_offsets(self) -> bool:
        return bool(self.offsets)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.opened_at

    def should_flush(self) -> bool:
        if self.rows and len(self.rows) >= self.max_rows:
            return True
        return self.has_pending_offsets and self.elapsed >= self.max_seconds

    def dedup_token(self) -> str:
        """Deterministic across replays, so a redelivered batch is byte-identical."""
        parts = ",".join(f"{p}:{lo}-{hi}" for p, (lo, hi) in sorted(self.offsets.items()))
        return f"{self.topic}|{parts}"

    def reset(self) -> None:
        self.rows = []
        self.offsets = {}
        self.opened_at = time.monotonic()


class Normalizer:
    """Consume -> decode -> normalize -> insert -> commit."""

    def __init__(
        self,
        settings: Settings | None = None,
        sink: ClickHouseSink | None = None,
        decoder: OtlpDecoder | None = None,
        normalizer: SpanNormalizer | None = None,
        stop_when_idle: float | None = None,
    ) -> None:
        self.settings = settings or default_settings
        self.sink = sink or ClickHouseSink(self.settings)
        self.decoder = decoder or OtlpDecoder()
        self.normalizer = normalizer or SpanNormalizer()
        self.batch = SpanBatch(
            self.settings.batch_max_rows,
            self.settings.batch_max_seconds,
            self.settings.kafka_topic,
        )
        # Replay tooling sets this: exit once the topic is drained instead of
        # running forever (Architecture.md §5.4).
        self.stop_when_idle = stop_when_idle
        self.metrics = PipelineMetrics(
            endpoint=self.settings.otel_metrics_endpoint,
            enabled=self.settings.self_telemetry,
        )
        self.span_count = 0
        self._running = True
        self._consumer: Consumer | None = None
        self._dlq: Producer | None = None

    # -- kafka clients -----------------------------------------------------
    @property
    def consumer(self) -> Consumer:
        if self._consumer is None:
            self._consumer = Consumer(
                {
                    "bootstrap.servers": self.settings.kafka_bootstrap,
                    "group.id": self.settings.kafka_group_id,
                    "auto.offset.reset": "earliest",
                    # Offsets commit after the insert, never on a timer.
                    "enable.auto.commit": False,
                    "isolation.level": "read_committed",
                    "max.poll.interval.ms": 300_000,
                }
            )
        return self._consumer

    @property
    def dlq(self) -> Producer:
        if self._dlq is None:
            self._dlq = Producer(
                {"bootstrap.servers": self.settings.kafka_bootstrap, "compression.type": "zstd"}
            )
        return self._dlq

    # -- lifecycle ---------------------------------------------------------
    def stop(self, signum: int | None = None, frame: FrameType | None = None) -> None:
        self._running = False
        log.info("shutdown requested")

    def run(self) -> None:
        self.sink.wait_ready()
        self.sink.migrate()
        self.consumer.subscribe([self.settings.kafka_topic])
        log.info(
            "consuming %s group=%s bootstrap=%s (auto-commit OFF)",
            self.settings.kafka_topic,
            self.settings.kafka_group_id,
            self.settings.kafka_bootstrap,
        )
        try:
            idle_since: float | None = None
            while self._running:
                message = self.consumer.poll(1.0)
                if message is None and self.stop_when_idle is not None:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= self.stop_when_idle:
                        log.info("topic drained; stopping (idle %.0fs)", self.stop_when_idle)
                        self._running = False
                elif message is not None:
                    idle_since = None
                if message is not None:
                    error = message.error()
                    if error is not None:
                        if error.code() == KafkaError._PARTITION_EOF:
                            continue
                        raise KafkaException(error)
                    self.handle(message)
                if self.batch.should_flush():
                    self.flush()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        try:
            self.flush()
        finally:
            if self._dlq is not None:
                self._dlq.flush(5)
            if self._consumer is not None:
                self._consumer.close()
            log.info("stopped after %d spans", self.span_count)

    # -- message handling --------------------------------------------------
    def handle(self, message: Message) -> None:
        partition, offset = message.partition(), message.offset()
        # confluent-kafka types these as Optional. A message without a partition
        # or offset cannot be committed or included in a deduplication token, so
        # accepting one would silently corrupt both. Refuse it loudly instead.
        if partition is None or offset is None:
            log.error("dropping message with no partition/offset: %r", message)
            return
        payload = message.value() or b""
        try:
            spans = self.decoder.decode(payload)
        except DecodeError as exc:
            self.dead_letter("decode_error", str(exc), partition, offset, payload)
            return
        except Exception as exc:  # noqa: BLE001 - one message must never stall a partition
            self.dead_letter(
                "unexpected_error", f"{type(exc).__name__}: {exc}", partition, offset, payload
            )
            return

        for span in spans:
            try:
                self.batch.add_row(
                    self.normalizer.to_row(span, partition=partition, offset=offset)
                )
                self.span_count += 1
                self.metrics.spans_normalized.add(1)
            except Exception as exc:  # noqa: BLE001
                self.dead_letter(
                    "normalize_error", f"{type(exc).__name__}: {exc}", partition, offset, payload
                )
        self.batch.track(partition, offset)

    def dead_letter(
        self, reason: str, detail: str, partition: int, offset: int, payload: bytes
    ) -> None:
        """Poison message -> DLQ, then the offset advances. Never skip silently."""
        log.warning("DLQ %s p%d@%d: %s", reason, partition, offset, detail)
        self.metrics.dead_lettered.add(1, {"reason": reason})
        try:
            # Headers carry the reason so the DLQ topic is triageable on its
            # own. Without them the reason exists only in ClickHouse, and DLQ
            # triage is exactly the case where ClickHouse may be the thing
            # that is broken.
            self.dlq.produce(
                self.settings.kafka_dlq_topic,
                value=payload,
                headers={"reason": reason, "detail": detail[:200]},
            )
            self.dlq.poll(0)
        except Exception as exc:  # noqa: BLE001
            log.error("DLQ produce failed: %s", exc)
        try:
            self.sink.dead_letter(
                DeadLetterRow(
                    reason=reason,
                    detail=detail[:4000],
                    kafka_partition=partition,
                    kafka_offset=offset,
                    raw_body=payload[:16000].hex(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.error("DLQ ClickHouse write failed: %s", exc)
        self.batch.track(partition, offset)

    def flush(self) -> None:
        if not self.batch.has_pending_offsets:
            return

        started = time.monotonic()
        try:
            written = self.sink.insert_spans(
                self.batch.rows, dedup_token=self.batch.dedup_token()
            )
        except Exception:
            # Counted before re-raising: an insert failure that is invisible is
            # the one that turns into silent data loss.
            self.metrics.insert_failures.add(1)
            raise
        elapsed_ms = (time.monotonic() - started) * 1000
        self.metrics.insert_duration.record(elapsed_ms)
        self.metrics.rows_inserted.add(written)
        self._record_freshness()

        # ---- ONLY NOW is it safe to commit ----
        self.consumer.commit(asynchronous=False)
        self.metrics.batches_committed.add(1)
        if written:
            log.info("inserted %d spans, committed offsets", written)
        else:
            log.info(
                "committed %d partition(s) with no insertable rows (all dead-lettered)",
                len(self.batch.offsets),
            )
        self.batch.reset()

    def _record_freshness(self) -> None:
        """Event time -> write time, per batch.

        THE headline pipeline metric (Architecture.md §9.1). Measured from the
        spans actually written rather than a synthetic probe, so it reflects the
        real path -- including the batch interval, which is its floor.
        """
        if not self.batch.rows:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        for row in self.batch.rows:
            self.metrics.freshness.record(
                max(0.0, (now - row.timestamp).total_seconds() * 1000)
            )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="[%(name)s] %(message)s", stream=sys.stdout
    )
    normalizer = Normalizer()
    signal.signal(signal.SIGTERM, normalizer.stop)
    signal.signal(signal.SIGINT, normalizer.stop)
    normalizer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
