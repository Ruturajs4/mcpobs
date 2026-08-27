/**
 * mcpobs -- MCP-native observability for TypeScript/Node MCP servers.
 *
 * MUST be called BEFORE the target MCP server class is imported/constructed
 * anywhere in the process. `instrument()` patches `Server.prototype` (see
 * dispatch.ts for exactly why), which only affects instances created AFTER
 * the patch is installed:
 *
 *   import { instrument } from "mcpobs";
 *   instrument();
 *
 *   import { YourMcpServer } from "your-mcp-server-package";
 *   const server = new YourMcpServer({ ... });   // now instrumented
 *
 * This is the same ordering requirement Node's own tracing agents already
 * use (`require('dd-trace').init()` as the literal first line of an
 * entrypoint) -- not a compromise specific to this SDK.
 */

import { NodeTracerProvider, BatchSpanProcessor } from "@opentelemetry/sdk-trace-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";
import type { SpanExporter } from "@opentelemetry/sdk-trace-node";

import { patchDispatch } from "./dispatch.js";
import { setTransport } from "./transport.js";
import { instrumentDownstream } from "./downstream.js";
import { SessionProvider, configure as configureSession, type SessionHeaders } from "./session.js";
import { SessionSpanExporter } from "./exporter.js";

export { classify, classifyResult, FailureKind, FailureKindSource } from "./classifier.js";
export { SpanAttributes } from "./dispatch.js";
export { SessionProvider } from "./session.js";
export { setCustomAttribute, CUSTOM_ATTRIBUTE_PREFIX } from "./custom.js";
export {
  instrumentDownstream,
  instrumentDatabases,
  availableDatabaseInstrumentations,
  type DownstreamOptions,
  type DatabaseInstrumentationReport,
} from "./downstream.js";

export interface InstrumentOptions {
  /** Default "stdio" -- the only transport this SDK's real-world validation
   * target ever runs. Override explicitly for other
   * transports; there is no auto-detection yet (see transport.ts). */
  transport?: string;
  /** Record failure text (truncated) from FAILING calls only. Mirrors the
   * Python SDK's default. */
  captureErrorDetail?: boolean;
  /** Record redacted args/results for every call. Off by default, mirrors
   * the Python SDK. */
  capturePayloads?: boolean;
  /** ADR-011: mint short-lived tokens from a customer-hosted endpoint
   * instead of a static long-lived key. */
  sessionEndpoint?: string;
  sessionHeaders?: SessionHeaders;
  /** Wire HTTP auto-instrumentation (Axios sits on Node's http module).
   * Default true. This alone is enough for the common case: a CJS-authored
   * HTTP client (axios and most of the npm ecosystem), instrumented via
   * `require-in-the-middle`, needs no launch-time flag -- only that
   * `instrument()` runs before the client library is imported (the same
   * ordering `instrument()` already requires for MCP-dispatch patching, see
   * dispatch.ts). If a dependency reaches `node:http` via its OWN genuine ESM
   * `import` statement instead, see register.ts's `mcpobs/register` for that
   * narrower case -- most consumers will not need it. */
  instrumentHttp?: boolean;
  /** Capture allow-listed request/response headers and (best-effort) request
   * AND response bodies on outbound HTTP spans. Its OWN opt-in, separate
   * from `instrumentHttp` -- matches instrument_httpx() being a distinct
   * call from base HTTP tracing in the Python SDK (D60). Unlike the Python
   * SDK, response bodies ARE captured here (size-capped, content-type
   * filtered, redacted) -- see downstream.ts's module docstring for the
   * measurement that makes this a real JS-only capability, not parity with
   * Python. A downstream response can carry customer data (PII, tokens
   * embedded in a body field pattern-redaction doesn't catch) -- weigh that
   * before enabling this in a customer's production server. Default
   * false. */
  captureHttpDetail?: boolean;
  serviceName?: string;
}

let installed = false;

export function instrument(options: InstrumentOptions = {}): void {
  if (installed) return;
  installed = true;

  setTransport(options.transport ?? "stdio", { explicit: !!options.transport });

  const exporter = buildExporter(options);
  const provider = new NodeTracerProvider({
    resource: resourceFromAttributes({
      [ATTR_SERVICE_NAME]: options.serviceName ?? "mcp-server",
    }),
    // OTel's 5s default loses spans when clients such as Codex stop a STDIO
    // child immediately after a tool result. A short delay preserves batching
    // without putting exporter latency on the request/response path.
    spanProcessors: [
      new BatchSpanProcessor(exporter, { scheduledDelayMillis: 100 }),
    ],
  });
  provider.register();

  const tracer = provider.getTracer("mcpobs");
  patchDispatch({
    tracer,
    captureErrorDetail: options.captureErrorDetail ?? true,
    capturePayloads: options.capturePayloads ?? false,
  });

  if (options.instrumentHttp ?? true) {
    instrumentDownstream({ captureHttpDetail: options.captureHttpDetail ?? false });
  }

  // Serialize lifecycle flushes. A client may close stdin and then send a
  // signal, or stdin may close just before `beforeExit`; queueing preserves
  // ordering and lets the later flush collect spans that finished while the
  // earlier one was in flight.
  let flushChain = Promise.resolve();
  const flush = (): Promise<void> => {
    flushChain = flushChain.then(() => provider.forceFlush()).catch(() => {});
    return flushChain;
  };

  // Measured, not assumed: BatchSpanProcessor's default flush interval is
  // 5000ms. A short-lived process -- or a client (Claude Desktop, an IDE)
  // tearing a stdio server down with SIGTERM the moment it's done with it --
  // can exit before that timer ever fires, silently losing every span
  // created since the last flush. Node's own default behavior on SIGTERM/
  // SIGINT is to exit immediately; registering a handler suppresses that,
  // so this must explicitly call process.exit() itself once the flush
  // settles (or times out) rather than leaving the process to hang.
  for (const signal of ["SIGTERM", "SIGINT"] as const) {
    process.on(signal, () => {
      const timeout = setTimeout(() => process.exit(0), 3000);
      flush().finally(() => {
          clearTimeout(timeout);
          process.exit(0);
        });
    });
  }

  // STDIO clients do not all terminate servers with an OS signal. Codex CLI
  // closes stdin when an ephemeral session finishes, so flush at that explicit
  // transport boundary. Keep `beforeExit` as a fallback for other clients that
  // stop their event loops without closing stdin first.
  process.stdin.once("end", () => {
    void flush();
  });
  process.once("beforeExit", () => {
    void flush();
  });
}

function buildExporter(options: InstrumentOptions): SpanExporter {
  if (options.sessionEndpoint || options.sessionHeaders) {
    configureSession({ endpoint: options.sessionEndpoint, headers: options.sessionHeaders });
    return new SessionSpanExporter({ provider: new SessionProvider({
      endpoint: options.sessionEndpoint,
      headers: options.sessionHeaders,
    }) });
  }
  // Static key / standard OTel env vars (OTEL_EXPORTER_OTLP_ENDPOINT,
  // OTEL_EXPORTER_OTLP_HEADERS) -- the exporter reads these itself when not
  // given an explicit url/headers.
  return new OTLPTraceExporter();
}
