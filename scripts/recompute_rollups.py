"""Rebuild every derived aggregate from `spans_raw`.

WHY THIS EXISTS
    A materialized view sees an INSERT BATCH, not a version history, so it
    cannot honour the rule every other read obeys: resolve each span to its
    latest `normalization_version` (D24). In normal operation that costs
    nothing, because each span is inserted once. After a REPLAY it is wrong in
    the worst possible way -- the counters ADD the replayed spans instead of
    superseding the originals, nothing errors, and every dashboard is quietly
    inflated.

    So each aggregate has two maintainers: the MV keeps it fresh, and this
    rebuilds it. Run it after any replay. That is not a workaround; it is the
    same class of operation as the replay itself, which also rewrites history.

    IT COVERS `trace_summaries` TOO, AND THAT WAS NOT THE ORIGINAL PLAN
    This script was written for the new rollup. Assertion E4 then showed
    `trace_summaries` -- which has existed since Day 2 -- drifting by exactly the
    same mechanism and for exactly the same reason: it is also a materialized
    view, so it also counts a replayed span twice. Assertion B4 never caught it
    because B4 counts distinct TRACES, and a replayed span does not add a trace.
    Repairing one aggregate and not the other would have been arbitrary.

WHY IT REPLACES PARTITIONS RATHER THAN DELETING AND RE-INSERTING
    `DROP PARTITION` followed by `INSERT` leaves a window -- seconds to minutes
    on a real partition -- where the dashboards read a table with a hole in it.
    `REPLACE PARTITION` swaps it atomically from a staging table, so a reader
    sees either the old numbers or the new ones and never a gap. A tool that
    repairs your metrics should not break them while it runs.

    python scripts/recompute_rollups.py [--days N] [--date YYYY-MM-DD ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from normalizer.config import Settings

#: Each span resolved to its latest normalization_version, exactly as
#: `query/repository.py` does. This is the whole point of the script: the
#: materialized view cannot express it, and this can.
#:
#: EVERY alias is `s_`-prefixed (D52). `AS timestamp` over `argMax(timestamp,...)`
#: makes ClickHouse resolve the `WHERE toDate(timestamp)` to the aggregate and
#: reject the query with a confusing ILLEGAL_AGGREGATION. That rule was written
#: after it bit three times in the query layer, and it bit again here the first
#: time this script ran -- the prefix is cosmetic and its absence is not.
LATEST_SPANS = """
    SELECT
        tenant_id,
        project_id,
        trace_id,
        span_id,
        argMax(timestamp, normalization_version)              AS s_timestamp,
        argMax(duration_ns, normalization_version)            AS s_duration_ns,
        argMax(span_name, normalization_version)              AS s_span_name,
        argMax(service_name, normalization_version)           AS s_service_name,
        argMax(service_version, normalization_version)        AS s_service_version,
        argMax(deployment_environment, normalization_version) AS s_environment,
        argMax(mcp_method, normalization_version)             AS s_mcp_method,
        argMax(mcp_tool_name, normalization_version)          AS s_mcp_tool_name,
        argMax(failure_category, normalization_version)       AS s_failure_category,
        argMax(failure_kind_source, normalization_version)    AS s_failure_kind_source,
        argMax(mcp_is_error, normalization_version)           AS s_mcp_is_error,
        argMax(is_latency_eligible, normalization_version)    AS s_is_latency_eligible
    FROM spans_raw
    WHERE toDate(timestamp) = {day:Date}
    GROUP BY tenant_id, project_id, trace_id, span_id
"""

#: Must stay identical to the SELECT in `012_rollups.sql`. Two copies of an
#: aggregation is exactly how a rollup drifts from its own definition, so
#: assertion E1 compares this table against the raw one on every verify run
#: rather than trusting that the copies stayed in step.
ROLLUP_SELECT = """
    SELECT
        tenant_id,
        project_id,
        toStartOfMinute(s_timestamp) AS bucket,
        s_service_name AS service_name,
        s_mcp_method AS mcp_method,
        s_mcp_tool_name AS mcp_tool_name,
        s_failure_category AS failure_category,
        count() AS calls,
        sum(s_mcp_is_error) AS errors,
        quantilesStateIf(0.50, 0.95, 0.99)(s_duration_ns, s_is_latency_eligible = 1) AS latency,
        countIf(s_is_latency_eligible = 1) AS latency_count,
        maxIf(s_duration_ns, s_is_latency_eligible = 1) AS latency_max,
        countIf(s_is_latency_eligible = 1 AND s_duration_ns = 0) AS zero_duration,
        min(if(s_is_latency_eligible = 1 AND s_duration_ns > 0, s_duration_ns, NULL))
            AS min_tick_ns,
        countIf(s_failure_kind_source = 'helper') AS helper_classified,
        max(s_timestamp) AS last_seen,
        anyLast(s_service_version) AS service_version,
        anyLast(s_environment) AS environment
    FROM ({latest})
    GROUP BY tenant_id, project_id, bucket, service_name, mcp_method,
             mcp_tool_name, failure_category
"""


#: Rebuilt from `spans_raw`, in the same shape their materialized views define.
#: `partition` is the expression each table's PARTITION BY uses, so that
#: REPLACE PARTITION targets exactly one day's worth of rows.
TARGETS: dict[str, str] = {
    "tool_metrics_1m": "toDate(bucket)",
    "trace_summaries": "trace_date",
}

#: Must stay identical to the SELECT in `013_trace_summaries_partitioned.sql`.
TRACE_SUMMARY_SELECT = """
    SELECT
        tenant_id,
        project_id,
        toDate(s_timestamp) AS trace_date,
        trace_id,
        min(s_timestamp) AS start_time,
        max(toDateTime64(
            (toUnixTimestamp64Nano(s_timestamp) + s_duration_ns) / 1000000000, 9)) AS end_time,
        sum(1) AS span_count,
        sum(s_mcp_is_error) AS error_span_count,
        max(s_service_name) AS service_name,
        max(s_mcp_tool_name) AS tool_name,
        max(s_mcp_method) AS mcp_method,
        argMaxState(s_failure_category, s_mcp_is_error) AS failure_category,
        argMinState(s_span_name, s_timestamp) AS first_span_name
    FROM ({latest})
    GROUP BY tenant_id, project_id, trace_date, trace_id
"""

SELECTS: dict[str, str] = {
    "tool_metrics_1m": ROLLUP_SELECT,
    "trace_summaries": TRACE_SUMMARY_SELECT,
}


class RollupRecomputer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import clickhouse_connect

            self._client = clickhouse_connect.get_client(
                host=self.settings.clickhouse_host,
                port=self.settings.clickhouse_port,
                username=self.settings.clickhouse_user,
                password=self.settings.clickhouse_password,
                database=self.settings.clickhouse_db,
                secure=self.settings.clickhouse_secure,
            )
        return self._client

    def dates(self, days: int | None) -> list[str]:
        """Partitions present in `spans_raw`, newest first."""
        window = f"WHERE timestamp > now() - INTERVAL {days} DAY" if days else ""
        rows = self.client.query(
            f"SELECT DISTINCT toDate(timestamp) AS d FROM spans_raw {window} ORDER BY d DESC"
        ).result_rows
        return [str(row[0]) for row in rows]

    def _sort_key(self, table: str) -> str:
        return self.client.query(
            "SELECT sorting_key FROM system.tables "
            "WHERE database = currentDatabase() AND name = {t:String}",
            parameters={"t": table},
        ).result_rows[0][0]

    def recompute_table(self, table: str, day: str) -> int:
        staging = f"{table}_recompute"
        # `AS <target>` copies the COLUMNS, so staging cannot drift from the
        # target's schema. The engine is restated because copying it verbatim
        # would copy the ZooKeeper replica path too, and a second table claiming
        # the same path fails with REPLICA_ALREADY_EXISTS. Staging is scratch,
        # so it is deliberately unreplicated -- REPLACE PARTITION needs matching
        # structure and partition key, not matching replication. The sort key is
        # read back from the server rather than restated, so it cannot drift
        # either.
        self.client.command(f"DROP TABLE IF EXISTS {staging}")
        self.client.command(
            f"CREATE TABLE {staging} AS {table} "
            f"ENGINE = AggregatingMergeTree() PARTITION BY {TARGETS[table]} "
            f"ORDER BY ({self._sort_key(table)})"
        )
        try:
            self.client.command(
                f"INSERT INTO {staging} " + SELECTS[table].format(latest=LATEST_SPANS),
                parameters={"day": day},
            )
            rows = self.client.query(f"SELECT count() FROM {staging}").result_rows[0][0]
            # Atomic: readers see the old numbers or the new ones, never a gap.
            # Replicated tables otherwise acknowledge the ALTER after it is
            # queued locally. A caller that queries immediately can briefly see
            # both the replaced and replacement parts and conclude the repair
            # failed. `alter_sync=2` waits for every active replica, making the
            # command's return value the operational completion boundary.
            self.client.command(
                f"ALTER TABLE {table} REPLACE PARTITION '{day}' FROM {staging}",
                settings={"alter_sync": 2},
            )
            return int(rows)
        finally:
            self.client.command(f"DROP TABLE IF EXISTS {staging}")

    def recompute(self, day: str) -> int:
        return sum(self.recompute_table(table, day) for table in TARGETS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=None, help="only the last N days")
    parser.add_argument("--date", action="append", default=None, help="specific date(s)")
    args = parser.parse_args()

    recomputer = RollupRecomputer()
    days = args.date or recomputer.dates(args.days)
    if not days:
        print("no spans_raw partitions to recompute")
        return 0

    total = 0
    for day in days:
        for table in TARGETS:
            rows = recomputer.recompute_table(table, day)
            total += rows
            print(f"  {day}  {table:<18} {rows:>7} rows")
    print(f"\n  {len(days)} partition(s), {total} rows rebuilt from spans_raw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
