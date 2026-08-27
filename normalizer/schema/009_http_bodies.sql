-- HTTP request/response bodies for downstream calls. Supersedes the note in
-- 008, which said bodies were "not capturable this way" (D60).
--
-- That was wrong, and wrong in an instructive way: the OTel HTTP
-- instrumentation genuinely does not record bodies, but it DOES expose
-- `request_hook`/`response_hook`, which is the seam. The conclusion had been
-- drawn from the absence of a feature rather than from the absence of a seam.
--
-- Captured by `mcpobs.instrument_httpx()` in the customer's process, redacted
-- and truncated there -- so, like every other payload column, nothing reaches
-- this table that was not already bounded and scrubbed at the source.
--
-- Headers are filtered to an allow-list; `authorization` and `cookie` never
-- leave the customer's process at all.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS http_request_body String DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS http_response_body String DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS http_request_headers String DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS http_response_headers String DEFAULT '';
