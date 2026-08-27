/**
 * `mcpobs/register` fixes a real, measured gap, but a NARROW one: this probe
 * script accesses `node:http` via a genuine ESM `import * as http` statement
 * of its own, and @opentelemetry/instrumentation-http produces ZERO spans for
 * THAT pattern without the fix (verified against the installed SDK directly,
 * not assumed from docs). This does NOT mean every HTTP client in an ESM
 * process needs this fix -- a CJS-authored client (axios and most of the npm
 * ecosystem) is instrumented via `require-in-the-middle` instead, which needs
 * no loader hook and no launch flag at all; verified separately against a
 * real CJS HTTP client in a real ESM MCP server (see register.ts's module
 * docstring). This file exists for the remaining case: a dependency whose own
 * source is ESM and reaches `node:http` directly.
 *
 * The supported contract is `node --import mcpobs/register entry.js`, NOT a
 * plain `import "mcpobs/register";` statement in the entrypoint -- that was
 * the original design and it measured as unreliable: `node:module`'s
 * `register()` hands work off to a separate loaders thread and returns
 * before that hook is confirmed active, so a sibling static import in the
 * SAME file (e.g. `node:http`, reached transitively through the MCP server
 * import) can already be resolving before the patch is live. `--import`
 * (or `NODE_OPTIONS=--import=...`) guarantees the hook is registered before
 * the entry file's own module graph starts loading at all. See register.ts's
 * module docstring for the full measurement trail.
 *
 * Testing this INSIDE vitest's own process is unreliable regardless: vitest's
 * worker may have already loaded `node:http` before this test file's own
 * imports run, for reasons that have nothing to do with whether the fix
 * works. This spawns REAL, separate `node` processes -- the same reasoning
 * tests/test_stdio_transport.py uses on the Python side for a different
 * process-startup-order question.
 */

import { describe, it, expect } from "vitest";
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const ROOT = join(import.meta.dirname, "..");
// `--import` resolves its specifier the same way a static `import` does --
// an absolute Windows path like "C:\..." is parsed as a URL with scheme
// "c:" and rejected, so this needs to be a file:// URL.
const REGISTER_PATH = pathToFileURL(join(ROOT, "dist", "register.js")).href;

const SCRIPT_BODY = `
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { HttpInstrumentation } from "@opentelemetry/instrumentation-http";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import * as http from "node:http";

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
registerInstrumentations({ instrumentations: [new HttpInstrumentation()], tracerProvider: provider });

const server = http.createServer((req, res) => { res.end("ok"); });
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const port = server.address().port;
await new Promise((resolve, reject) => {
  const req = http.request(\`http://127.0.0.1:\${port}/x\`, { method: "GET" }, (res) => {
    res.on("data", () => {});
    res.on("end", () => resolve());
  });
  req.on("error", reject);
  req.end();
});
await new Promise((r) => setTimeout(r, 200));
console.log(JSON.stringify({ spanCount: exporter.getFinishedSpans().length }));
server.close();
`;

function runInRealProcess(extraArgs: string[], cwd: string = ROOT): Promise<{ spanCount: number }> {
  // Written INSIDE mcpobs-js's own tree, not os.tmpdir() -- Node resolves
  // node_modules by walking up from the SCRIPT's own location, not from
  // `cwd`. A script outside this tree cannot see @opentelemetry/* at all,
  // regardless of what cwd is passed to spawn().
  const dir = mkdtempSync(join(ROOT, ".register-test-tmp-"));
  const scriptPath = join(dir, "probe.mjs");
  writeFileSync(scriptPath, SCRIPT_BODY);
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [...extraArgs, scriptPath], { cwd });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("close", (code) => {
      rmSync(dir, { recursive: true, force: true });
      if (code !== 0) {
        reject(new Error(`probe process exited ${code}\nstderr:\n${stderr}`));
        return;
      }
      const line = stdout.trim().split("\n").pop() ?? "{}";
      try {
        resolve(JSON.parse(line));
      } catch {
        reject(new Error(`could not parse probe output: ${stdout}`));
      }
    });
    child.on("error", reject);
  });
}

describe("mcpobs/register, against real separate node processes", () => {
  it(
    "WITHOUT --import mcpobs/register, HTTP auto-instrumentation produces zero spans in pure ESM",
    async () => {
      const { spanCount } = await runInRealProcess([]);
      expect(spanCount).toBe(0);
    },
    15_000,
  );

  it(
    "WITH node --import mcpobs/register, HTTP auto-instrumentation produces the expected spans",
    async () => {
      const { spanCount } = await runInRealProcess(["--import", REGISTER_PATH]);
      expect(spanCount).toBe(2); // client span + server span
    },
    15_000,
  );

  it(
    "still works when the process cwd is NOT mcpobs's own install directory",
    async () => {
      // The regression this test exists to catch: register.ts used to
      // resolve "@opentelemetry/instrumentation/hook.mjs" relative to
      // pathToFileURL("./") -- i.e. relative to cwd, not to register.ts's
      // OWN location. A customer launched via a process manager, systemd
      // unit, or monorepo where cwd isn't mcpobs's package root would have
      // that resolution fail silently. os.tmpdir() has no node_modules
      // ancestor chain leading back to mcpobs's dependencies, so this cwd
      // is deliberately hostile to the old, buggy resolution -- the probe
      // SCRIPT still lives inside mcpobs-js's own tree (unrelated concern,
      // see the comment on runInRealProcess), only the spawned process's
      // cwd is elsewhere.
      const { spanCount } = await runInRealProcess(["--import", REGISTER_PATH], tmpdir());
      expect(spanCount).toBe(2);
    },
    15_000,
  );
});
