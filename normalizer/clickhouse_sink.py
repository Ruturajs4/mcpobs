"""Batched ClickHouse writer + dead-letter."""

from __future__ import annotations

import logging
import time

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from normalizer.config import Settings
from normalizer.config import settings as default_settings
from normalizer.migrations import MigrationRunner
from normalizer.models import DeadLetterRow, SpanRow

log = logging.getLogger(__name__)


class ClickHouseSink:
    SPANS_TABLE = "spans_raw"
    DLQ_TABLE = "ingest_dead_letter"

    def __init__(self, settings: Settings | None = None, client: Client | None = None) -> None:
        self.settings = settings or default_settings
        self._client = client

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = clickhouse_connect.get_client(
                host=self.settings.clickhouse_host,
                port=self.settings.clickhouse_port,
                database=self.settings.clickhouse_db,
                username=self.settings.clickhouse_user,
                password=self.settings.clickhouse_password,
                secure=self.settings.clickhouse_secure,
            )
        return self._client

    def insert_spans(self, rows: list[SpanRow], dedup_token: str | None = None) -> int:
        if not rows:
            return 0
        settings: dict[str, str | int] = {}
        if dedup_token:
            # ADR-006. Verified working on ReplicatedMergeTree (assertion B11);
            # the local stack runs embedded Keeper so this is a real mechanism
            # here, not a production-only one.
            settings["insert_deduplication_token"] = dedup_token
            # WITHOUT THIS, DEDUPLICATION IS HALF-APPLIED. ClickHouse dedupes
            # the block written to spans_raw but, by default, still propagates
            # it to every dependent materialized view -- so a replayed batch
            # left spans_raw correct while trace_summaries counted the spans
            # TWICE. Aggregates silently disagreeing with the table they are
            # built from is the worst possible failure mode: nothing errors and
            # every dashboard is wrong. Caught by assertion B4 (D39).
            settings["deduplicate_blocks_in_dependent_materialized_views"] = 1
        self.client.insert(
            self.SPANS_TABLE,
            [row.values() for row in rows],
            column_names=SpanRow.columns(),
            settings=settings or None,
        )
        return len(rows)

    def dead_letter(self, row: DeadLetterRow) -> None:
        self.client.insert(
            self.DLQ_TABLE,
            [row.values()],
            column_names=DeadLetterRow.columns(),
        )

    def migrate(self) -> list[str]:
        applied = MigrationRunner(self.client, self.settings.clickhouse_db).run()
        if applied:
            log.info("applied migrations: %s", ", ".join(applied))
        else:
            log.info("schema up to date")
        return applied

    def wait_ready(self, timeout: float = 240.0) -> None:
        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                self.client.query("SELECT 1")
                return
            except Exception as exc:  # noqa: BLE001 - retried until the deadline
                last = exc
                self._client = None  # force a fresh connection attempt
                time.sleep(2)
        raise RuntimeError(f"ClickHouse not ready after {timeout}s: {last}")
