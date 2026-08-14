# mcp-observability

MCP-native observability. Day-1 vertical slice: a Python MCP SDK v2 server's
spans travel **OTel → Collector → Kafka → stateless normalizer → ClickHouse** and
come back out as a queryable, MCP-aware trace.

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
make check     # unit tests + ruff + mypy (no stack required)
make up        # clickhouse + kafka + collector + normalizer
make demo      # run the tool scenarios over stdio and streamable HTTP
make verify    # A1-A9 acceptance assertions
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

**Idempotency is untestable locally.** `insert_deduplication_token` requires
`ReplicatedMergeTree`; the local schema is plain `MergeTree`, so a replayed batch
*will* duplicate rows here. Test it in staging.

## Layout

```
demo_server/   MCP SDK v2 server + OTel bootstrap + scenarios
normalizer/    Kafka consumer -> MCP field extraction -> ClickHouse
collector/     OTel Collector gateway config (otlp in, kafka out)
kafka/         explicit topic creation -- never auto-create
scripts/       verification and the T3 attribute capture
```
