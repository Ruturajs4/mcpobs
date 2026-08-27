-- Messaging dimensions, so a queue publish explains its own time.
--
-- `downstream_kind = 'messaging'` has been classified correctly since U6 and
-- had NO promoted attributes behind it: the console rendered a grey tag and
-- nothing else, because every field the UI needed was buried in
-- `span_attributes`. That is the same gap DF-12 named for `db.operation` --
-- data present and discarded on its way to a column -- one span kind over.
--
-- Promoted rather than read from the raw map at render time, for the reason
-- D59 gives: the raw map is a fallback for replay, not a query surface. A
-- console that reaches into it for a field is a console that cannot filter or
-- group by that field.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS messaging_system LowCardinality(String) DEFAULT '';

-- The topic or queue. NOT LowCardinality: a queue name is a customer's
-- namespace and can run to thousands, which is exactly the case
-- LowCardinality degrades on.
ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS messaging_destination String DEFAULT '';

ALTER TABLE mcpobs.spans_raw
    ADD COLUMN IF NOT EXISTS messaging_operation LowCardinality(String) DEFAULT '';
