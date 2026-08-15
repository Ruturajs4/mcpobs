-- Captured error text from failing tool calls.
--
-- WHY: 140 of 146 failing spans carried no message anywhere. The SDK's
-- reachable branch calls set_status(StatusCode.ERROR) with no description
-- (D13), so status_message is empty and "what actually went wrong" was
-- unanswerable from the console.
--
-- DELIBERATELY NOT input_preview/output_preview. Those belong to payload
-- capture (V2 §15 / DF-8), which stays a separate opt-in feature writing
-- separate columns -- and assertion B2 still requires them to be NULL. Keeping
-- this in its own column is what stops "we capture errors" from drifting into
-- "we capture everything".
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS failure_detail String DEFAULT '';
