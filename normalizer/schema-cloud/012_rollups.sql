-- CLOUD OVERRIDE of schema/012_rollups.sql -- identical except for the
-- ENGINE line. See schema-cloud/001_spans_raw.sql's header for why.
--
-- DF-7: minute rollups.
--
-- Deferred to Day 4-5 deliberately, so they would encode the query patterns the
-- API actually validated rather than a guess made before it existed. Days 3-5
-- validated them, so this is now overdue rather than early.
--
-- ONE TABLE, NOT TWO
-- The register named `server_metrics_1m` AND `tool_metrics_1m`. Building both
-- would be a mistake: the server view is exactly the tool view with the tool
-- column dropped, so a second materialized view would be a second copy of the
-- same arithmetic, free to drift from the first. With `mcp_tool_name` in the
-- ORDER BY, `GROUP BY service_name` over this one table answers the server
-- question -- and answers it EXACTLY, because the tool names are in the key, so
-- `uniqExact` over them is not an estimate. Two tables would have bought a
-- second source of truth and nothing else.
--
-- WHY `failure_category` IS IN THE KEY
-- Every summary view wants a breakdown by category alongside its counts. With
-- the category in the key that is a `GROUP BY`; without it, it needs a separate
-- aggregate state per category. It multiplies rows by ~7 (the category count is
-- fixed and small), which is a good trade for making the breakdown free.
--
-- THE REPLAY HAZARD, STATED PLAINLY
-- Every read of `spans_raw` goes through `argMax(..., normalization_version)`
-- (D24), because a replay leaves several versions of a span and a naive read
-- mixes corrected rows with buggy ones. A materialized view CANNOT do that: it
-- sees an insert batch, not a version history, so a replay ADDS to these
-- counters instead of superseding.
--
-- That is not a reason to skip rollups; it is a reason to say what maintains
-- them. Two things do:
--   * `scripts/recompute_rollups.py` (`make rollups`) drops and rebuilds
--     affected partitions from `spans_raw` through the same argMax the query
--     layer uses. Run it after any replay -- it is the same class of operation
--     as the replay itself.
--   * Assertion E1 compares every rollup counter against the raw table on
--     every verify run. If a replay happens and nobody recomputes, the build
--     says so. An aggregate silently disagreeing with the table it is built
--     from is the worst failure mode this system has (D39) -- so it is checked
--     rather than hoped for.
--
-- RETENTION
-- 90 days, against 7 for `spans_raw`. Outliving the raw data is the entire
-- point: a rollup row is a few hundred bytes per minute per tool, so trend
-- history costs almost nothing. `trace_summaries` deliberately does NOT do this
-- (see 013) because a trace summary implies a drill-down that would 404.
CREATE TABLE IF NOT EXISTS mcpobs.tool_metrics_1m
(
    tenant_id        LowCardinality(String),
    project_id       LowCardinality(String),
    bucket           DateTime,
    service_name     LowCardinality(String),
    mcp_method       LowCardinality(String),
    mcp_tool_name    String,
    failure_category LowCardinality(String),

    calls            SimpleAggregateFunction(sum, UInt64),
    errors           SimpleAggregateFunction(sum, UInt64),

    -- Latency over ELIGIBLE spans only (D29). A `subscriptions/listen` span's
    -- duration is a stream lifetime, not a latency, and one of them in a p95
    -- destroys the chart. Filtered HERE, at write time, so the rollup cannot be
    -- read in a way that forgets -- the same reasoning that made
    -- `is_latency_eligible` a column instead of a query-time WHERE.
    latency          AggregateFunction(quantiles(0.50, 0.95, 0.99), UInt64),
    latency_count    SimpleAggregateFunction(sum, UInt64),
    latency_max      SimpleAggregateFunction(max, UInt64),

    -- DF-4: the clock, measured rather than assumed.
    -- OTel timestamps spans with `time.time_ns()`, whose real granularity is a
    -- property of the customer's host, not of our pipeline. Recording the
    -- zero-duration count and the smallest non-zero duration per bucket makes
    -- the granularity OBSERVABLE per service, so the console can refuse to show
    -- percentiles the clock cannot support instead of showing confident wrong
    -- numbers.
    --
    -- `min_tick_ns` is NULLABLE on purpose. `minIf` over UInt64 returns 0 when
    -- no row matches, and 0 is a legal-looking tick that would merge into the
    -- aggregate and permanently claim a granularity of zero -- the one value
    -- that would make the caveat never fire. NULL means "no sample"; `min`
    -- skips it.
    zero_duration    SimpleAggregateFunction(sum, UInt64),
    min_tick_ns      SimpleAggregateFunction(min, Nullable(UInt64)),

    -- How much of the error intelligence is real versus coarse span-derived
    -- guesswork (D21). Mixing the two silently would misrepresent the core
    -- claim of the product.
    helper_classified SimpleAggregateFunction(sum, UInt64),

    last_seen        SimpleAggregateFunction(max, DateTime64(9)),
    service_version  SimpleAggregateFunction(anyLast, String),
    environment      SimpleAggregateFunction(anyLast, String)
)
ENGINE = ReplicatedAggregatingMergeTree
PARTITION BY toDate(bucket)
ORDER BY (tenant_id, project_id, bucket, service_name, mcp_method, mcp_tool_name, failure_category)
TTL toDate(bucket) + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS mcpobs.tool_metrics_1m_mv
TO mcpobs.tool_metrics_1m AS
SELECT
    tenant_id,
    project_id,
    toStartOfMinute(timestamp) AS bucket,
    service_name,
    mcp_method,
    mcp_tool_name,
    failure_category,
    count() AS calls,
    sum(mcp_is_error) AS errors,
    quantilesStateIf(0.50, 0.95, 0.99)(duration_ns, is_latency_eligible = 1) AS latency,
    countIf(is_latency_eligible = 1) AS latency_count,
    maxIf(duration_ns, is_latency_eligible = 1) AS latency_max,
    countIf(is_latency_eligible = 1 AND duration_ns = 0) AS zero_duration,
    min(if(is_latency_eligible = 1 AND duration_ns > 0, duration_ns, NULL)) AS min_tick_ns,
    countIf(failure_kind_source = 'helper') AS helper_classified,
    max(timestamp) AS last_seen,
    anyLast(service_version) AS service_version,
    anyLast(deployment_environment) AS environment
FROM mcpobs.spans_raw
GROUP BY tenant_id, project_id, bucket, service_name, mcp_method, mcp_tool_name, failure_category;
