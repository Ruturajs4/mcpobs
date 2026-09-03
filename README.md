<img src="assets/logo.svg" width="48" height="48" alt="mcpobs">

# mcp-observability

MCP-native observability for Model Context Protocol servers. Spans travel
**OTel → Collector → Kafka → normalizer → ClickHouse** and come back out as a
queryable, MCP-aware trace — which tool failed, why, and where the time went.

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
make check     # unit tests + ruff + mypy (no stack required)
make up-lite   # clickhouse, ingest, query -- three containers, single tenant
make demo      # run the tool scenarios over stdio and streamable HTTP

open http://localhost:8080     # the console
```

This self-hosted image is **single-tenant**: any `x-api-key` value is
accepted, so there's no key to provision.

## Add it to your server

**Python:**

```bash
pip install mcpobs
```

```python
from mcpobs import instrument
instrument(mcp)          # that is the whole integration
```

See [`README-sdk.md`](README-sdk.md) for the full Python SDK reference.

**Node / TypeScript:**

```bash
npm install @mcpobs/node
```

```ts
import { instrument } from "@mcpobs/node";
instrument();             // call before your MCP server is constructed
```

See [`mcpobs-js/README.md`](mcpobs-js/README.md) for the full Node SDK
reference.

## What you get

- **Failures are classified, not counted** — `tool_error`, `unknown_tool`,
  `invalid_arguments`, `protocol_error`, `401`/`403` are each a different
  problem with a different owner, not one collapsed error rate.
- **Long-running tools stay visible** — `report_progress()` calls and
  subscription events show up as spans *while the tool is still running*, not
  only after it finishes.
- **Your database, cache, and API calls** show up as child spans under the
  tool call — one opt-in call (`instrument_downstream()`) auto-detects
  whatever OpenTelemetry instrumentors you already have installed.
- **Nothing sensitive by default** — tool arguments and results are never
  captured unless you turn it on; error text is truncated and secrets are
  redacted before storage.
- **Session tokens for stdio servers** — when the client launches your server
  on someone else's machine, mint a short-lived token instead of shipping a
  long-lived key.

Full guides for each of these live at the [docs site](https://docs.example.com),
built from [`docs-public/`](docs-public/).

## Layout

```
demo_server/   MCP SDK v2 server + OTel bootstrap + scenarios
mcpobs/        the Python SDK (failure taxonomy, payload capture)
mcpobs-js/     the Node/TypeScript SDK
control/       pluggable Authenticator interface + single-tenant default --
               a multi-tenant control plane is a separate deployment concern
ingest/        gateway -- resolves the caller, stamps the tenant
collector/     OTel Collector gateway config (otlp in, kafka out)
normalizer/    Kafka consumer -> MCP field extraction -> ClickHouse
archiver/      Kafka consumer -> raw OTLP to object storage
query/         read API + console
kafka/         explicit topic creation -- never auto-create
scripts/       verification and attribute capture
```

## Documentation

Customer-facing docs live in [`docs-public/`](docs-public/) (`make docs` for
live reload, `make docs-build` to build strictly).

## License

Apache-2.0.
