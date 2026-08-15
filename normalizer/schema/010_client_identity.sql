-- Which client made the call.
--
-- V2 6.1 asks "which clients are calling which tools", and until now the
-- honest answer was that we could not tell. The SDK sets no client attribute
-- on the span. The information was never actually missing, though: every MCP
-- request carries `_meta."io.modelcontextprotocol/clientInfo"`, and we were
-- discarding it because we only looked at the parts of `params` we already had
-- a column for.
--
-- Promoted to a real column rather than left inside `input_preview`, because
-- payload capture is OFF by default and client identity must not be a
-- side-effect of turning on something far more invasive.
--
-- Self-reported and unverified, exactly as the spec warns. Safe for a
-- breakdown; never an authorisation input.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS client_name LowCardinality(String) DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS client_version LowCardinality(String) DEFAULT '';
