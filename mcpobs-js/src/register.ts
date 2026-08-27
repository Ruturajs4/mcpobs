/**
 * ONLY needed for a narrow case: a dependency that reaches `node:http` via
 * its OWN genuine ESM `import` statement, rather than through `require()`.
 * Most real HTTP client libraries do NOT need this -- axios, and most of the
 * npm HTTP-client ecosystem, are CommonJS-authored internally, so even when
 * consumed from an ESM project, their OWN internal `require('http')` call is
 * intercepted by OTel's `require-in-the-middle` hook, which is a plain,
 * patchable JS function override (`Module.prototype.require`) with NO
 * process-startup-time flag required at all -- `instrument()` as a normal
 * function call, run before that dependency is ever imported (see
 * dispatch.ts's ordering requirement, which already demands this), is
 * sufficient. Verified directly against a real CJS-authored HTTP client in a
 * real ESM MCP server: HTTP spans, with full request/response header
 * capture, with no `--import`/`NODE_OPTIONS` flag on the launch command.
 *
 * This file exists for the remaining case: a dependency whose OWN source is
 * ESM and does `import * as http from "node:http"` (or similar) directly.
 * That requires `import-in-the-middle`, which needs `node:module`'s
 * `register()` to install a loader hook before `node:http` is first loaded
 * anywhere in the process -- and MUST be loaded via Node's `--import` flag
 * (or `NODE_OPTIONS=--import=...`), NOT a regular `import "mcpobs/register";`
 * statement in the entrypoint:
 *
 *   node --import mcpobs/register dist/index.js
 *   # or:
 *   NODE_OPTIONS="--import mcpobs/register" node dist/index.js
 *
 * WHY NOT A NORMAL IMPORT -- this was tried first and measured to be
 * unreliable, not a style preference. Calling `register()` synchronously from
 * top-level code in a file that is itself reached via a normal
 * `import "mcpobs/register";` statement LOOKS like it runs early enough --
 * ES module evaluation order does guarantee this file's body runs before any
 * later sibling import in the same file. But `register()`'s actual work
 * happens on a separate, dedicated loaders thread: the synchronous call only
 * *starts* that handshake, and does not block until the hook is confirmed
 * active. Measured directly: with `import "./register.js"` as the literal
 * first line of an entry file, followed immediately by
 * `import * as http from "node:http"` and a real HTTP request, the result
 * was 0 spans -- the hooks thread had not finished registering by the time
 * `node:http` resolved. The exact same registration call, made to run via
 * `node --import ./register.js entry.js` instead, produced the correct 2
 * spans every time. This matches Node's own documented guidance: the
 * `--experimental-loader` flag's deprecation warning itself points at
 * `--import` as the supported replacement for early hook registration, not
 * at calling `register()` from inside the entry module.
 *
 * Before reaching for this file, check whether the dependency in question is
 * actually CJS-authored (most are) -- if so, a plain `instrument()` call,
 * ordered before that dependency loads, is enough and this file adds an
 * unnecessary launch-command requirement.
 *
 * The specifier is resolved from THIS MODULE's own location
 * (`import.meta.url`), not from the process's current working directory.
 * `pathToFileURL("./")` resolves relative to `cwd`, which is a real bug, not
 * a style choice: a customer launching via a process manager, systemd unit,
 * or monorepo where cwd is not mcpobs's own install directory would have
 * Node's resolver walk up from the WRONG starting point looking for
 * `node_modules/@opentelemetry/instrumentation`, fail to find it, and
 * silently never register the hook -- the exact failure mode this file
 * exists to fix, reappearing one layer up. `import.meta.url` always points
 * inside mcpobs's own package directory, where `@opentelemetry/
 * instrumentation` is guaranteed to resolve as mcpobs's own dependency,
 * regardless of the customer process's cwd.
 */

import { register } from "node:module";

register("@opentelemetry/instrumentation/hook.mjs", import.meta.url);
