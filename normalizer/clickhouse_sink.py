"""Batched ClickHouse writer + dead-letter."""

from __future__ import annotations

import os
import time
from typing import Any

import clickhouse_connect

from normalizer.normalize import COLUMNS


class ClickHouseSink:
    def __init__(self) -> None:
        self.client = clickhouse_connect.get_client(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            database=os.getenv("CLICKHOUSE_DB", "mcpobs"),
            username=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        )

    def insert_spans(self, rows: list[dict[str, Any]], dedup_token: str | None = None) -> int:
        if not rows:
            return 0
        data = [[row[c] for c in COLUMNS] for row in rows]
        settings = {}
        if dedup_token:
            # ADR-006. NOTE: a no-op on this MergeTree table -- deduplication
            # requires ReplicatedMergeTree. Sent anyway so the production path
            # is exercised and the token format is validated.
            settings["insert_deduplication_token"] = dedup_token
        self.client.insert(
            "spans_raw", data, column_names=COLUMNS, settings=settings or None
        )
        return len(rows)

    def dead_letter(
        self, reason: str, detail: str, partition: int, offset: int, raw: bytes
    ) -> None:
        self.client.insert(
            "ingest_dead_letter",
            [[reason, detail[:4000], partition, offset, raw[:16000].hex()]],
            column_names=["reason", "detail", "kafka_partition", "kafka_offset", "raw_body"],
        )

    def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                self.client.query("SELECT 1")
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2)
        raise RuntimeError(f"ClickHouse not ready: {last}")
