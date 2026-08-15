# mcp-observability

MCP-native observability. Day-1 vertical slice: a Python MCP SDK v2 server's
spans travel **OTel → Collector → Kafka → stateless normalizer → ClickHouse** and
come back out as a queryable, MCP-aware trace.

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
make check     # unit tests + ruff + mypy (no stack required)
make up        # clickhouse, kafka, collector, postgres, minio, ingest,
               # normalizer, archiver, query API
make devkeys   # provision a local org + API keys (invite-only; see below)
make demo      # run the tool scenarios over stdio and streamable HTTP
make verify    # A-F acceptance assertions

open http://localhost:8080     # the console
```

## Structure

`normalizer/` is production code and is held to a strict bar: typed Pydantic
models, `mypy --disallow-untyped-defs`, full ruff rule set. `demo_server/`,
`scripts/` and `tests/` are the harness and are linted more loosely.

- `models.py` — `SpanRow` is the single declaration of the ClickHouse schema;
  insert columns are **derived** from it, so a column list can never drift out
  of alignment with the values.
- `migrations.py` — `MigrationRunner` applies ordered `.sql` files once, tracked
  by checksum in a `schema_migrations` table. Replaces `docker-entrypoint-initdb.d`,
  which only runs against an empty data directory and so silently ignored every
  schema change after the first boot.
- `consumer.py` — `SpanBatch` owns rows *and* offsets, because a batch can hold
  offsets with no rows (everything dead-lettered) and those offsets still must
  be committed.
- `taxonomy.py` / `normalize.py` — stateless transforms per ADR-005.

`make attrs` regenerates `docs/observed_attributes.md` — the observed span
contract, which outranks every document where they disagree.

## Design

- [`docs/Architecture.md`](docs/Architecture.md) — system of record, ADR-001…010
- [`docs/decisions.md`](docs/decisions.md) — decision log, including the Day-1 findings
- [`docs/observed_attributes.md`](docs/observed_attributes.md) — **generated**, the span contract
- [`docs/deferred.md`](docs/deferred.md) — everything knowingly postponed, and what forces it back

## Getting a key

The platform is **invite-only**. There is no signup page, and there is no
endpoint that creates an organisation -- an operator does it from the database
side, and every account after the first exists because someone already inside
issued an invite:

```bash
python scripts/admin.py bootstrap --org acme --email you@acme.com   # once
python scripts/admin.py accept --code <code> --name "You"
python scripts/admin.py key --org acme --scopes ingest              # for servers
python scripts/admin.py key --org acme --scopes read                # for the console
```

Keys are shown **once**. The database stores only a hash, so there is no command
that can print one again -- if you lose it, revoke it and issue another.

`ingest` and `read` are separate scopes on purpose: an ingest key lives in your
server process and your deployment config, a read key in a browser, and one
being compromised should not imply the other.

Point your exporter at the gateway, not at a Collector:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4319/v1/traces
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=mcpo_production_...
```

Your `tenant.id`, `project.id` and `data.region` resource attributes are
**overwritten** from the key. That is not a limitation -- it is what stops
anyone writing telemetry into someone else's account by setting an attribute.

Locally, `make devkeys` provisions an org and both keys into a gitignored
`.mcpobs-keys.env`, and `make demo` uses them automatically.

## Failure intelligence

`MCPServer` converts every tool failure — raised exception, `isError` result,
unknown tool, schema violation — into `error.type="tool_error"` before the OTel
middleware sees it, so the raw span cannot tell them apart (`docs/decisions.md`
D13).

`mcpobs` recovers the distinction. One line in the customer's server:

```python
from mcpobs import instrument
instrument(mcp)
```

**What leaves your process.** By default this sends, for failing tool calls
only: the failure category, and the error text truncated to 512 characters.
Successful results are never read. Tool inputs and outputs are never captured —
that is a separate opt-in feature.

Error text is captured because without it nobody can answer *why* a call
failed: the MCP SDK reports an ERROR status with no message, so the reason
exists only inside the result. Be aware an exception string can contain user
data. To send only the category and no text:

```python
instrument(mcp, capture_error_detail=False)
```

**Tool arguments and results** are NOT sent by default. Enable them when you
need to see what a call actually received and returned:

```python
instrument(mcp, capture_payloads=True)
```

They are truncated to 2048 characters and passed through pattern-based
redaction (fields named `password`/`token`/`api_key`/…, and bearer tokens, API
keys and JWTs by shape). That redaction is **incomplete by construction** — it
will not catch a secret in a field called `note`. It reduces harm; it does not
make capture safe. Turn it on deliberately.

**Outbound HTTP calls** are a separate opt-in again, because they instrument a
different thing — your HTTP client, not your MCP server:

```python
from mcpobs import instrument_httpx
instrument_httpx()
```

This records, on the downstream span: the **request body**, and request and
response headers filtered to an **allow-list** (`content-type`, `user-agent`,
`traceparent`, …). `authorization`, `cookie` and friends are never read at all,
rather than read and then scrubbed — so a header nobody anticipated is excluded
by default rather than captured by default. Bodies go through the same
truncation and redaction as tool payloads, with the same incompleteness.

There is **no response body**, and that is a limitation rather than a setting.
The OpenTelemetry instrumentation wraps the HTTP *transport*, so its span ends
when the transport returns — and httpx reads the response body after that.
Capturing one would mean buffering every response inside the hook, turning any
streaming download in your process into a full in-memory read to satisfy our
telemetry. The console says so on the span instead of showing a blank
(`docs/decisions.md` D67–D69).

It annotates the span the SDK already opened — it does not wrap the protocol or
create spans — and **captures no tool input or output**. The distinguishing text
is SDK-generated boilerplate, read in the customer's process and reduced to a
single enum before anything leaves it. That is what keeps error intelligence a
core feature rather than one gated behind payload capture (V2 §15).

Servers without the helper still work; they report the coarse `tool_error`, and
`failure_kind_source` records which rows came from where so the two data
qualities never silently mix.

`tests/test_sdk_contract.py` is the tripwire: it drives a real server in-process
and fails the build if an SDK release changes the message formats the classifier
depends on.

## Known gaps

See [`docs/deferred.md`](docs/deferred.md) for the full register with the trigger
that forces each one back.

The one worth knowing before you read a dashboard: **`otlp.spans.raw` is not
actually keyed by tenant** (DF-19). ADR-004 says it is; measurement says every
message carries a null key, because the Collector's Kafka exporter cannot key
traces by a resource attribute. Partitioning is therefore by batch, not by
tenant, so there is no per-tenant ordering and no whale isolation yet.

(The older note here said idempotency was untestable locally and had to be
checked in staging. That stopped being true when the local stack gained embedded
Keeper: `ReplicatedMergeTree` runs here, and assertion B11 exercises
`insert_deduplication_token` for real.)

## Layout

```
demo_server/   MCP SDK v2 server + OTel bootstrap + scenarios
mcpobs/        the customer-side helper (failure taxonomy, payload capture)
control/       Postgres control plane: orgs, users, invites, projects, keys
ingest/        authenticating gateway -- resolves the key, stamps the tenant
collector/     OTel Collector gateway config (otlp in, kafka out)
normalizer/    Kafka consumer -> MCP field extraction -> ClickHouse
archiver/      Kafka consumer -> raw OTLP to object storage
query/         read API + console
kafka/         explicit topic creation -- never auto-create
scripts/       admin CLI, verification, and the T3 attribute capture
```
