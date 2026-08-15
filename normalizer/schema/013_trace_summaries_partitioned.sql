-- DF-3: give `trace_summaries` a partition key and a TTL.
--
-- THE DEFECT
-- `spans_raw` drops after 7 days by partition. `trace_summaries` had neither a
-- PARTITION BY nor a TTL, so it outlived the data it summarised and grew
-- without bound. At laptop scale that is invisible; at the volumes in
-- Architecture section 9.3 it is a table that cannot be dropped by partition and
-- therefore cannot be dropped cheaply at all.
--
-- WHY IT WAS DEFERRED, AND WHY THAT REASONING WAS TOO STRONG
-- 004 recorded that "every candidate date lives in an aggregate column whose
-- value changes as parts merge, and a partition key must be deterministic".
-- True of `min(timestamp)` -- and irrelevant, because the partition key does
-- not have to come from an aggregate. `toDate(timestamp)` of the SPAN BEING
-- INSERTED is deterministic, known at insert time, and never changes. The
-- blocker was the assumption that the trace's date had to be a property of the
-- trace rather than of the row.
--
-- THE COST, WHICH IS REAL BUT TINY
-- Putting `trace_date` in the key means a trace that STRADDLES MIDNIGHT becomes
-- two rows, one per date, with its spans split between them. 004 named this
-- correctly. What it did not weigh is the frequency: a trace lasting T seconds
-- straddles with probability T/86400, so at 10ms per trace that is roughly one
-- trace in eight million. Nor does it have to be lived with -- any reader that
-- does `GROUP BY trace_id` merges the halves back exactly, and reading an
-- AggregatingMergeTree already requires aggregating across unmerged parts, so
-- correct readers pay nothing for this. Assertion B4 now counts distinct
-- traces rather than rows, which is what it always meant.
--
-- RETENTION MATCHES `spans_raw`, DELIBERATELY
-- 7 days, not the 90 the metric rollups get. A rollup row is a statistic and
-- stands alone; a trace summary is a LINK -- every one of them implies a
-- drill-down into spans that must still exist. A summary outliving its spans
-- would put rows in the console that 404 when clicked, which is worse than not
-- showing them.
--
-- A partition key cannot be added by ALTER, so this rebuilds the table and
-- backfills from `spans_raw` -- which is sound precisely because the table is
-- derived data: everything that should exist is reconstructible from the raw
-- spans that are still inside their own retention.
DROP VIEW IF EXISTS mcpobs.trace_summaries_mv;

DROP TABLE IF EXISTS mcpobs.trace_summaries;

CREATE TABLE IF NOT EXISTS mcpobs.trace_summaries
(
    tenant_id        LowCardinality(String),
    project_id       LowCardinality(String),
    -- Deterministic, from the inserted row, never from an aggregate.
    trace_date       Date,
    trace_id         String,

    start_time       SimpleAggregateFunction(min, DateTime64(9)),
    end_time         SimpleAggregateFunction(max, DateTime64(9)),
    span_count       SimpleAggregateFunction(sum, UInt64),
    error_span_count SimpleAggregateFunction(sum, UInt64),

    -- max() over a String picks the non-empty value, because '' sorts lowest.
    -- Correct for the overwhelmingly common single-tool trace; a trace calling
    -- several tools reports one of them. Trace DETAIL reads raw spans and shows
    -- all of them, so the imprecision is confined to list views.
    service_name     SimpleAggregateFunction(max, String),
    tool_name        SimpleAggregateFunction(max, String),
    mcp_method       SimpleAggregateFunction(max, String),

    -- Prefers a category from a failing span: argMax over mcp_is_error.
    failure_category AggregateFunction(argMax, String, UInt8),

    -- The earliest span, used as a PROXY for the root in list views.
    -- The true root is the span whose parent is absent from the trace, which is
    -- not computable incrementally -- and per D7 the MCP span may legitimately
    -- be a CHILD of an instrumented client, so `parent_span_id = ''` is not a
    -- reliable test either. Trace detail resolves the real root at query time
    -- over spans_raw, where the whole trace is visible. (D22)
    first_span_name  AggregateFunction(argMin, String, DateTime64(9))
)
ENGINE = ReplicatedAggregatingMergeTree('/clickhouse/tables/{shard}/trace_summaries_p', '{replica}')
PARTITION BY trace_date
ORDER BY (tenant_id, project_id, trace_date, trace_id)
TTL trace_date + INTERVAL 7 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS mcpobs.trace_summaries_mv
TO mcpobs.trace_summaries AS
SELECT
    tenant_id,
    project_id,
    toDate(timestamp) AS trace_date,
    trace_id,
    min(timestamp) AS start_time,
    max(toDateTime64((toUnixTimestamp64Nano(timestamp) + duration_ns) / 1000000000, 9)) AS end_time,
    sum(1) AS span_count,
    sum(mcp_is_error) AS error_span_count,
    max(service_name) AS service_name,
    max(mcp_tool_name) AS tool_name,
    max(mcp_method) AS mcp_method,
    argMaxState(failure_category, mcp_is_error) AS failure_category,
    argMinState(span_name, timestamp) AS first_span_name
FROM mcpobs.spans_raw
GROUP BY tenant_id, project_id, trace_date, trace_id;

-- Backfill. Reads through argMax over normalization_version, exactly as the
-- query layer does (D24): rebuilding from the raw table is the one chance to
-- resolve replayed spans to their corrected version, and inserting every
-- version would bake a replay's double-count into the new table on day one.
INSERT INTO mcpobs.trace_summaries
SELECT
    tenant_id,
    project_id,
    toDate(span_time) AS trace_date,
    trace_id,
    min(span_time) AS start_time,
    max(toDateTime64((toUnixTimestamp64Nano(span_time) + duration_ns) / 1000000000, 9)) AS end_time,
    sum(1) AS span_count,
    sum(mcp_is_error) AS error_span_count,
    max(service_name) AS service_name,
    max(mcp_tool_name) AS tool_name,
    max(mcp_method) AS mcp_method,
    argMaxState(failure_category, mcp_is_error) AS failure_category,
    argMinState(span_name, span_time) AS first_span_name
FROM (
    SELECT
        tenant_id,
        project_id,
        trace_id,
        span_id,
        argMax(timestamp, normalization_version)        AS span_time,
        argMax(duration_ns, normalization_version)      AS duration_ns,
        argMax(span_name, normalization_version)        AS span_name,
        argMax(service_name, normalization_version)     AS service_name,
        argMax(mcp_tool_name, normalization_version)    AS mcp_tool_name,
        argMax(mcp_method, normalization_version)       AS mcp_method,
        argMax(mcp_is_error, normalization_version)     AS mcp_is_error,
        argMax(failure_category, normalization_version) AS failure_category
    FROM mcpobs.spans_raw
    GROUP BY tenant_id, project_id, trace_id, span_id
)
GROUP BY tenant_id, project_id, trace_date, trace_id;
