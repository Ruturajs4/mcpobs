"""The normalizer: a stateless Kafka consumer that writes spans to ClickHouse.

The ordering below is the whole point of putting Kafka in on Day 1:

    poll -> decode -> normalize -> INSERT -> *then* commit offsets

Offsets are never committed before the insert returns and never on a timer
(`enable.auto.commit=false`). A crash between insert and commit replays the
batch; the deduplication token makes the replay a no-op in production
(ADR-006 -- see the caveat in clickhouse_sink.insert_spans).

Getting this backwards is the most dangerous bug available today, because it
looks fine right up until a crash silently loses spans.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

from normalizer.clickhouse_sink import ClickHouseSink
from normalizer.normalize import to_row
from normalizer.otlp_decode import DecodeError, flatten

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
TOPIC = os.getenv("KAFKA_TOPIC", "otlp.spans.raw")
DLQ_TOPIC = os.getenv("KAFKA_DLQ_TOPIC", "otlp.spans.dlq")
GROUP_ID = os.getenv("KAFKA_GROUP_ID", "normalizer")
BATCH_MAX_ROWS = int(os.getenv("BATCH_MAX_ROWS", "10000"))
BATCH_MAX_SECONDS = float(os.getenv("BATCH_MAX_SECONDS", "5"))

_running = True


def _stop(*_: Any) -> None:
    global _running
    _running = False
    print("[normalizer] shutdown requested", flush=True)


class Normalizer:
    def __init__(self) -> None:
        self.sink = ClickHouseSink()
        self.consumer = Consumer(
            {
                "bootstrap.servers": BOOTSTRAP,
                "group.id": GROUP_ID,
                "auto.offset.reset": "earliest",
                # Offsets commit after the insert, never on a timer.
                "enable.auto.commit": False,
                "isolation.level": "read_committed",
                "max.poll.interval.ms": 300000,
            }
        )
        self.dlq = Producer({"bootstrap.servers": BOOTSTRAP, "compression.type": "zstd"})
        self.rows: list[dict] = []
        self.span_count = 0
        self.batch_bounds: dict[int, list[int]] = {}  # partition -> [min, max] offset
        self.last_flush = time.time()

    # -- batching ---------------------------------------------------------
    def _track(self, partition: int, offset: int) -> None:
        bounds = self.batch_bounds.get(partition)
        if bounds is None:
            self.batch_bounds[partition] = [offset, offset]
        else:
            bounds[0] = min(bounds[0], offset)
            bounds[1] = max(bounds[1], offset)

    def _dedup_token(self) -> str:
        parts = ",".join(
            f"{p}:{lo}-{hi}" for p, (lo, hi) in sorted(self.batch_bounds.items())
        )
        return f"{TOPIC}|{parts}"

    def _should_flush(self) -> bool:
        return bool(self.rows) and (
            len(self.rows) >= BATCH_MAX_ROWS
            or (time.time() - self.last_flush) >= BATCH_MAX_SECONDS
        )

    def flush(self) -> None:
        if not self.rows:
            self.last_flush = time.time()
            return
        token = self._dedup_token()
        written = self.sink.insert_spans(self.rows, dedup_token=token)
        # ---- ONLY NOW is it safe to commit ----
        self.consumer.commit(asynchronous=False)
        print(f"[normalizer] inserted {written} spans, committed offsets", flush=True)
        self.rows.clear()
        self.batch_bounds.clear()
        self.last_flush = time.time()

    # -- message handling --------------------------------------------------
    def handle(self, message: Any) -> None:
        partition, offset = message.partition(), message.offset()
        payload = message.value() or b""
        try:
            spans = flatten(payload)
        except DecodeError as exc:
            self._to_dlq("decode_error", str(exc), partition, offset, payload)
            return
        except Exception as exc:  # noqa: BLE001 - never let one message stall a partition
            self._to_dlq("unexpected_error", f"{type(exc).__name__}: {exc}", partition, offset, payload)
            return

        for span in spans:
            try:
                self.rows.append(to_row(span, partition=partition, offset=offset))
                self.span_count += 1
            except Exception as exc:  # noqa: BLE001
                self._to_dlq("normalize_error", f"{type(exc).__name__}: {exc}", partition, offset, payload)
        self._track(partition, offset)

    def _to_dlq(self, reason: str, detail: str, partition: int, offset: int, payload: bytes) -> None:
        """Poison message -> DLQ, then the offset advances. Never skip silently."""
        print(f"[normalizer] DLQ {reason} p{partition}@{offset}: {detail}", flush=True)
        try:
            self.dlq.produce(DLQ_TOPIC, value=payload, headers={"reason": reason})
            self.dlq.poll(0)
        except Exception as exc:  # noqa: BLE001
            print(f"[normalizer] DLQ produce failed: {exc}", flush=True)
        try:
            self.sink.dead_letter(reason, detail, partition, offset, payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[normalizer] DLQ clickhouse write failed: {exc}", flush=True)
        self._track(partition, offset)

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.sink.wait_ready()
        self.consumer.subscribe([TOPIC])
        print(
            f"[normalizer] consuming {TOPIC} group={GROUP_ID} "
            f"bootstrap={BOOTSTRAP} (auto-commit OFF)",
            flush=True,
        )
        try:
            while _running:
                message = self.consumer.poll(1.0)
                if message is None:
                    if self._should_flush():
                        self.flush()
                    continue
                if message.error():
                    if message.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(message.error())
                self.handle(message)
                if self._should_flush():
                    self.flush()
        finally:
            try:
                self.flush()
            finally:
                self.dlq.flush(5)
                self.consumer.close()
                print(f"[normalizer] stopped after {self.span_count} spans", flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    Normalizer().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
