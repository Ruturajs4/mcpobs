# Observed attribute report (T3)

> **GENERATED FILE — do not hand-edit.** Regenerate with `python scripts/dump_observed_attrs.py`.

This report is the source of truth for what the MCP Python SDK actually emits. Where it disagrees with the Day-1 engineering document, **this report wins** (Day-1 doc D10).

Captured: 2026-08-16 15:40 UTC

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
| `time.time_ns()` smallest tick | `1.505 ms` |
| `monotonic` resolution | `15.625 ms` |
| `perf_counter` resolution | `0.100 us` |
| Consequence | Spans shorter than ~1.50 ms record as `duration_ns = 0` |

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
| `stdio` | `tools/call enqueue_job` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call query_orders` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call summarize` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call customer_profile` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call customer_profile` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call order_history` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call check_stock` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call cache_warm` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call partner_health` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call place_order` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call place_order` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `stdio` | `prompts/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `prompts/get triage_error` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `prompts/get summarize_incident` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `resources/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call publish_change` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `subscriptions/listen` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `notifications/cancelled` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call slow_export` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `tools/call slow_export` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `stdio` | `notifications/cancelled` | `SpanKind.SERVER` | `UNSET` | — | 0 |
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
| `http` | `tools/call enqueue_job` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call query_orders` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call summarize` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call customer_profile` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call customer_profile` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call order_history` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call check_stock` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call cache_warm` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call partner_health` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call place_order` | `SpanKind.SERVER` | `ERROR` | `tool_error` | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call place_order` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `prompts/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `prompts/get triage_error` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `prompts/get summarize_incident` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `resources/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `resources/read` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call publish_change` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `subscriptions/listen` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call slow_export` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/list` | `SpanKind.SERVER` | `UNSET` | — | 0 |
| `http` | `tools/call slow_export` | `SpanKind.SERVER` | `UNSET` | — | 0 |

## Attributes emitted on MCP spans

| Attribute | Python type | Spans | Example |
| --- | --- | --- | --- |
| `error.type` | `str` | 10/89 | `tool_error` |
| `gen_ai.operation.name` | `str` | 48/89 | `execute_tool` |
| `gen_ai.prompt.name` | `str` | 4/89 | `triage_error` |
| `gen_ai.tool.call.arguments` | `str` | 85/89 | `{
  "jsonrpc": "2.0",
  "id": 1,
  "method":` |
| `gen_ai.tool.call.result` | `str` | 83/89 | `{
  "jsonrpc": "2.0",
  "id": 1,
  "result":` |
| `gen_ai.tool.name` | `str` | 48/89 | `echo_fast` |
| `jsonrpc.request.id` | `str` | 87/89 | `1` |
| `mcp.method.name` | `str` | 89/89 | `server/discover` |
| `mcp.protocol.version` | `str` | 89/89 | `2026-07-28` |
| `mcp.resource.uri` | `str` | 4/89 | `config://limits` |
| `mcpobs.cancelled` | `bool` | 4/89 | `True` |
| `mcpobs.client.name` | `str` | 83/89 | `mcp` |
| `mcpobs.client.version` | `str` | 83/89 | `0.1.0` |
| `mcpobs.failure.detail` | `str` | 10/89 | `upstream rejected the request` |
| `mcpobs.failure.kind` | `str` | 10/89 | `tool_error` |
| `mcpobs.failure.kind.version` | `int` | 10/89 | `2` |
| `mcpobs.mrtr.state.in` | `str` | 2/89 | `e973cab711cdcb3d` |
| `mcpobs.mrtr.state.out` | `str` | 2/89 | `e973cab711cdcb3d` |
| `mcpobs.request.size` | `int` | 85/89 | `411` |
| `mcpobs.response.size` | `int` | 83/89 | `540` |
| `mcpobs.result.type` | `str` | 2/89 | `input_required` |
| `network.transport` | `str` | 89/89 | `stdio` |

## Expected by the Day-1 doc but NOT emitted

These appear in Day-1 doc §4.2. The SDK does not emit them; the corresponding columns will be NULL.

- `rpc.response.status_code`
- `mcp.session.id`
- `network.protocol.name`
- `network.protocol.version`
- `jsonrpc.protocol.version`
- `client.address`
- `client.port`
- `server.address`
- `server.port`

## Failure taxonomy reachability

Distinct `error.type` values observed across all failure scenarios: `tool_error`

Failing spans: 10 · spans carrying `rpc.response.status_code`: 0 · spans carrying exception events: 0

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
- Shared: 22 attributes

**The transport is recorded.** `network.transport` is `stdio` on stdio spans and `streamable-http` on http spans, so the two populations are distinguishable in storage. The MCP SDK does not emit this attribute; `mcpobs` derives it from `server.run(transport)`, from `instrument_asgi()`, or from an explicit `instrument(server, transport=...)`.

## Downstream child spans (A4)

| Transport | Child span | Method | Status |
| --- | --- | --- | --- |
| `stdio` | `GET` | `GET` | `200` |
| `stdio` | `GET` | `GET` | `500` |
| `stdio` | `POST` | `POST` | `201` |
| `stdio` | `mcpobs.demo.jobs send` | `` | `` |
| `stdio` | `SELECT` | `` | `` |
| `stdio` | `chat gpt-4o-mini` | `` | `` |
| `stdio` | `GET` | `` | `` |
| `stdio` | `GET` | `` | `` |
| `stdio` | `SELECT` | `` | `` |
| `stdio` | `SELECT` | `` | `` |
| `stdio` | `SELECT` | `` | `` |
| `stdio` | `SETEX` | `` | `` |
| `stdio` | `GET` | `` | `` |
| `stdio` | `SETEX` | `` | `` |
| `stdio` | `GET` | `` | `` |
| `stdio` | `SETEX` | `` | `` |
| `stdio` | `GET` | `` | `` |
| `stdio` | `SETEX` | `` | `` |
| `stdio` | `GET` | `` | `` |
| `stdio` | `SETEX` | `` | `` |
| `stdio` | `GET` | `` | `` |
| `stdio` | `GET` | `GET` | `200` |
| `stdio` | `GET` | `GET` | `200` |
| `stdio` | `GET` | `GET` | `200` |
| `stdio` | `SELECT` | `` | `` |
| `stdio` | `UPDATE` | `` | `` |
| `stdio` | `POST` | `POST` | `200` |
| `stdio` | `POST` | `POST` | `200` |
| `stdio` | `POST` | `POST` | `200` |
| `stdio` | `INSERT` | `` | `` |
| `stdio` | `DEL` | `` | `` |
| `stdio` | `SELECT` | `` | `` |
| `stdio` | `UPDATE` | `` | `` |
| `stdio` | `POST` | `POST` | `200` |
| `stdio` | `POST` | `POST` | `503` |
| `stdio` | `mcp.subscription.event` | `` | `` |
| `stdio` | `mcp.progress` | `` | `` |
| `stdio` | `mcp.progress` | `` | `` |
| `stdio` | `mcp.progress` | `` | `` |
| `stdio` | `mcp.progress` | `` | `` |
| `http` | `GET` | `GET` | `200` |
| `http` | `GET` | `GET` | `500` |
| `http` | `POST` | `POST` | `201` |
| `http` | `mcpobs.demo.jobs send` | `` | `` |
| `http` | `SELECT` | `` | `` |
| `http` | `chat gpt-4o-mini` | `` | `` |
| `http` | `GET` | `` | `` |
| `http` | `GET` | `` | `` |
| `http` | `SELECT` | `` | `` |
| `http` | `SELECT` | `` | `` |
| `http` | `SELECT` | `` | `` |
| `http` | `SETEX` | `` | `` |
| `http` | `GET` | `` | `` |
| `http` | `SETEX` | `` | `` |
| `http` | `GET` | `` | `` |
| `http` | `SETEX` | `` | `` |
| `http` | `GET` | `` | `` |
| `http` | `SETEX` | `` | `` |
| `http` | `GET` | `` | `` |
| `http` | `SETEX` | `` | `` |
| `http` | `GET` | `` | `` |
| `http` | `GET` | `GET` | `200` |
| `http` | `GET` | `GET` | `200` |
| `http` | `GET` | `GET` | `200` |
| `http` | `SELECT` | `` | `` |
| `http` | `UPDATE` | `` | `` |
| `http` | `POST` | `POST` | `200` |
| `http` | `POST` | `POST` | `503` |
| `http` | `SELECT` | `` | `` |
| `http` | `mcp.subscription.event` | `` | `` |
| `http` | `mcp.progress` | `` | `` |
| `http` | `mcp.progress` | `` | `` |
| `http` | `mcp.progress` | `` | `` |
| `http` | `mcp.progress` | `` | `` |

