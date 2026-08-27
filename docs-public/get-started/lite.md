# Lite deployment

The same product on three containers instead of eleven, with no Kafka, no
object storage, no Redis, and no Postgres. This is the self-hosted image:
single-tenant, no key management, run it and point your server at it.

## What you get

Every part of the actual product is unchanged: MCP semantic extraction, the
five-category failure taxonomy, multi-round-trip tracking, trace assembly,
the console, and insert idempotency (the same `ReplicatedMergeTree` +
embedded Keeper the full stack uses, so a retried export still cannot
duplicate a span).

## What's different

| | Full (managed, multi-tenant) | Lite (self-hosted) |
| --- | --- | --- |
| Containers | 11 | 3 (ClickHouse, `ingest`, `query`) |
| Measured idle RAM | ~2.8 GiB | ~420 MiB |
| Ingest path | Collector → Kafka → normalizer | `ingest` decodes, normalizes and inserts in one step |
| Ack boundary | 200 once Kafka durably has the batch | 200 once ClickHouse's insert returns |
| Tenancy | Multi-tenant, real per-org keys | Single tenant, any `x-api-key` value accepted |

`ingest` still stamps every span with tenant/project attributes rather than
trusting the payload, and still applies the exact same ClickHouse schema —
there is no Kafka in front of it to hand the work to, so it calls the
decode → normalize → insert pipeline directly. Lite just has one static
tenant to stamp, since there's no control plane here to authenticate a
key into a real organisation.

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
- **No quota enforcement at all.** There's no control plane to hold a limit
  against, so every request is accepted regardless of volume. For a
  single-tenant self-hosted install this is usually fine — there's no other
  tenant to protect from a burst.
- **No real access control.** Any `x-api-key` value is accepted, including
  none. This is appropriate for a self-hosted, single-operator install; it
  is not a mode to expose on a network anyone untrusted can reach.
- **Slower under sustained load spikes.** The full stack's Collector queues
  and retries on your behalf; lite's ack boundary is ClickHouse's own insert
  latency. Normal traffic is unaffected — this only shows up if you're
  pushing enough volume that batching would have mattered.

None of this is a smaller product — it's a different one, chosen for a
different constraint (footprint and zero-config over throughput headroom,
long-term replay, and multi-tenant isolation). The schema and the SDK are
identical either way; what changes moving to a managed, multi-tenant
deployment is the control plane in front of `ingest`/`query`, not the data
model underneath.

## Run it

```bash
git clone https://github.com/Ruturajs4/mcpobs.git
cd mcpobs
make up-lite
```

Nothing to provision — single-tenant means there's no key to issue. Point
your server at it exactly as in the [quickstart](quickstart.md):

```python
from mcpobs import instrument

instrument(mcp)
```

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=anything"
```

!!! warning "Cannot run alongside a managed multi-tenant deployment on the same host"

    Lite uses fixed host ports (8123, 9000, 4319, 8080) for its own
    containers. If you're also running a separate multi-tenant deployment
    locally, run one at a time against those ports.

Open the console at `http://localhost:8080`. It still asks for a key on
first load — type anything, it isn't checked against anything here.

## Bring your own ClickHouse or real multi-tenancy

A managed deployment adds a real control plane in front of `ingest`/`query`
(Postgres-backed orgs, per-org API keys, quotas, an admin console) and a
ready-made bring-your-own-database compose file for pointing at ClickHouse
Cloud or your own Postgres. Neither ships in this self-hosted image — see
[API keys and access](../operate/keys.md) for what that model looks like.

## Next

- [Your first trace](first-trace.md) — how to read what you're now collecting.
- [Session tokens for stdio](../operate/session-tokens.md) — still fully
  supported in lite; nothing about credential minting depends on Kafka.
