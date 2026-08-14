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

| D17 | 2026-08-15 | Failure classification runs in the customer's process via an opt-in middleware that annotates the SDK's existing span. **No tool content is captured, transmitted or stored.** | Resolves D13 without dragging the taxonomy behind payload capture and its redaction/retention gates (V2 §15). Error intelligence stays a core feature. |
| D18 | 2026-08-15 | The classifier is a bridge, not an endpoint. Pursue an upstream SDK change that exposes the failure kind directly. | We match SDK-internal message strings; `tests/test_sdk_contract.py` fails the build if they move, but the durable fix is upstream. |
| D19 | 2026-08-15 | `protocol_error` **is** reachable — correcting D13. | `_handle_call_tool` re-raises `MCPError` rather than converting it (`mcpserver/server.py:422`). Day 1 saw no such error only because no demo tool raised one. |
| D20 | 2026-08-15 | `resultType` is read off the sealed wire form by the same middleware, **reversing D11**. | Middleware receives the serialized dict, which carries `resultType`. MRTR interim results are therefore observable, and `pending_input` is checked before any error branch so it can never enter an error rate. |
| D21 | 2026-08-15 | Record `failure_kind_source` (`helper` \| `span`) on every row. | Two different data qualities must not silently mix. A dashboard can say "12% of your servers are not running the helper" instead of quietly under-reporting. |

---

## Finding D13 — RESOLVED 2026-08-15 by D17

> **Status: resolved.** The finding below stands as written — the taxonomy is
> genuinely not reachable *from span attributes*. What changed is that it does
> not have to be: the distinguishing text is SDK-generated boilerplate present
> in the result, and a middleware can classify it in the customer's process and
> annotate the span the SDK already opened.
>
> Verified end to end: `server_exception`, `tool_error`, `unknown_tool` and
> `invalid_arguments` are now distinct on real telemetry, with zero tool content
> stored (assertions A3b, B1–B3). The V2 §25 launch gate is met.

## Original finding — the failure taxonomy is not reachable from span attributes

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

| D23 | 2026-08-15 | Replay is proven, not assumed. A deliberate normalizer bug was shipped, detected, fixed, and the data corrected by reprocessing from Kafka with a fresh consumer group — no producer resent anything. | ADR-007 is the justification for the entire Kafka tier and had never been exercised. `scripts/replay.py` is the operational tool. |
| D24 | 2026-08-15 | Reads that must be correct across a normalizer change MUST resolve `argMax(..., normalization_version)` per `(trace_id, span_id)`. | Proven necessary: a naive query over replayed data mixes buggy v2 rows with corrected v3 rows and reported 6 tools where there are 5. |
| D25 | 2026-08-15 | Any `trace_locator` read must dedupe explicitly (`LIMIT 1 BY trace_id`). | ReplacingMergeTree deduplicates only when parts merge, which is asynchronous and may never have happened. A replay re-inserts every trace_id, so a naive lookup returns duplicates. Day 3's API depends on this. |
| D26 | 2026-08-15 | "Is recent data healthy?" must be asked in **event time**, never ingest time. | Replay deliberately decouples the two clocks: it re-ingests old spans *now*, so `ingested_at` cannot isolate a current run. Found when a helper-coverage assertion failed against replayed history. |
