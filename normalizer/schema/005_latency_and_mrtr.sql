-- U5: latency eligibility and MRTR correlation.
--
-- ---------------------------------------------------------------------------
-- is_latency_eligible: a COLUMN, not a query-time filter.
-- ---------------------------------------------------------------------------
-- A `subscriptions/listen` span's duration is a STREAM LIFETIME, not a latency.
-- One of them in a p95 destroys the chart. The same is true of an MRTR interim
-- round, whose duration excludes the client think-time that dominates the real
-- wait.
--
-- Enforced as a stored column so the default is safe. A query-time WHERE clause
-- gets forgotten in exactly one place and the bug ships; a column forces an
-- explicit opt-in to include stream spans.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS is_latency_eligible UInt8 DEFAULT 1;

-- ---------------------------------------------------------------------------
-- MRTR correlation (D28).
-- ---------------------------------------------------------------------------
-- MEASURED (scripts/mrtr_experiment.py): one logical tool call becomes TWO
-- tools/call spans, and they do NOT share a trace_id -- the client's retry
-- starts a new trace. The only link is `requestState`, which the server emits
-- on the asking round and the client echoes back on the answering round.
--
-- Stored as a HASH, never the raw blob: requestState carries recorded
-- elicitation outcomes, i.e. the user's actual answers. Storing it would be
-- payload capture through the back door (D17).
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS mrtr_state_out LowCardinality(String) DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS mrtr_state_in LowCardinality(String) DEFAULT '';
