-- U2: trace assembly.
--
-- This is ADR-005 becoming real: incremental aggregation inside ClickHouse
-- instead of a stateful stream processor. The normalizer stays stateless --
-- one span in, one row out -- and the store does the windowing. Late-arriving
-- spans (the normal case, not the exception: a child routinely lands before its
-- parent) merge into the aggregate with no watermark logic at all.

-- ---------------------------------------------------------------------------
-- trace_summaries: one row per trace, maintained incrementally.
-- ---------------------------------------------------------------------------
-- SimpleAggregateFunction where the partial state IS the value (min/max/sum) --
-- those columns are read directly. AggregateFunction only where a real state is
-- needed (argMin/argMax), and those require -Merge at read time.
CREATE TABLE IF NOT EXISTS mcpobs.trace_summaries
(
    tenant_id        LowCardinality(String),
    project_id       LowCardinality(String),
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
ENGINE = AggregatingMergeTree
ORDER BY (tenant_id, project_id, trace_id);
-- No PARTITION BY: every candidate date lives in an aggregate column, whose
-- value changes as parts merge, and a partition key must be deterministic. A
-- trace can also straddle midnight, so toDate(min(timestamp)) is not stable
-- either. Production needs a partitioning and TTL strategy here; the local
-- dataset does not.

CREATE MATERIALIZED VIEW IF NOT EXISTS mcpobs.trace_summaries_mv
TO mcpobs.trace_summaries AS
SELECT
    tenant_id,
    project_id,
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
GROUP BY tenant_id, project_id, trace_id;

-- ---------------------------------------------------------------------------
-- trace_locator: trace_id -> where to look.
-- ---------------------------------------------------------------------------
-- spans_raw is ordered by (tenant, project, time, service, tool) deliberately,
-- per V2 §12.3, because that serves the common dashboard queries. The cost is
-- that a bare `WHERE trace_id = ...` scans. This table turns trace-by-id into a
-- point lookup that yields the tenant, project and date, after which the read
-- from spans_raw is partition-pruned. Day 3's API depends on it.
CREATE TABLE IF NOT EXISTS mcpobs.trace_locator
(
    trace_id   String,
    tenant_id  LowCardinality(String),
    project_id LowCardinality(String),
    trace_date Date,
    first_seen DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(first_seen)
ORDER BY (trace_id, trace_date);

CREATE MATERIALIZED VIEW IF NOT EXISTS mcpobs.trace_locator_mv
TO mcpobs.trace_locator AS
SELECT
    trace_id,
    tenant_id,
    project_id,
    toDate(timestamp) AS trace_date,
    now() AS first_seen
FROM mcpobs.spans_raw;
