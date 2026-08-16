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

## Documentation

Two trees, and the split is load-bearing:

- **`docs-public/`** — the customer-facing site. This is MkDocs' `docs_dir`.
  `make docs` serves it with live reload; `make docs-build` builds it strictly.
- **`docs/`** — internal engineering documents. MkDocs never reads this
  directory, so nothing here can be published by accident.

Put a new page on the side it belongs to. `docs/alpha-readiness.md` and
`docs/deferred.md` enumerate unpatched weaknesses and known gaps, so publishing
them would hand an attacker a checklist -- `tests/test_docs.py` fails the build
if an internal file, or internal text, appears in the public tree.

## Design

- [`docs/Architecture.md`](docs/Architecture.md) — system of record, ADR-001…010
- [`docs/decisions.md`](docs/decisions.md) — decision log, including the Day-1 findings
- [`docs/observed_attributes.md`](docs/observed_attributes.md) — **generated**, the span contract
- [`docs/deferred.md`](docs/deferred.md) — everything knowingly postponed, and what forces it back
- [`docs/alpha-readiness.md`](docs/alpha-readiness.md) — current release verdict, blockers, and alpha gate

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

## Long-running tools and subscriptions

A span is only exported when it **ends**, so a tool that runs for an hour tells
you nothing for an hour. Two opt-ins fix that:

```python
from mcpobs import instrument_progress, ObservedSubscriptionBus

instrument_progress()                                    # every report_progress() call
bus = ObservedSubscriptionBus(InMemorySubscriptionBus())  # every published event
mcp = MCPServer("srv", subscriptions=bus)
```

Each `ctx.report_progress()` becomes a child span of the running call carrying
value, total, percentage and message — exported immediately, so you can see a
job's position **while it is still running**. A 10-second call produces its
progress trail in the console before the call itself has landed.

Progress emission is capped at 200 spans per operation; hitting the cap emits a
marker rather than going quiet, because a stream that just stops looks like the
tool stopping.

> Span *events* would not work here. They ride on their span and are exported
> when it ends — which is exactly the moment that is too late.

## Operator console

A separate dashboard at `/admin`, for people who run the platform rather than
people who use it:

```bash
python scripts/admin.py key --org acme --scopes admin
open http://localhost:8080/admin
```

- **Tenants** — every org, joined to its telemetry volume, with quota headroom
  and one-click hard-quota. Flags tenants over a soft threshold, tenants that
  never sent a span, and telemetry with no organisation behind it.
- **Pipeline** — freshness, dead letters by reason, and which normalization
  versions are live (more than one means a deploy or replay is in flight).
- **API keys** — prefixes, last use, and revoke.
- **Invites** — invite-only, so this *is* the signup queue.
- **Audit** — every operator action, and every refused attempt.

Quota changes and key revocations are written **in the same transaction as the
action**, so an action and its record land together or not at all. That is what
lets a revoke be unconditional: an audit log in a separate store would force a
choice between blocking a revoke when the log is down — leaving a leaked
credential live — and doing it anyway, which loses the record of the one action
most worth having. CLI actions are audited too; `scripts/admin.py` needs
database access, which makes it the *more* privileged path.

Reads are **not** audited. The console refreshes every 30 seconds, so logging
list views would bury the mutations under thousands of rows meaning "a tab was
open". "Who looked at customer X" is a real question and this does not answer
it.

`admin` is a **third scope**, not a flag on a read key: every other scope is
bounded by one org and this one is not. A read key is refused here, and no HTTP
endpoint can grant `admin` — only the CLI, which needs database access.

It is a separate page with a separate stored credential, not a mode of the
customer console. A toggle would be one rendering bug away from showing a
customer everyone else's tenants.

## Quotas

Ingest is metered per tenant, in **spans** — a request can carry one span or ten
thousand, so metering requests would let the same volume through in a hundredth
of the calls.

```bash
python scripts/admin.py quota --org acme                      # show
python scripts/admin.py quota --org acme --plan pro           # change plan
python scripts/admin.py quota --org acme --per-minute 100000  # override
python scripts/admin.py quota --org acme --clear              # back to the plan
```

| Plan | Spans/minute | Spans/day |
| --- | --- | --- |
| `trial` | 2,000 | 200,000 |
| `pro` | 50,000 | 20,000,000 |
| `enterprise` | unlimited | unlimited |

At **80%** of either limit, spans are stamped with
`mcpobs.quota.soft_exceeded` — visible in your own console, on exactly the data
that was at risk, rather than only in our logs.

Over the limit, the gateway returns **429** with `Retry-After` and the counters
behind the decision:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 51
X-Quota-Used-Minute: 6
X-Quota-Limit-Minute: 5
```

429 rather than 403 on purpose: an OTLP exporter **retries** a 429 and abandons
a 403, so a rate limit sent as 403 would make your client give up on data it
could have delivered a minute later.

If our counter store is unavailable, requests are **allowed**. Refusing your
telemetry because our bookkeeping broke would protect the wrong party.

## Authorization (401 / 403)

If your server is an OAuth 2.1 resource server, wrap its ASGI app:

```python
from mcpobs import instrument_asgi

app = instrument_asgi(mcp.streamable_http_app())
uvicorn.run(app, host="0.0.0.0", port=8000)
```

Authorization is a **transport-level** concern in MCP, so a client without a
valid token is refused before any method runs and produces no MCP span at all.
Without this wrapper, "my clients cannot connect" is not an error in the
console -- it is simply absent.

`401` and `403` are shown as their own categories and are deliberately **not**
counted as failures. The spec's flow opens with an unauthenticated request
answered by a 401 -- that is how a client discovers where to authenticate -- and
`403 insufficient_scope` drives the routine step-up flow. A *persistent* 401
rate is the real signal, and you can see it because these are separate
categories rather than errors.

> Do not rely on `StarletteInstrumentor().instrument()` for this. It patches the
> `Starlette` class, and the MCP SDK binds that name at import time, so whether
> it works depends on your import order -- and it reports success either way.

## Redis, Postgres, and everything else your tools call

Your tool calls a database or a cache; we show it as a child span under the tool
call, with its own timing, so a slow tool says *where* the time went instead of
looking like unexplained server time.

We do not patch your clients for you. Adding observability to an MCP server
should not silently monkey-patch your database driver, so downstream
instrumentation is something you turn on. There are two ways, and the first is
better when you can use it.

**1. The OpenTelemetry agent (preferred).** Nothing from us involved:

```bash
pip install opentelemetry-instrumentation-redis opentelemetry-instrumentation-psycopg
opentelemetry-instrument python -m your_server
```

It picks up any instrumentation package the day you install it.

**2. One call, in-process.** For servers you do not launch yourself -- which is
most MCP servers, because the *client* spawns them over stdio and you do not own
that command line:

```python
from mcpobs import instrument, instrument_downstream

instrument(mcp)
report = instrument_downstream()      # {'httpx': 'instrumented', 'redis': 'instrumented', ...}
```

It discovers whatever you have installed through OpenTelemetry's own entry-point
group -- the same mechanism the agent uses -- so `pip install
opentelemetry-instrumentation-redis` is the entire integration. It returns a
report rather than nothing, because a call that patches an unknown set of
libraries should tell you what it touched. It never raises: one package with a
version conflict is reported and skipped, never allowed to stop your server
booting. `instrument_downstream(exclude=("sqlite3",))` opts individual ones out.

What you get per span kind:

| Called | Shown as | Fields |
| --- | --- | --- |
| Redis, Postgres, MySQL, Mongo, SQLite | `db` | system, operation, collection/table, statement (**redacted before storage**) |
| HTTP (httpx, requests, aiohttp) | `http` | method, status, host, URL, and request body/headers with `instrument_httpx()` |
| OpenAI, Anthropic, Bedrock | `llm` | system, model, input/output tokens |
| Kafka, RabbitMQ, SQS | `messaging` | stored and rendered generically |

SQL statements are redacted at normalize time, never at render: a secret that
reaches the table cannot be recalled (D62).

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
