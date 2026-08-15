"""The raw archive: `otlp.spans.raw` -> object storage.

WHAT IT IS FOR
    ADR-007 makes Kafka retention a product capability rather than an
    operational parameter: the raw topic IS the replay window. 72 hours is not
    long enough to re-derive a season of telemetry after a normalizer bug, so
    the archive extends that window without buying broker disk.

WHY IT IS A CONSUMER GROUP AND NOT A COLLECTOR EXPORTER
    The Collector ships an `awss3` exporter, and using it would have been less
    code. It would also archive BEFORE Kafka, which breaks the property the
    whole design rests on: the archive must contain exactly what the pipeline
    consumed, byte for byte, or replaying from it does not reproduce anything.
    Reading from the topic -- as its own consumer group, with its own lag and
    its own replay position (Architecture.md §4) -- is what makes the archive a
    faithful copy rather than a parallel opinion.

WHY RAW PROTOBUF AND NOT PARQUET
    Architecture.md §4 says "raw archive to parquet". This writes the OTLP
    protobuf bytes instead, and the reason is ADR-007's own conclusion:
    "One replay mechanism is worth paying for." Replay means feeding these bytes
    back through `normalizer/consumer.py`. Parquet would need a second, different
    replay path that reads columns instead of messages -- exactly the split
    ADR-007 rejected when it declined S3-as-only-replay-source.

    The cost is real and worth stating: this archive is not directly queryable
    by Athena or DuckDB. That is the right trade here because queryable
    long-term telemetry is ClickHouse's job, and a parquet projection can always
    be derived FROM the archive later. The reverse -- recovering exact replay
    fidelity from a lossy columnar copy -- cannot.

OFFSET DISCIPLINE, IDENTICAL TO THE NORMALIZER
    poll -> accumulate -> PUT -> *then* commit. A crash between the put and the
    commit re-archives an object under the same deterministic key, which
    overwrites rather than duplicates. That is why the key is derived from the
    offset range instead of a timestamp or a uuid.
"""

from __future__ import annotations

import io
import logging
import os
import signal
import sys
import time
import zlib
from datetime import UTC, datetime
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException

log = logging.getLogger("archiver")


class ArchiveBatch:
    """Messages pending a put, grouped by the object each will land in.

    Keyed by (tenant, partition) rather than kept as one flat list. An object
    belongs to exactly one tenant and one partition, and deriving that grouping
    at flush time from a flat list is what produced the first version's bug:
    it looped partitions x tenants and wrote every tenant's messages once per
    partition, silently duplicating the archive.
    """

    def __init__(self, max_messages: int, max_seconds: float, max_bytes: int) -> None:
        self.max_messages = max_messages
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        #: (tenant, partition) -> (payloads, first_offset, last_offset)
        self.groups: dict[tuple[str, int], tuple[list[bytes], int, int]] = {}
        self.count = 0
        self.size = 0
        self.opened_at = time.monotonic()

    def add(self, tenant: str, payload: bytes, partition: int, offset: int) -> None:
        key = (tenant, partition)
        group = self.groups.get(key)
        if group is None:
            self.groups[key] = ([payload], offset, offset)
        else:
            payloads, first, last = group
            payloads.append(payload)
            self.groups[key] = (payloads, min(first, offset), max(last, offset))
        self.count += 1
        self.size += len(payload)

    def full(self) -> bool:
        return (
            self.count >= self.max_messages
            or self.size >= self.max_bytes
            or (self.count > 0 and time.monotonic() - self.opened_at >= self.max_seconds)
        )

    def reset(self) -> None:
        self.groups.clear()
        self.count = 0
        self.size = 0
        self.opened_at = time.monotonic()


class ObjectStore:
    """S3, or anything speaking its API.

    MinIO locally, exactly as ClickHouse runs with embedded Keeper: the point of
    the local rung is that the ENGINE is the real one (D40). A filesystem stub
    would not exercise credentials, bucket policy, key layout or multipart
    behaviour, so every bug in those would wait for staging to find.
    """

    def __init__(self) -> None:
        import boto3

        self.bucket = os.getenv("ARCHIVE_BUCKET", "mcpobs-archive")
        self.client = boto3.client(
            "s3",
            endpoint_url=os.getenv("S3_ENDPOINT", "http://minio:9000"),
            aws_access_key_id=os.getenv("S3_ACCESS_KEY", "mcpobs"),
            aws_secret_access_key=os.getenv("S3_SECRET_KEY", "mcpobs-secret"),
            region_name=os.getenv("S3_REGION", "us-east-1"),
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:  # noqa: BLE001
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, body: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)


def archive_key(tenant: str, when: datetime, partition: int, first: int, last: int) -> str:
    """`tenant/date/hour/partition-first-last.otlp.zz`.

    TENANT FIRST, and that ordering is load-bearing rather than tidy. It makes
    "delete everything belonging to this customer" a prefix delete, and a
    per-tenant lifecycle rule expressible as a prefix rule. Putting the date
    first would have made both of those a full scan, and GDPR erasure is not a
    thing to discover you cannot do cheaply.

    OFFSETS IN THE NAME, not a timestamp or a uuid, so re-archiving the same
    messages after a crash between put and commit overwrites the identical
    object instead of creating a second copy of it.
    """
    return (
        f"{tenant}/{when:%Y-%m-%d}/{when:%H}/"
        f"p{partition:03d}-{first:012d}-{last:012d}.otlp.zz"
    )


def frame(payloads: list[bytes]) -> bytes:
    """Length-prefixed concatenation of the raw payloads.

    Protobuf is not self-delimiting: concatenating two `ExportTraceServiceRequest`
    messages parses as one merged message, silently, with repeated fields joined.
    That would look fine and be wrong -- the archive would no longer contain the
    individual Kafka messages it claims to. A 4-byte big-endian length before
    each payload keeps the boundaries the topic had.
    """
    buffer = io.BytesIO()
    for payload in payloads:
        buffer.write(len(payload).to_bytes(4, "big"))
        buffer.write(payload)
    return zlib.compress(buffer.getvalue(), level=6)


def unframe(blob: bytes) -> list[bytes]:
    """Inverse of `frame`, so a replay can read what was archived.

    Shipped alongside the writer deliberately. An archive format with no reader
    is a promise nobody has tested, and this one is exercised by assertion F5.
    """
    raw = zlib.decompress(blob)
    out, cursor = [], 0
    while cursor < len(raw):
        size = int.from_bytes(raw[cursor:cursor + 4], "big")
        cursor += 4
        out.append(raw[cursor:cursor + size])
        cursor += size
    return out


class Archiver:
    def __init__(self, store: ObjectStore | None = None) -> None:
        self.topic = os.getenv("KAFKA_TOPIC", "otlp.spans.raw")
        self.store = store or ObjectStore()
        self.batch = ArchiveBatch(
            max_messages=int(os.getenv("ARCHIVE_MAX_MESSAGES", "500")),
            max_seconds=float(os.getenv("ARCHIVE_MAX_SECONDS", "10")),
            max_bytes=int(os.getenv("ARCHIVE_MAX_BYTES", str(8 * 1024 * 1024))),
        )
        self.running = True
        self.objects_written = 0
        self.consumer = Consumer({
            "bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP", "kafka:9092"),
            # Its OWN group. Independent lag, independent scaling, independent
            # replay position from the normalizer (Architecture.md §4) -- an
            # archiver that fell behind must never slow down normalization.
            "group.id": os.getenv("KAFKA_GROUP_ID", "archiver"),
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        })

    def stop(self, *_: Any) -> None:
        self.running = False

    @staticmethod
    def split_by_tenant(payload: bytes) -> list[tuple[str, bytes]]:
        """One message in, one payload per tenant out.

        TWO MEASURED FACTS FORCED THIS, and the first version had neither right.

        1. The Kafka message key is NULL. ADR-004 says the topic is keyed by
           `tenant_id`, and the topic table in Architecture.md §6 says the same.
           It has never been true: the Collector's kafkaexporter in 0.115 has no
           option to key traces by a resource attribute, so every message has
           been produced with a null key and round-robined across partitions.
           Reading the tenant off the key produced an archive filed entirely
           under `unknown/`. Filed as DF-19; nothing else here can fix it.

        2. One message can carry SEVERAL tenants. The Collector's batch
           processor merges spans from concurrent requests into one
           ExportTraceServiceRequest with many ResourceSpans, and those requests
           came from different customers. So "the tenant of a message" is not a
           well-formed question -- only "the tenant of a ResourceSpans" is.

        This reads resource attributes and nothing else. The archiver stays
        deliberately MCP-unaware (Architecture.md §4) -- it never looks at a
        span, a method name or a payload -- but it cannot be TENANT-unaware,
        because keeping one customer's bytes out of another's object is the
        whole reason the archive has a key layout.
        """
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        request = ExportTraceServiceRequest()
        request.ParseFromString(payload)

        by_tenant: dict[str, ExportTraceServiceRequest] = {}
        for resource_spans in request.resource_spans:
            tenant = "unknown"
            for attribute in resource_spans.resource.attributes:
                if attribute.key == "tenant.id" and attribute.value.string_value:
                    tenant = attribute.value.string_value
                    break
            grouped = by_tenant.setdefault(tenant, ExportTraceServiceRequest())
            grouped.resource_spans.append(resource_spans)

        return [(tenant, req.SerializeToString()) for tenant, req in by_tenant.items()]

    def flush(self) -> None:
        if not self.groups_pending():
            return

        now = datetime.now(UTC)
        # One object per (tenant, partition). Never one object spanning two
        # tenants: a shared object would make per-tenant deletion a rewrite
        # rather than a delete, and would hand anyone with read access to one
        # tenant's prefix the contents of another's.
        for (tenant, partition), (payloads, first, last) in self.batch.groups.items():
            self.store.put(
                archive_key(tenant, now, partition, first, last), frame(payloads)
            )
            self.objects_written += 1

        # Commit only after every put returned. A crash in between re-archives
        # to the same deterministic key, which overwrites rather than
        # duplicates -- which is why the key carries offsets, not a timestamp.
        self.consumer.commit(asynchronous=False)
        log.info(
            "archived %d messages (%d bytes) into %d object(s)",
            self.batch.count, self.batch.size, len(self.batch.groups),
        )
        self.batch.reset()

    def groups_pending(self) -> bool:
        """Whether there is anything to write.

        A separate method because the empty case has a history: the normalizer
        stalled a partition when a batch held offsets but no rows and returned
        early without committing. There is no equivalent here -- a skipped
        message contributes no offset to commit either -- but the reasoning is
        worth leaving where the next person will look for it.
        """
        if self.batch.groups:
            return True
        self.batch.reset()
        return False

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.store.ensure_bucket()
        self.consumer.subscribe([self.topic])
        log.info("archiving %s -> s3://%s (group=%s)",
                 self.topic, self.store.bucket, os.getenv("KAFKA_GROUP_ID", "archiver"))
        try:
            while self.running:
                message = self.consumer.poll(1.0)
                if message is None:
                    if self.batch.full():
                        self.flush()
                    continue
                error = message.error()
                if error is not None:
                    if error.code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(error)
                payload = message.value()
                if payload:
                    try:
                        parts = self.split_by_tenant(payload)
                    except Exception as exc:  # noqa: BLE001
                        # An undecodable message still has to be archived, or the
                        # archive is not a faithful copy of the topic. Filed
                        # under `unknown/` verbatim rather than dropped: losing
                        # bytes is worse than filing them awkwardly.
                        log.warning("archiving undecodable message verbatim: %s", exc)
                        parts = [("unknown", payload)]
                    partition, offset = message.partition(), message.offset()
                    for tenant, part in parts:
                        self.batch.add(tenant, part, int(partition or 0), int(offset or 0))
                if self.batch.full():
                    self.flush()
        finally:
            self.flush()
            self.consumer.close()
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    return Archiver().run()


if __name__ == "__main__":
    sys.exit(main())
