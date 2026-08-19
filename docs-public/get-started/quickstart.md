# Quickstart

Two separate environments, and it is worth knowing which is which before you
start. **The SDK is a package you install from PyPI** into whatever environment
your MCP server already runs in. **The backend is a stack you run from a clone.**
Mixing them up is the most common way this page goes wrong.

## 1. Run the stack

From a clone of the repository, in its own virtualenv:

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
make up        # ClickHouse, Kafka, Collector, Postgres, MinIO, ingest,
               # normalizer, archiver, query API
make devkeys   # provision a local org and API keys
```

`make devkeys` writes a read key and an ingest key to a local, git-ignored file.
Access is invite-only: there is no self-service signup, and no endpoint that
mints a key.

!!! note "These requirements are the backend's, not the SDK's"

    `requirements-dev.txt` builds the stack above. It does **not** install
    `mcpobs` into your server — that is the next step, and it comes from PyPI.

## 2. Install the SDK in your server

In your MCP server's own environment, not the one you just made:

```bash
pip install mcpobs
```

Python 3.11 or newer. If your server runs over **stdio**, install
`"mcpobs[sessions]"` instead — see the warning in step 4.

## 3. Instrument your server

```python
from mcpobs import instrument

instrument(mcp)  # (1)!
```

1.  `mcp` is your `MCPServer` instance. This adds a middleware; it does not
    wrap or replace the protocol.

!!! note "Something in your process must still export spans"

    `mcpobs` annotates and classifies the spans your MCP SDK opens; it does not
    create a `TracerProvider` and, outside the session-token path, ships no
    exporter. If you have no OTel export configured yet, add one — otherwise the
    next step points at a collector that never receives anything. See
    [Python SDK](../integrate/python-sdk.md#this-package-does-not-configure-opentelemetry-for-you).

## 4. Point it at the collector

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<your ingest key>"
```

!!! warning "If your server runs over stdio, stop here"

    The client launches it on your **user's** machine, so a key in that config
    is a permanent credential on someone else's laptop. Use
    [session tokens](../operate/session-tokens.md) instead — and install
    `"mcpobs[sessions]"` rather than plain `mcpobs`, or no token is ever obtained
    and the stderr diagnostic will blame your endpoint for a missing dependency.

!!! note "Ingest keys and read keys are different"

    An ingest key lives in your server process and deployment config. A read key
    lives in a browser. One being compromised must not imply the other, so the
    console will refuse an ingest key and the ingest endpoint will refuse a read
    key. See [API keys and access](../operate/keys.md).

## 5. Open the console

```
http://localhost:8080
```

Sign in with the **read** key. Traces appear within seconds — end-to-end
freshness is typically under a minute, and the console shows you the current
figure in the sidebar so you never have to guess whether you are looking at
current data.

## Next

- [Your first trace](first-trace.md) — how to read what you are now collecting.
- [Databases, caches and APIs](../integrate/downstream.md) — make your
  Postgres, Redis and HTTP calls visible too.
