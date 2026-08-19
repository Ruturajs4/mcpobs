# Lite deployment

The same product on four containers instead of eleven, with no Kafka, no
object storage, and no Redis. If you are evaluating self-hosting and the full
stack's footprint is the thing giving you pause, start here.

## What you get

Every part of the actual product is unchanged: MCP semantic extraction, the
five-category failure taxonomy, multi-round-trip tracking, trace assembly,
the console, and insert idempotency (the same `ReplicatedMergeTree` +
embedded Keeper the full stack uses, so a retried export still cannot
duplicate a span).

## What's different

| | Full stack | Lite |
| --- | --- | --- |
| Containers | 11 | 4 (ClickHouse, Postgres, `ingest`, `query`) |
| Measured idle RAM | ~2.8 GiB | ~420 MiB |
| Ingest path | Collector → Kafka → normalizer | `ingest` decodes, normalizes and inserts in one step |
| Ack boundary | 200 once Kafka durably has the batch | 200 once ClickHouse's insert returns |

`ingest` still authenticates every request, still overwrites tenant/project
attributes from your key rather than trusting the payload, and still applies
the exact same ClickHouse schema — there is no Kafka in front of it to hand
the work to, so it calls the decode → normalize → insert pipeline directly.

## What you give up

Read this before choosing lite for anything beyond evaluation.

- **No replay.** The full stack keeps 48–72 hours of raw OTLP in Kafka
  specifically so a normalizer bug or a new derived field can be reprocessed
  without asking you to resend anything. Lite has no such buffer: once a
  batch is inserted, fixing how it was normalized means re-deriving from
  what's already stored, not replaying the original bytes.
- **No long-retention archive.** The full stack's archiver writes every span
  to object storage as parquet. Lite doesn't run one — what's in ClickHouse
  is what you have.
- **No per-tenant quota isolation.** Quota enforcement is already
  best-effort in the full stack (it fails open if Redis is unreachable);
  lite simply never has Redis, so every request is counted per-process
  rather than shared across replicas. For a single-tenant, single-instance
  deployment this is the same behavior you'd get anyway.
- **Slower under sustained load spikes.** The full stack's Collector queues
  and retries on your behalf; lite's ack boundary is ClickHouse's own insert
  latency. Normal traffic is unaffected — this only shows up if you're
  pushing enough volume that batching would have mattered.

None of this is a smaller product — it's a different one, chosen for a
different constraint (footprint over throughput headroom and long-term
replay). If you outgrow it, the upgrade is additive: the schema, the SDK, and
every module lite calls are the same ones the full stack uses. Putting Kafka
in front of `ingest` later does not require touching data you've already
stored.

## Run it

```bash
git clone https://github.com/Ruturajs4/mcpobs.git
cd mcpobs
make up-lite
make devkeys-lite
```

`make devkeys-lite` writes a read key and an ingest key to a local,
git-ignored file, provisioned against the lite stack's own Postgres. Same
invite-only model as the full stack: no self-service signup.

Point your server at it exactly as in the [quickstart](quickstart.md):

```python
from mcpobs import instrument

instrument(mcp)
```

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<your ingest key>"
```

!!! warning "Cannot run alongside the full stack"

    Lite uses the same host ports (8123, 5433, 4319, 8080) as `docker-compose.yml`,
    deliberately — you run one deployment or the other, not both against the
    same client config. `make down` before `make up-lite`, and vice versa.

Open the console at `http://localhost:8080` and sign in with the **read**
key, exactly as in the full stack.

## Bring your own database

Two containers instead of four: no local ClickHouse, no local Postgres.
Point `ingest` and `query` at databases you already run — ClickHouse Cloud,
your own self-hosted ClickHouse, and RDS/Supabase/Neon/Cloud SQL or any
Postgres.

Nothing about `ingest` or `query` assumes its database is a sibling
container — connection details are read from environment variables either
way. This is the same lite image, with the two managed database containers
removed.

```bash
cat > .env <<'EOF'
CONTROL_PLANE_DSN=postgresql://user:pass@your-postgres-host:5432/mcpobs?sslmode=require
CLICKHOUSE_HOST=your-instance.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_PASSWORD=your-clickhouse-password
EOF

make up-byo
make devkeys
```

`docker compose` reads `.env` automatically. `CONTROL_PLANE_DSN`,
`CLICKHOUSE_HOST`, `CLICKHOUSE_PORT` and `CLICKHOUSE_PASSWORD` have no
defaults and the stack refuses to start without them — better than silently
falling back to a hostname that will never resolve. `CLICKHOUSE_SECURE`
defaults to `true` here (most external ClickHouse is TLS-only); set it to
`false` if yours genuinely isn't.

!!! warning "Check ReplicatedMergeTree support before committing to this"

    This project's idempotent inserts (spans surviving a retried export
    without duplicating) rely on `ReplicatedMergeTree`, which needs a working
    Keeper. ClickHouse Cloud has this built in. A bare self-hosted
    `clickhouse-server` with no Keeper configured will fail the schema
    migration outright the first time `ingest` starts — see
    `clickhouse/config.d/keeper.xml` in the repository for what the managed
    deployments set up on your behalf.

## Next

- [Your first trace](first-trace.md) — how to read what you're now collecting.
- [Session tokens for stdio](../operate/session-tokens.md) — still fully
  supported in lite; nothing about credential minting depends on Kafka.
