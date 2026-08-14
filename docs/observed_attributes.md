# Observed attribute report (T3)

> **GENERATED FILE — do not hand-edit.** Regenerate with `python scripts/dump_observed_attrs.py`.

This report is the source of truth for what the MCP Python SDK actually emits. Where it disagrees with the Day-1 engineering document, **this report wins** (Day-1 doc D10).

Captured: 2026-08-14 16:33 UTC

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

## Attributes emitted on MCP spans

| Attribute | Python type | Spans | Example |
| --- | --- | --- | --- |
| `error.type` | `str` | 8/23 | `tool_error` |
| `gen_ai.operation.name` | `str` | 14/23 | `execute_tool` |
| `gen_ai.tool.name` | `str` | 14/23 | `echo_fast` |
| `jsonrpc.request.id` | `str` | 23/23 | `1` |
| `mcp.method.name` | `str` | 23/23 | `server/discover` |
| `mcp.protocol.version` | `str` | 23/23 | `2026-07-28` |

## Expected by the Day-1 doc but NOT emitted

These appear in Day-1 doc §4.2. The SDK does not emit them; the corresponding columns will be NULL.

- `gen_ai.prompt.name`
- `mcp.resource.uri`
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
- `gen_ai.tool.call.arguments`
- `gen_ai.tool.call.result`

## Transport comparison

- Attributes on `stdio` only: _none_
- Attributes on `http` only: _none_
- Shared: 6 attributes

**The two transports emit identical attribute sets.** Transport is therefore not observable from span attributes alone on Day 1 — `network.transport` is not emitted, so the normalizer cannot distinguish stdio from streamable HTTP.

## Downstream child spans (A4)

| Transport | Child span | Method | Status |
| --- | --- | --- | --- |
| `stdio` | `GET` | `GET` | `200` |
| `stdio` | `GET` | `GET` | `500` |
| `http` | `GET` | `GET` | `200` |
| `http` | `GET` | `GET` | `500` |

