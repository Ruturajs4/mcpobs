# Quickstart

Two separate environments, and it is worth knowing which is which before you
start. **The SDK is a package you install from PyPI** into whatever environment
your MCP server already runs in. **The backend is a stack you run from a clone.**
Mixing them up is the most common way this page goes wrong.

## 1. Run the stack

From a clone of the repository:

```bash
git clone https://github.com/Ruturajs4/mcpobs.git
cd mcpobs
make up-lite
```

Three containers: ClickHouse, `ingest`, `query`. No Kafka, no database
beyond ClickHouse, and no key to provision — this self-hosted image is
**single-tenant**: any value in `x-api-key` (including none at all) is
accepted, because there is only one tenant to authenticate into. See
[Lite deployment](lite.md) for the full picture, including what that trades
away versus a managed, multi-tenant deployment.

!!! note "Docker only, no local Python needed for this step"

    `make up-lite` builds and runs the containers with `docker compose`. You
    don't need a `.venv` or `requirements-dev.txt` unless you're working on
    this repo's own code.

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
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=anything"
```

This self-hosted image is single-tenant, so `x-api-key` isn't checked
against anything — any value works, including omitting the header
entirely. A managed, multi-tenant deployment enforces real per-org keys
here instead; see [API keys and access](../operate/keys.md) for that model.

!!! warning "If your server runs over stdio, stop here"

    The client launches it on your **user's** machine, so a key in that config
    is a permanent credential on someone else's laptop. Use
    [session tokens](../operate/session-tokens.md) instead — and install
    `"mcpobs[sessions]"` rather than plain `mcpobs`, or no token is ever obtained
    and the stderr diagnostic will blame your endpoint for a missing dependency.

## 5. Open the console

```
http://localhost:8080
```

The console still asks for a key on first load — type anything, it isn't
checked against anything in single-tenant mode. Traces appear within
seconds — end-to-end freshness is typically under a minute, and the console
shows you the current figure in the sidebar so you never have to guess
whether you are looking at current data.

## Next

- [Your first trace](first-trace.md) — how to read what you are now collecting.
- [Databases, caches and APIs](../integrate/downstream.md) — make your
  Postgres, Redis and HTTP calls visible too.
