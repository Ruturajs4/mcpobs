# Python SDK

## Install

```bash
pip install mcpobs
```

Python 3.11 or newer. The package is on PyPI as
[`mcpobs`](https://pypi.org/project/mcpobs/) — Apache-2.0, fully typed, and
published from a tagged CI run so you can match a release to the commit that
built it.

=== "Your own infrastructure (HTTP)"

    ```bash
    pip install mcpobs
    ```

    Pair it with whatever exports your OTel spans today.

=== "Client-launched server (stdio)"

    ```bash
    pip install "mcpobs[sessions]"
    ```

    The extra adds an OTLP HTTP exporter and `httpx`, which
    [session tokens](../operate/session-tokens.md) need.

!!! warning "The extra is not optional if you use session tokens"

    Measured on a clean install of 0.1.0: with plain `pip install mcpobs` and a
    `session_endpoint` configured, no token is ever obtained, so nothing is
    exported. Your server runs fine and reports nothing.

    It does tell you, on **stderr**:

    ```
    [mcpobs] session endpoint unreachable (No module named 'httpx');
             telemetry paused, retrying
    ```

    Read the parenthesis, not the first three words. The message says
    *unreachable*, which sends most people to check their endpoint and their
    network — but the cause here is a missing dependency, and no amount of
    debugging the endpoint will fix it.

### This package does not configure OpenTelemetry for you

`mcpobs` annotates and classifies the spans your MCP SDK already opens. It does
not create a `TracerProvider`, and — apart from the session-token path — it does
not ship an exporter. **If nothing in your process exports spans, installing
this package alone will not make traces appear.**

That is deliberate rather than an omission. A customer who already runs an OTel
pipeline should not have ours forced into their process, and the alternative is a
library that quietly takes over global tracing state.

So core installs nothing you did not ask for:

| Not installed | Why |
| --- | --- |
| An OTLP exporter | Classifying failures does not require ours. Export through your existing pipeline. |
| `httpx` | Only the session-token path needs an HTTP client. |
| Any instrumentor | `instrument_downstream()` finds them through OpenTelemetry's own entry points, so you install exactly the ones you use — we never pull `psycopg`, `redis` or `pymysql` into your environment on your behalf. |

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
