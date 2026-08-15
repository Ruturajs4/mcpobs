-- U6: downstream dimensions beyond HTTP.
--
-- The V2 trace-waterfall promise (§6.1) is that a slow tool call shows WHERE
-- the time went -- MCP handling, HTTP, database, Redis, LLM or internal work.
-- Day 1 promoted only the HTTP dimensions, so a tool whose latency came from a
-- database or an LLM looked like unexplained server time.
--
-- `db_system` already existed but was never populated by a real span; these are
-- the columns it needs alongside it, plus the LLM dimensions a server invoking
-- a model would emit.

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS db_operation LowCardinality(String) DEFAULT '';

-- NOT the statement text. `db.query.text` can contain literals -- customer
-- identifiers, emails, anything a developer inlined into SQL -- so capturing it
-- is payload capture with a different name (V2 §15). The *summary* is the
-- parameterised shape, which is what you actually group by.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS db_collection LowCardinality(String) DEFAULT '';

-- LLM calls made BY the server. Distinct from anything the client's model does:
-- we see only what crosses our boundary (V2 §2.2).
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS gen_ai_system LowCardinality(String) DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS gen_ai_model LowCardinality(String) DEFAULT '';

-- Token counts are the cost signal for a server that calls an LLM downstream.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS gen_ai_input_tokens Nullable(UInt32);

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS gen_ai_output_tokens Nullable(UInt32);

-- Which downstream kind explains this span's time. Derived once at normalize
-- time so the waterfall does not re-derive it per query, and so "unexplained
-- server time" is a value you can filter on rather than an absence you have to
-- infer.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS downstream_kind LowCardinality(String) DEFAULT '';
