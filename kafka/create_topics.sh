#!/usr/bin/env bash
# Topics are an explicit contract (Architecture.md §6), never auto-created.
set -euo pipefail

BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka:9092}"
KT=/opt/kafka/bin/kafka-topics.sh

create() {
  local name="$1" parts="$2" retention_ms="$3"
  if $KT --bootstrap-server "$BOOTSTRAP" --list | grep -qx "$name"; then
    echo "topic exists: $name"
  else
    $KT --bootstrap-server "$BOOTSTRAP" --create \
      --topic "$name" \
      --partitions "$parts" \
      --replication-factor 1 \
      --config "retention.ms=$retention_ms" \
      --config compression.type=zstd
    echo "created topic: $name (partitions=$parts)"
  fi
}

# Local: 6 partitions. Production starts at 24 (Architecture.md §9.2) -- enough
# headroom for growth, small enough for fast rebalances.
# Retention 72h = the replay window (ADR-007). DLQ keeps 14d for triage.
create otlp.spans.raw 6 259200000
create otlp.spans.dlq 2 1209600000

echo "--- topics ---"
$KT --bootstrap-server "$BOOTSTRAP" --list
