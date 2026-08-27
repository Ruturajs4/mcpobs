-- CLOUD OVERRIDE of schema/002_dead_letter.sql -- identical except for the
-- ENGINE line. See schema-cloud/001_spans_raw.sql's header for why.
--
-- Poison messages land here and the partition advances. A stalled partition is
-- never "fixed" by skipping a message without a DLQ write -- that is data loss
-- with extra steps.

CREATE TABLE IF NOT EXISTS mcpobs.ingest_dead_letter
(
    received_at     DateTime DEFAULT now(),
    reason          LowCardinality(String),
    detail          String,
    kafka_partition Int32,
    kafka_offset    Int64,
    raw_body        String
)
ENGINE = ReplicatedMergeTree
ORDER BY received_at
TTL received_at + INTERVAL 3 DAY;
