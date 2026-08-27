-- U1: the refined failure taxonomy.
--
-- `failure_category` stays the single product-facing field. When the helper
-- middleware is present it carries a real distinction (server_exception vs
-- unknown_tool vs invalid_arguments vs tool_error); without it, it degrades to
-- the coarse value the raw span supports. `failure_kind_source` records which,
-- so a dashboard can say "12% of your servers are not running the helper"
-- rather than silently mixing two different data qualities.
--
-- First migration applied by MigrationRunner rather than by the container's
-- init hook -- which is the point of having built the runner.

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS failure_kind_source LowCardinality(String) DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS classifier_version UInt16 DEFAULT 0;
