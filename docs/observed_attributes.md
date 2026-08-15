# Observed attribute report (T3)

> **GENERATED FILE — do not hand-edit.** Regenerate with `python scripts/dump_observed_attrs.py`.

This report is the source of truth for what the MCP Python SDK actually emits. Where it disagrees with the Day-1 engineering document, **this report wins** (Day-1 doc D10).

Captured: 2026-08-15 16:38 UTC

## Resolved versions

| Package | Version |
| --- | --- |
| `mcp` | `2.0.0` |
| `opentelemetry-api` | `1.44.0` |
| `opentelemetry-sdk` | `1.44.0` |
| `opentelemetry-proto` | `1.44.0` |
| `opentelemetry-exporter-otlp-proto-http` | `1.44.0` |
| `opentelemetry-instrumentation-httpx` | `0.65b0` |
| `httpx` | `0.28.1` |
| `confluent-kafka` | `2.15.0` |
| `clickhouse-connect` | `1.7.1` |

## Clock resolution

| Property | Value |
| --- | --- |
| Platform | `Windows 10` |
| `time.time_ns()` smallest tick | `0.752 ms` |
| `monotonic` resolution | `15.625 ms` |
| `perf_counter` resolution | `0.100 us` |
| Consequence | Spans shorter than ~0.75 ms record as `duration_ns = 0` |

> **Latency percentiles for fast tools are not trustworthy on this platform.**
> OpenTelemetry timestamps spans with `time.time_ns()`, so the clock tick above is
> the floor on any measurable duration. Linux `clock_gettime` is nanosecond-grade,
> so production servers are very likely unaffected -- but a customer running an MCP
> server on Windows would see the same flattening, and our own local numbers must
> not be read as real latency.

## Span inventory

| Transport | Span name | Kind | Status | error.type | Events |
| --- | --- | --- | --- | --- | --- |
| `stdio` | `server/discover` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call echo_fast` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call fetch_status` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call fetch_status` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call soft_fail` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `stdio` | `tools/call explode` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `stdio` | `tools/call no_such_tool` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `stdio` | `tools/call echo_fast` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `stdio` | `tools/call confirm_deploy` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call confirm_deploy` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call submit_order` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call query_orders` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call summarize` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `prompts/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `prompts/get triage_error` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `prompts/get summarize_incident` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `resources/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `server/discover` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call echo_fast` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call fetch_status` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call fetch_status` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call soft_fail` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `http` | `tools/call explode` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `http` | `tools/call no_such_tool` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call echo_fast` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call confirm_deploy` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call confirm_deploy` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call submit_order` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call query_orders` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call summarize` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `prompts/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `prompts/get triage_error` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `prompts/get summarize_incident` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `resources/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |

## Attributes emitted on MCP spans

| Attribute | Python type | Spans | Example |
| --- | --- | --- | --- |
| `error.type` | `str` | 8/50 | `tool_error` |
| `gen_ai.operation.name` | `str` | 24/50 | `execute_tool` |
| `gen_ai.prompt.name` | `str` | 4/50 | `triage_error` |
| `gen_ai.tool.call.arguments` | `str` | 50/50 | `{
  "jsonrpc": "2.0",
  "id": 1,
  "method":` |
| `gen_ai.tool.call.result` | `str` | 50/50 | `{
  "jsonrpc": "2.0",
  "id": 1,
  "result":` |
| `gen_ai.tool.name` | `str` | 24/50 | `echo_fast` |
| `jsonrpc.request.id` | `str` | 50/50 | `1` |
| `mcp.method.name` | `str` | 50/50 | `server/discover` |
| `mcp.protocol.version` | `str` | 50/50 | `2026-07-28` |
| `mcp.resource.uri` | `str` | 4/50 | `config://limits` |
| `mcpobs.client.name` | `str` | 50/50 | `mcp` |
| `mcpobs.client.version` | `str` | 50/50 | `0.1.0` |
| `mcpobs.failure.detail` | `str` | 8/50 | `upstream rejected the request` |
| `mcpobs.failure.kind` | `str` | 8/50 | `tool_error` |
| `mcpobs.failure.kind.version` | `int` | 8/50 | `2` |
| `mcpobs.mrtr.state.in` | `str` | 2/50 | `e973cab711cdcb3d` |
| `mcpobs.mrtr.state.out` | `str` | 2/50 | `e973cab711cdcb3d` |
| `mcpobs.request.size` | `int` | 50/50 | `411` |
| `mcpobs.response.size` | `int` | 50/50 | `540` |
| `mcpobs.result.type` | `str` | 2/50 | `input_required` |

## Expected by the Day-1 doc but NOT emitted

These appear in Day-1 doc §4.2. The SDK does not emit them; the corresponding columns will be NULL.

- `rpc.response.status_code`
- `mcp.session.id`
- `network.transport`
- `network.protocol.name`
- `network.protocol.version`
- `jsonrpc.protocol.version`
- `client.address`
- `client.port`
- `server.address`
- `server.port`

## Failure taxonomy reachability

Distinct `error.type` values observed across all failure scenarios: `tool_error`

Failing spans: 8 · spans carrying `rpc.response.status_code`: 0 · spans carrying exception events: 0

> ### FINDING: the failure taxonomy is not reachable from span attributes
>
> Four deliberately different failure modes were exercised — a tool returning
> `isError=True`, a handler raising `RuntimeError`, a call to an unknown tool, and a
> schema-violating argument. **All four produce an identical span**: status `ERROR`,
> `error.type="tool_error"`, no `rpc.response.status_code`, no exception event.
>
> Cause: `MCPServer`'s tool handler catches everything and converts it to a
> `CallToolResult(isError=True)` *before* `OpenTelemetryMiddleware` observes the
> result, so the middleware's `except Exception` and `except MCPError` branches are
> unreachable for anything routed through `tools/call`.
>
> **Consequences.** Day-1 doc §9.5 lists five categories; only `ok` and `tool_error`
> are reachable. Assertion A3 cannot pass as originally written. V2 §25's launch
> checklist item *"MCP isError and thrown exception are distinguishable"* is **not
> achievable from span attributes** with the stock SDK.
>
> **The product angle.** The differentiator V2 §6.3 sells is an MCP failure taxonomy.
> If every failure looks the same on the span, that taxonomy needs a source beyond
> attributes — result-content inspection (a payload feature, opt-in per V2 §15) or an
> upstream SDK change. This is the single most important thing Day 1 found and it
> belongs in the Day-2 agenda, not in a backlog.

## Transport comparison

- Attributes on `stdio` only: _none_
- Attributes on `http` only: _none_
- Shared: 20 attributes

**The two transports emit identical attribute sets.** Transport is therefore not observable from span attributes alone on Day 1 — `network.transport` is not emitted, so the normalizer cannot distinguish stdio from streamable HTTP.

## Downstream child spans (A4)

| Transport | Child span | Method | Status |
| --- | --- | --- | --- |
| `stdio` | `GET` | `GET` | `200` |
| `stdio` | `GET` | `GET` | `500` |
| `stdio` | `POST` | `POST` | `201` |
| `stdio` | `SELECT` | `` | `` |
| `stdio` | `chat gpt-4o-mini` | `` | `` |
| `http` | `GET` | `GET` | `200` |
| `http` | `GET` | `GET` | `500` |
| `http` | `POST` | `POST` | `201` |
| `http` | `SELECT` | `` | `` |
| `http` | `chat gpt-4o-mini` | `` | `` |

