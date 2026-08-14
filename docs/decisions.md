# Decision log

Structural decisions (D1–D3b) are inherited from `Architecture.md` ADR-001…010,
which remains the system of record. D11+ were taken during Day-1 execution.

| # | Date | Decision | Rationale |
| --- | --- | --- | --- |
| D1 | 2026-08-14 | Collector produces to Kafka directly; no bespoke ingest service. | ADR-003. Never hand-write the most availability-critical tier. |
| D2 | 2026-08-14 | Kafka is in the path from Day 1. | ADR-002. The ack boundary and offset-commit ordering are semantics, not infrastructure. |
| D2b | 2026-08-14 | Apache Kafka, not Redpanda. | ADR-002. Apache 2.0, open-source tiered storage, Connect sinks, managed availability. |
| D3 | 2026-08-14 | Normalizer is stateless; trace assembly happens in ClickHouse. | ADR-005. No stream processor, now or later. |
| D3b | 2026-08-14 | Partition by tenant; never require trace locality or ordering. | ADR-004. Spans arrive out of order regardless. |
| D11 | 2026-08-14 | `result_type` column created but left NULL; `pending_input` dropped from taxonomy v0. | The SDK emits no `resultType` attribute, so MRTR interim results are invisible on the span. Column kept so no migration is needed when it becomes visible. |
| D12 | 2026-08-14 | Capture every scenario over **both** stdio and streamable HTTP. | Transport shapes what a customer runs; we needed to know whether attributes differ. **They do not** — the two transports emit identical attribute sets. |
| D13 | 2026-08-14 | Ship taxonomy v0 with only `ok` and `tool_error` reachable, and say so loudly. | See finding below. Inventing a distinction the telemetry cannot support would be worse than reporting the gap. |
| D14 | 2026-08-14 | `rpc_status_code` is `Nullable(String)`, not `Int32`. | The SDK sets `str(code)`. Day-1 doc §9.3 was wrong. |
| D15 | 2026-08-14 | Demo server calls downstream via plain `httpx`, not the SDK's `httpx2`. | `opentelemetry-instrumentation-httpx` patches `httpx` only; with `httpx2` the downstream child span never appears and A4 would silently pass as vacuous. |

---

## Finding D13 — the failure taxonomy is not reachable from span attributes

**What we tested.** Four deliberately distinct failure modes:

| Scenario | Intent |
| --- | --- |
| `soft_fail` | tool returns `CallToolResult(isError=True)` |
| `explode` | handler raises `RuntimeError` |
| `no_such_tool` | call to an unregistered tool |
| `echo_fast({"message": {...}})` | schema-violating argument |

**What we got.** All four produce an identical span: `status=ERROR`,
`error.type="tool_error"`, no `rpc.response.status_code`, no exception event.

**Cause.** `MCPServer`'s tool handler catches everything and converts it to
`CallToolResult(isError=True)` *before* `OpenTelemetryMiddleware` observes the
result. The middleware's `except MCPError` and `except Exception` branches
(`mcp/server/_otel.py:45-60`) are therefore unreachable for anything routed
through `tools/call`.

**Consequences.**

1. Day-1 doc §9.5 lists five categories; two are reachable.
2. V2 §25's launch checklist item *"MCP isError and thrown exception are
   distinguishable"* is **not achievable from span attributes** with the stock SDK.
3. V2 §6.3 sells an MCP failure taxonomy as a core differentiator. Today that
   taxonomy has one failure bucket.

**Options for Day 2** (none chosen yet):

- Inspect `CallToolResult` content text — distinguishes the cases, but content is
  a payload feature (opt-in, redaction-gated per V2 §15), so the taxonomy would
  inherit payload-capture constraints.
- Contribute an upstream SDK change so the middleware sees the original failure.
- Ship a documented single failure bucket for v1 and revisit.

This is the most important thing Day 1 found. It belongs on the Day-2 agenda,
not in a backlog.
