-- The downstream equivalent of request/response.
--
-- These were already stored -- unredacted -- inside `span_attributes`, so
-- "we do not promote db.statement" (D36) was giving privacy theatre rather than
-- privacy: the value was in the row either way, just somewhere less obvious.
-- Promoting them REDACTED, and redacting them in the raw map too, is strictly
-- safer than the status quo as well as more useful.
--
-- Not captured, and not capturable this way: HTTP request/response BODIES. The
-- OTel HTTP instrumentation does not record them, and `mcpobs` wraps the MCP
-- server, not the customer's outbound HTTP client. See D60.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS http_url String DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS db_statement String DEFAULT '';
