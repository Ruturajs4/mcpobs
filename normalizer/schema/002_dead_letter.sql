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
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{shard}/ingest_dead_letter', '{replica}')
ORDER BY received_at
TTL received_at + INTERVAL 3 DAY;
