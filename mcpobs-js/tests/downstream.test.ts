/**
 * The request-body capture path is the highest-risk new code in this
 * package: it patches `.write`/`.end` on a live `http.ClientRequest`. Tested
 * against a REAL local HTTP server and a REAL client request -- not a
 * hand-rolled fake of the Node http module -- because a subtly wrong patch
 * (wrong argument forwarding, wrong return value, calling the original
 * twice) would only show up against the real thing.
 *
 * The detail-capture tests run in a REAL, SEPARATE `node` process, not
 * in-process under vitest -- same reasoning as register.test.ts: vitest's
 * own worker may have already loaded `node:http` for reasons unrelated to
 * this package, before this file's own code ever runs, which makes the
 * @opentelemetry/instrumentation-http ESM patch unreliable to observe
 * in-process. `instrumentDatabases`/`availableDatabaseInstrumentations`
 * don't depend on spans being created, so those stay in-process below.
 *
 * The probe process is launched with `node --import <register.js>`, NOT a
 * plain `import "..."` statement inside the probe script -- a static import
 * measured as unreliable (see register.ts's module docstring and
 * register.test.ts): `node:module`'s `register()` hands off to a separate
 * loaders thread and returns before the hook is confirmed active, so this
 * script's own `import * as http` a few lines later could already be
 * resolving before the patch is live.
 */

import { describe, it, expect } from "vitest";
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { instrumentDatabases, availableDatabaseInstrumentations } from "../src/downstream.js";

const ROOT = join(import.meta.dirname, "..");
// `--import` resolves its specifier the same way a static `import` does --
// an absolute Windows path like "C:\..." is parsed as a URL with scheme
// "c:" and rejected, so this needs to be a file:// URL.
const REGISTER_PATH = pathToFileURL(join(ROOT, "dist", "register.js")).href;

// ESM import specifiers need file:// URLs on Windows -- a raw "C:\..." path
// is parsed as a URL with scheme "c:" and rejected by the ESM loader.
const PROBE_SCRIPT = `
import * as http from "node:http";
import { gzipSync } from "node:zlib";
import { trace, SpanKind } from "@opentelemetry/api";
import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { instrumentDownstream } from "${pathToFileURL(join(ROOT, "dist", "downstream.js")).href}";

const exporter = new InMemorySpanExporter();
const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
trace.setGlobalTracerProvider(provider);
instrumentDownstream({ captureHttpDetail: true });

const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => (body += chunk));
  req.on("end", () => {
    if (req.url === "/gzip-echo") {
      const payload = gzipSync(Buffer.from(JSON.stringify({ compressed: true, echoed: body })));
      res.setHeader("content-type", "application/json");
      res.setHeader("content-encoding", "gzip");
      res.end(payload);
      return;
    }
    if (req.url === "/binary") {
      res.setHeader("content-type", "image/png");
      res.end(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
      return;
    }
    if (req.url === "/big") {
      res.setHeader("content-type", "text/plain");
      res.end("x".repeat(600 * 1024)); // bigger than MAX_RESPONSE_BODY_BYTES (512 KiB)
      return;
    }
    if (req.url === "/big-json") {
      // The regression this test exists for: a real "list projects" response
      // ran past the OLD 64 KiB cap and got cut off mid-object -- invalid
      // JSON, which the query UI's tree renderer correctly refuses to parse
      // and silently falls back to flat text for. Bumping the cap to 512 KiB
      // doesn't make truncation impossible, just rarer -- this proves the
      // still-truncated case degrades safely (a marker, not a crash) rather
      // than proving truncation itself is gone.
      const items = Array.from({ length: 40000 }, (_, i) => ({ id: i, name: \`item-\${i}\` }));
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ items }));
      return;
    }
    res.setHeader("content-type", "application/json");
    res.setHeader("x-request-id", "server-generated-id");
    res.setHeader("set-cookie", "should-never-be-captured=1");
    res.end(JSON.stringify({ echoed: body, ok: true }));
  });
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;

function post(path, body, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      \`http://127.0.0.1:\${port}\${path}\`,
      { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer sekret", ...headers } },
      (res) => {
        res.on("data", () => {});
        res.on("end", resolve);
      },
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// write(chunk, encoding) -- the SDK's own overload, not something a caller
// invents. A string chunk written with a non-UTF-8 encoding is text in THAT
// encoding, not UTF-8 text that happens to look like it; the real bytes on
// the wire are correct regardless (originalWrite still gets the original
// encoding argument), only the CAPTURED preview used to get this wrong.
function postWithEncoding(path, text, encoding) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      \`http://127.0.0.1:\${port}\${path}\`,
      { method: "POST", headers: { "content-type": "text/plain" } },
      (res) => {
        res.on("data", () => {});
        res.on("end", resolve);
      },
    );
    req.on("error", reject);
    req.write(Buffer.from(text, "utf-8").toString(encoding), encoding);
    req.end();
  });
}

// Reproduces axios's own Node adapter behaviour EXACTLY (lib/adapters/
// http.js): reads content-encoding, pipes the raw response through its own
// decompressor, then \`delete res.headers['content-encoding']\` -- "to not
// confuse downstream operations" (axios's own comment). This is the client
// mcpobs-js actually runs behind in production; a real regression here
// (reading the header lazily, after axios has already deleted it) silently
// produced "[binary, N bytes]" for every real compressed API response.
function postWithMutatingClient(path, body) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      \`http://127.0.0.1:\${port}\${path}\`,
      { method: "POST", headers: { "content-type": "application/json" } },
      (res) => {
        if (res.headers["content-encoding"] === "gzip") {
          delete res.headers["content-encoding"];
        }
        res.on("data", () => {});
        res.on("end", resolve);
      },
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

await post("/echo-1", JSON.stringify({ message: "hello from a real client request" }));
await post("/echo-2", "{}", { "x-request-id": "client-req-id" });
await post("/echo-3", "{}");
await post("/echo-4", "{}");
await post("/echo-5", JSON.stringify({ a: 1 }));
await post("/echo-6", JSON.stringify({ note: "sk-abcdefghijklmnopqrstuvwx embedded in a field" }));
await post("/gzip-echo", JSON.stringify({ x: 1 }));
await post("/binary", "{}");
await post("/big", "{}");
await postWithMutatingClient("/gzip-echo", JSON.stringify({ x: 2 }));
await post("/big-json", "{}");
await postWithEncoding("/echo-7", "hello from base64 land", "base64");

await new Promise((r) => setTimeout(r, 300));
server.close();

// HttpInstrumentation creates BOTH a client span (the outgoing request this
// package annotates) and a server span (the local test server receiving it)
// per call, both named just the HTTP method -- distinguish by kind, not name.
const spans = exporter.getFinishedSpans().filter((s) => s.kind === SpanKind.CLIENT);
console.log(JSON.stringify(spans.map((s) => ({ name: s.name, attributes: s.attributes }))));
`;

interface ProbeSpan {
  name: string;
  attributes: Record<string, unknown>;
}

function runProbe(): Promise<ProbeSpan[]> {
  const dir = mkdtempSync(join(ROOT, ".downstream-test-tmp-"));
  const scriptPath = join(dir, "probe.mjs");
  writeFileSync(scriptPath, PROBE_SCRIPT);
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["--import", REGISTER_PATH, scriptPath], { cwd: ROOT });
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
      const line = stdout.trim().split("\n").pop() ?? "[]";
      try {
        resolve(JSON.parse(line));
      } catch {
        reject(new Error(`could not parse probe output: ${stdout}\nstderr:\n${stderr}`));
      }
    });
    child.on("error", reject);
  });
}

const REQ_BODY = "mcpobs.http.request.body";
const REQ_HEADERS = "mcpobs.http.request.headers";
const RESP_HEADERS = "mcpobs.http.response.headers";
const RESP_BODY = "mcpobs.http.response.body";

let spans: ProbeSpan[];

describe("instrumentDownstream request/response detail capture", () => {
  it(
    "runs the real probe process and captures all twelve requests",
    async () => {
      spans = await runProbe();
      expect(spans.length).toBe(12);
    },
    20_000,
  );

  it("captures the real request body written via .write()+.end()", () => {
    const body = spans[0].attributes[REQ_BODY] as string;
    expect(body).toContain("hello from a real client request");
  });

  it("captures request headers, but only the allow-listed ones -- authorization is never read", () => {
    const headers = spans[1].attributes[REQ_HEADERS] as string;
    expect(headers).toContain("client-req-id");
    expect(headers).not.toContain("sekret");
    expect(headers).not.toContain("authorization");
  });

  it("captures response headers, allow-listed only -- set-cookie is never read", () => {
    const headers = spans[2].attributes[RESP_HEADERS] as string;
    expect(headers).toContain("server-generated-id");
    expect(headers).not.toContain("should-never-be-captured");
  });

  it("captures a plain (uncompressed) JSON response body -- overturns the inherited Python assumption", () => {
    // Measured directly (see downstream.ts's module docstring): unlike
    // httpx, Node's client span stays open through the response stream's
    // 'end' event, so this is a real capability, not a gap.
    const body = spans[3].attributes[RESP_BODY] as string;
    expect(body).toContain('"ok":true');
  });

  it("the real request still completes normally -- patching write/end must not break the call", () => {
    // If .write()/.end() forwarded arguments wrong, or returned the wrong
    // value, or double-invoked the original, the probe's post() calls would
    // have hung or rejected and runProbe() above would already have thrown.
    expect(spans[4]).toBeDefined();
  });

  it("redacts a recognizable secret FORMAT embedded in the raw request body text", () => {
    // A raw HTTP body is opaque bytes-that-happen-to-be-text, not a
    // structured object -- there is no key name to check the way tool
    // args/results get (redactObject). Only pattern-based redaction of
    // recognizable secret formats (Bearer tokens, sk-/gh*_/AKIA/JWT shapes)
    // applies here, matching mcpobs/http.py's own documented, deliberate
    // scope (D56): a bare `"password": "..."` field in a raw body is NOT
    // caught by this layer, by design, on both sides of this SDK.
    const body = spans[5].attributes[REQ_BODY] as string;
    expect(body).toContain("[redacted]");
    expect(body).not.toContain("sk-abcdefghijklmnopqrstuvwx");
  });

  it("decompresses a real gzip-encoded response body", () => {
    // Real APIs commonly gzip their responses -- axios sends Accept-Encoding
    // for exactly this reason. Without decompression this would render as
    // "[binary, N bytes]", which is what this test would catch regressing to.
    const body = spans[6].attributes[RESP_BODY] as string;
    expect(body).toContain('"compressed":true');
  });

  it("skips response body capture entirely for a known-binary content-type", () => {
    expect(spans[7].attributes[RESP_BODY]).toBeUndefined();
  });

  it("caps and truncates a response body larger than MAX_RESPONSE_BODY_BYTES", () => {
    const body = spans[8].attributes[RESP_BODY] as string;
    expect(body).toContain("...[truncated]");
    // Capped well under the real 600 KiB the server sent.
    expect(body.length).toBeLessThan(600 * 1024);
  });

  it("still decompresses correctly even when the real client (axios-like) deletes content-encoding after reading it", () => {
    // The regression this test exists to catch: reading content-encoding
    // lazily, inside the async 'end' handler, silently found nothing once a
    // real client (axios, and this probe's postWithMutatingClient) had
    // already deleted the header from the SAME shared headers object by
    // that point -- every real compressed API response rendered as
    // "[binary, N bytes]" instead of its actual body.
    const body = spans[9].attributes[RESP_BODY] as string;
    expect(body).toContain('"compressed":true');
  });

  it("degrades safely (a marker, not a crash or garbage) for a real JSON response that still gets truncated", () => {
    // The exact shape of the bug report this cap increase responded to: a
    // real "list projects" endpoint, real JSON, cut off mid-array. Proves
    // this still works correctly even though MAX_RESPONSE_BODY_BYTES only
    // makes truncation rarer, not impossible -- capture must never throw,
    // and a truncated capture must be honestly marked, never silently wrong.
    const body = spans[10].attributes[RESP_BODY] as string;
    expect(body).toContain("...[truncated]");
    expect(body.length).toBeLessThan(600 * 1024);
  });

  it("decodes a request body written with a non-UTF-8 encoding correctly, not as raw UTF-8 of the encoded text", () => {
    // The regression: request.write(chunk, encoding) forwarded `encoding`
    // to the real write() call (so the actual bytes on the wire were always
    // right) but accumulate() ignored it and always did Buffer.from(chunk)
    // -- i.e. treated base64 TEXT as UTF-8 text, producing a garbled preview
    // instead of the real "hello from base64 land".
    const body = spans[11].attributes[REQ_BODY] as string;
    expect(body).toContain("hello from base64 land");
  });
});

describe("instrumentDatabases", () => {
  it("reports a clear, non-throwing outcome when the optional peer dependency is not resolvable", async () => {
    // @opentelemetry/auto-instrumentations-node IS installed as a
    // devDependency in THIS package for type-checking, so this test cannot
    // exercise the true "never installed" path without faking the import --
    // what it CAN and does prove is the never-throws contract end to end.
    await expect(instrumentDatabases()).resolves.not.toThrow();
  });

  it("availableDatabaseInstrumentations lists exactly the curated allow-list, not the full ~35-package bundle", () => {
    const names = availableDatabaseInstrumentations();
    expect(names).toContain("@opentelemetry/instrumentation-pg");
    expect(names).toContain("@opentelemetry/instrumentation-redis");
    expect(names).toContain("@opentelemetry/instrumentation-mysql2");
    // Deliberately NOT included: this MCP server integration target has no
    // HTTP framework of its own to trace server-side, and enabling every
    // bundled instrumentor by default is exactly what this design avoids.
    expect(names).not.toContain("@opentelemetry/instrumentation-express");
    expect(names).not.toContain("@opentelemetry/instrumentation-aws-lambda");
  });
});
