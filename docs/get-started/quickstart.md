# Quickstart

## 1. Run the stack

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
make up        # ClickHouse, Kafka, Collector, Postgres, MinIO, ingest,
               # normalizer, archiver, query API
make devkeys   # provision a local org and API keys
```

`make devkeys` writes a read key and an ingest key to a local, git-ignored file.
Access is invite-only: there is no self-service signup, and no endpoint that
mints a key.

## 2. Instrument your server

```python
from mcpobs import instrument

instrument(mcp)  # (1)!
```

1.  `mcp` is your `MCPServer` instance. This adds a middleware; it does not
    wrap or replace the protocol.

## 3. Point it at the collector

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<your ingest key>"
```

!!! note "Ingest keys and read keys are different"

    An ingest key lives in your server process and deployment config. A read key
    lives in a browser. One being compromised must not imply the other, so the
    console will refuse an ingest key and the ingest endpoint will refuse a read
    key. See [API keys and access](../operate/keys.md).

## 4. Open the console

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
