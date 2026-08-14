# mcp-observability

MCP-native observability. Day-1 vertical slice: a Python MCP SDK v2 server's
spans travel **OTel → Collector → Kafka → stateless normalizer → ClickHouse** and
come back out as a queryable, MCP-aware trace.

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
make up        # clickhouse + kafka + collector + normalizer
make demo      # run the tool scenarios over stdio and streamable HTTP
make verify    # A1-A8 acceptance assertions
```

`make attrs` regenerates `docs/observed_attributes.md` — the observed span
contract, which outranks every document where they disagree.

## Design

- [`docs/../../MCP_Observability_Engineering/Architecture.md`](../MCP_Observability_Engineering/Architecture.md) — system of record, ADR-001…010
- [`docs/decisions.md`](docs/decisions.md) — decision log, including the Day-1 findings
- [`docs/observed_attributes.md`](docs/observed_attributes.md) — **generated**, the span contract

## Known gaps

**The failure taxonomy has one failure bucket.** `MCPServer` converts every tool
failure — raised exception, `isError` result, unknown tool, schema violation —
into `error.type="tool_error"` before the OTel middleware sees it. See
`docs/decisions.md` D13. `make verify` reports this as a WARN, not a pass.

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
