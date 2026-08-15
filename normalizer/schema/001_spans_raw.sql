-- Day-1 span table.
--
-- ReplicatedMergeTree EVERYWHERE, including locally.
--
-- The original version of this file was MergeTree with a caveat saying
-- idempotency "must be tested in staging, not on a laptop". That caveat was the
-- problem: `insert_deduplication_token` (ADR-006) is inert on an unreplicated
-- table, so a replayed batch silently duplicated rows while appearing to work,
-- and the single most important correctness property in the pipeline went
-- untested for two days.
--
-- Replicated tables keep their deduplication blocks in Keeper, and that works
-- with ONE replica -- so the local stack runs embedded Keeper
-- (clickhouse/config.d/keeper.xml) and exercises the real engine. The macros
-- mean this DDL is unchanged from here up to a real cluster.
--
-- Column set is justified by docs/observed_attributes.md (T3), not by guesswork.
-- Columns marked NOT EMITTED exist so the schema is stable when the SDK or the
-- semantic conventions start populating them; they are NULL/empty today.

CREATE DATABASE IF NOT EXISTS mcpobs;

CREATE TABLE IF NOT EXISTS mcpobs.spans_raw
(
    -- tenancy (hard-coded to 'local' on Day 1, real in Phase 1)
    tenant_id              LowCardinality(String) DEFAULT 'local',
    project_id             LowCardinality(String) DEFAULT 'local',
    environment            LowCardinality(String) DEFAULT 'local',

    -- trace
    timestamp              DateTime64(9),
    duration_ns            UInt64,
    trace_id               String,
    span_id                String,
    parent_span_id         String,
    span_name              String,
    span_kind              LowCardinality(String),
    status_code            LowCardinality(String),
    status_message         String,

    -- service (from OTel Resource, never span attributes)
    service_name           LowCardinality(String),
    service_version        LowCardinality(String),
    deployment_environment LowCardinality(String),

    -- MCP -- all observed in T3
    mcp_method             LowCardinality(String),
    mcp_tool_name          LowCardinality(String),
    gen_ai_operation       LowCardinality(String),
    protocol_version       LowCardinality(String),
    jsonrpc_request_id     String,

    -- MCP -- NOT EMITTED by mcp 2.0.0 (see T3 report), kept for stability
    mcp_prompt_name        LowCardinality(String),
    mcp_resource_uri       String,
    transport              LowCardinality(String),
    mcp_session_id         Nullable(String),   -- removed from the protocol in 2026-07-28

    -- failure
    mcp_is_error           UInt8 DEFAULT 0,
    result_type            LowCardinality(String),  -- NOT EMITTED; MRTR invisible on Day 1
    failure_category       LowCardinality(String),
    error_type             LowCardinality(String),
    -- STRING, not Int32: the SDK sets str(code). Day-1 doc §9.3 said Int32 and was wrong.
    rpc_status_code        Nullable(String),

    -- downstream (promoted from child spans)
    http_method            LowCardinality(String),
    http_status_code       Nullable(UInt16),
    http_host              String,
    db_system              LowCardinality(String),

    -- payload (opt-in per V2 §15; NULL on Day 1)
    input_size             Nullable(UInt32),
    output_size            Nullable(UInt32),
    input_preview          Nullable(String),
    output_preview         Nullable(String),

    -- raw -- everything we did not promote, so nothing is lost
    resource_attributes    Map(LowCardinality(String), String),
    span_attributes        Map(LowCardinality(String), String),

    normalization_version  UInt16 DEFAULT 1,
    kafka_partition        Int32,
    kafka_offset           Int64,
    ingested_at            DateTime DEFAULT now()
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/spans_raw', '{replica}')
PARTITION BY toDate(timestamp)
ORDER BY (tenant_id, project_id, toStartOfHour(timestamp), service_name, mcp_tool_name)
TTL toDate(timestamp) + INTERVAL 7 DAY;
