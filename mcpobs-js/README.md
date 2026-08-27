# mcpobs

Observability for Model Context Protocol servers, built for the protocol
rather than adapted to it. TypeScript/Node SDK — see
[mcpobs on PyPI](https://pypi.org/project/mcpobs/) for the Python one.

A generic tracing tool tells you a request took 800ms and returned 200. This
tells you which tool failed, *why* it failed, and where the time went —
because it understands MCP as a protocol.

```bash
npm install @mcpobs/node
```

```ts
import { instrument } from "@mcpobs/node";
instrument();                 // call before your MCP server is constructed

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
const server = new McpServer({ name: "my-server", version: "1.0.0" });
```

`instrument()` patches `Server.prototype`, so it only affects instances
created *after* it runs — the same ordering rule Node's other tracing
agents already use (`require('dd-trace').init()` as literally the first
line of an entrypoint), not a compromise specific to this SDK.

## What you get

**Every protocol method is instrumented**, not just `tools/call` —
`initialize`, `tools/list`, `resources/read`, everything dispatched through
the MCP SDK's `Server`, each with the right named attribute where one
applies (a tool name, a resource URI).

**Failures are classified, not counted.** `tool_error`, `server_exception`,
`unknown_tool`, `invalid_arguments`, `protocol_error` — each is a different
problem with a different owner. One "error rate" collapses them into a
number that cannot tell you what to do next.

## Your database, cache and HTTP calls

```ts
import { instrument, instrumentDatabases, availableDatabaseInstrumentations } from "@mcpobs/node";

instrument();
await instrumentDatabases();   // Postgres, MySQL, Redis, MongoDB, ...
```

Node has no formal instrumentor registry the way Python's
`importlib.metadata.entry_points()` does, so this wraps
`@opentelemetry/auto-instrumentations-node` — a curated ~35-instrumentor
bundle, each safe to enable even when its target library was never
installed. It's an **optional peer dependency**, not a regular one:
`npm install @mcpobs/node` alone does not pull in ~35 packages you might not use.

```bash
npm install @opentelemetry/auto-instrumentations-node
```

Outbound HTTP (axios, node-fetch, undici, and most of the npm ecosystem
built on `node:http`) is instrumented by `instrument()` itself — no separate
call needed for basic tracing. Two further opt-ins go deeper:

```ts
instrument({
  instrumentHttp: true,        // default; base HTTP spans
  captureHttpDetail: true,     // request/response headers AND bodies
});
```

`captureHttpDetail` captures the **response body**, not just the request —
a genuine Node-only capability, not parity with the Python SDK. Python's
`httpx`-based instrumentation closes its client span before the response
body is read; Node's `@opentelemetry/instrumentation-http` keeps the span
open through the full response stream, measured directly rather than
assumed. Response and request bodies are size-capped, content-type
filtered, and pass through the same redaction as tool payloads — weigh that
before enabling it in a production server, since a response body can carry
customer data.

## What does not leave your process

Error classification reads SDK-generated boilerplate and reduces it to a
single enum before anything is exported. Tool arguments and results are
**not** captured by default.

```ts
instrument({ capturePayloads: true });   // opt in to redacted args/results
```

## Servers launched by the client (stdio)

If the client starts your server, it runs on your **user's** machine — so it
must not hold a long-lived credential. Your backend mints a short-lived
token instead:

```ts
instrument({
  sessionEndpoint: "https://yourapp.com/mcpobs-session",
  sessionHeaders: () => ({ authorization: `Bearer ${currentToken()}` }),
});
```

Pass a **function**, not a static object — the credential authenticating
that call usually refreshes, and a value read once at startup would work
for an hour and then quietly stop.

## A dependency that reaches `node:http` via its own ESM `import`

`instrument()`'s HTTP patching covers the common case (a CJS-authored
client library, hooked via `require-in-the-middle`) with no launch flag
needed. A dependency that imports `node:http` directly via a genuine ESM
`import` statement needs the loader hook instead:

```bash
node --import @mcpobs/node/register your-server.js
```

Most consumers will not need this — it's a narrower case than the default
path above.

## Requirements

- Node 18+
- `@modelcontextprotocol/sdk` (peer dependency)
- `@opentelemetry/auto-instrumentations-node` (optional peer dependency,
  only needed for `instrumentDatabases()`)

Licensed under Apache-2.0.
