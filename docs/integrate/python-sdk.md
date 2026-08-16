# Python SDK

## Install

```bash
pip install mcpobs
```

## Instrument your server

```python
from mcpobs import instrument

instrument(mcp)
```

That is the whole integration. It installs a middleware on your `MCPServer`
that classifies failures and annotates the spans the MCP SDK already opens. It
does not wrap the protocol, and it does not create spans of its own.

## What you get immediately

| | |
| --- | --- |
| Every MCP method | `tools/call`, `tools/list`, `prompts/get`, `resources/read`, and anything else the protocol adds |
| Classified failures | Five distinct categories rather than one error rate |
| Multi-round-trip calls | Elicitation and sampling rounds tracked without corrupting your error rate |
| Progress and cancellation | Long-running calls observable *while* they run |

## Servers without the helper still work

If you export OTel spans but do not install `mcpobs`, everything still arrives.
Failures report the coarse `tool_error` rather than a precise category, and
every row records *which* source classified it — so the two data qualities never
silently mix. The console shows the share of precisely-classified failures, and
tells you when it is below 100%.

## Optional pieces

Each is a separate call because each instruments a different thing:

```python
from mcpobs import instrument_downstream, instrument_httpx, instrument_asgi

instrument_downstream()   # your database, cache and HTTP clients
instrument_httpx()        # request bodies and allow-listed headers
instrument_asgi(app)      # transport-level 401/403, before any MCP method runs
```

See [Databases, caches and APIs](downstream.md) and
[Payloads and privacy](payloads.md).

Full signatures in the [SDK reference](../reference/sdk.md).
