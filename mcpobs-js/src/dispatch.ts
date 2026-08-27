/**
 * The core instrumentation: patch MCP request dispatch, create spans,
 * classify failures.
 *
 * WHY PATCH `Server.prototype.setRequestHandler`, NOT `McpServer.prototype.tool`.
 * A downstream MCP server's constructor commonly registers every tool
 * synchronously, inside the constructor. An
 * instance-based `instrument(server)` called after construction would be
 * wrapping handlers already registered -- too late. Patching a PROTOTYPE
 * method before any instance exists sidesteps the ordering problem
 * entirely, regardless of when inside a constructor the real registration
 * happens.
 *
 * `Server.setRequestHandler(schema, handler)` (the LOW-LEVEL class beneath
 * McpServer, exposed publicly as `mcp.server`) is the single point EVERY
 * protocol method's dispatch flows through -- not just tools/call and
 * prompts/get. `McpServer.tool()`/`.prompt()`/`.resource()` each call
 * `this.server.setRequestHandler(...)` once per method they own
 * (tools/list AND tools/call from `.tool()`, resources/list AND
 * resources/read from `.resource()`, and so on) -- confirmed by reading
 * @modelcontextprotocol/sdk's own compiled source (server/mcp.js). Wrapping
 * here sees everything: SDK-level rejections (unknown tool, bad args) AND
 * whatever the handler itself decided, in one interception point.
 *
 * EVERY method gets a span, not an allowlisted subset. This was originally
 * narrower (tools/call and prompts/get only) -- wrong, measured against
 * Python's own behavior: Python's middleware has no per-method allowlist at
 * all (grepped middleware.py directly), because the underlying Python MCP
 * SDK creates a span for every dispatched method itself and mcpobs there
 * only ANNOTATES whichever span already exists. The TS SDK creates no spans
 * on its own, so "instrument everything" here is what parity with Python
 * actually requires, not an allowlist that happened to cover the two
 * methods someone thought to type in first. A client's `tools/list` call
 * that silently fails, or takes 4 seconds, was previously invisible.
 *
 * The schema argument has no string method name on it directly -- it is a
 * zod object schema. `schema.shape.method.value` reads the literal method
 * string back out (verified at runtime against the installed SDK:
 * `CallToolRequestSchema.shape.method.value === 'tools/call'`).
 */

import type { Span, Tracer } from "@opentelemetry/api";
import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { classifyResult, firstText, FailureKind } from "./classifier.js";
import { capturePayload } from "./payload.js";
import { getTransport } from "./transport.js";

/**
 * Attribute names are NOT invented here -- they are the exact keys
 * normalizer/taxonomy.py already reads, verified by reading that file
 * directly rather than assumed:
 *
 *   mcp.method.name        REQUIRED -- classify() returns early to
 *                           transport-auth handling if this is absent, so a
 *                           span missing it is invisible to the taxonomy
 *                           entirely, not just unclassified.
 *   mcpobs.failure.kind     "the helper's classification wins when present"
 *                           -- taxonomy.py returns this AS-IS, no
 *                           server-side change needed for JS spans to land
 *                           in the same categories Python spans do.
 *   mcpobs.failure.kind.version, mcpobs.result.type, mcpobs.cancelled
 *                           the same HELPER_MARKERS taxonomy.py checks to
 *                           report `source() == "helper"` rather than
 *                           "span" -- stamping them is what makes this SDK
 *                           show up as a first-class classifier, not a
 *                           bare span the taxonomy has to guess about.
 */
const ATTR = {
  MCP_METHOD: "mcp.method.name",
  TOOL_NAME: "gen_ai.tool.name",
  PROMPT_NAME: "gen_ai.prompt.name",
  RESOURCE_URI: "mcp.resource.uri",
  TRANSPORT: "network.transport",
  FAILURE_KIND: "mcpobs.failure.kind",
  FAILURE_KIND_VERSION: "mcpobs.failure.kind.version",
  FAILURE_KIND_SOURCE: "mcpobs.failure.kind_source",
  FAILURE_DETAIL: "mcpobs.failure.detail",
  RESULT_TYPE: "mcpobs.result.type",
  CANCELLED: "mcpobs.cancelled",
  REQUEST: "gen_ai.tool.call.arguments",
  RESPONSE: "gen_ai.tool.call.result",
  REQUEST_SIZE: "mcpobs.request.size",
  RESPONSE_SIZE: "mcpobs.response.size",
} as const;

const CLASSIFIER_VERSION = 1;
const MAX_FAILURE_DETAIL = 512;

/** Every method gets a span (see module docstring for why this is not an
 * allowlist). This map only supplies the OPTIONAL "which tool/prompt/
 * resource" sub-label for the methods that have one -- a method absent from
 * here still gets instrumented, just named by its bare method string
 * (`tools/list`, `initialize`, `ping`, ...), matching exactly what
 * `resource_uri`/`tool_name`-less methods look like in Python today. */
const NAMED_ARG: Record<string, { attr: string; argName: string }> = {
  "tools/call": { attr: ATTR.TOOL_NAME, argName: "name" },
  "prompts/get": { attr: ATTR.PROMPT_NAME, argName: "name" },
  "resources/read": { attr: ATTR.RESOURCE_URI, argName: "uri" },
};
const NO_NAMED_ARG = { attr: "", argName: "" };

export interface DispatchOptions {
  tracer: Tracer;
  captureErrorDetail: boolean;
  capturePayloads: boolean;
}

let patched = false;

export function patchDispatch(opts: DispatchOptions): void {
  if (patched) return;
  patched = true;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const proto = Server.prototype as any;
  const original: typeof proto.setRequestHandler = proto.setRequestHandler;

  proto.setRequestHandler = function patchedSetRequestHandler(
    schema: unknown,
    handler: (request: unknown, extra: unknown) => unknown,
    ...rest: unknown[]
  ) {
    const method = (schema as { shape?: { method?: { value?: string } } })?.shape?.method?.value;
    if (!method) {
      // Not a request schema this SDK's zod shape convention applies to --
      // has never been observed in practice, but original.call is always
      // the safe fallback for anything this hook doesn't understand.
      return original.call(this, schema, handler, ...rest);
    }
    const info = NAMED_ARG[method] ?? NO_NAMED_ARG;

    const wrapped = async (request: unknown, extra: unknown) => {
      return runWithSpan(opts, method, info, request, extra, () => handler(request, extra));
    };
    return original.call(this, schema, wrapped, ...rest);
  };
}

/** Test seam: patch state is process-wide (the prototype is shared). */
export function resetDispatch(): void {
  const proto = Server.prototype as unknown as { setRequestHandler?: unknown };
  void proto;
  patched = false;
}

async function runWithSpan(
  opts: DispatchOptions,
  method: string,
  info: { attr: string; argName: string },
  request: unknown,
  extra: unknown,
  invoke: () => unknown,
): Promise<unknown> {
  const params = (request as { params?: Record<string, unknown> })?.params ?? {};
  const name = typeof params[info.argName] === "string" ? (params[info.argName] as string) : "";
  const spanName = name ? `${method} ${name}` : method;

  const span = opts.tracer.startSpan(spanName);
  span.setAttribute(ATTR.MCP_METHOD, method);
  if (name) span.setAttribute(info.attr, name);
  const transport = getTransport();
  if (transport) span.setAttribute(ATTR.TRANSPORT, transport);

  const requestId = (extra as { requestId?: unknown })?.requestId;
  if (opts.capturePayloads) {
    setCapturedPayload(span, ATTR.REQUEST, ATTR.REQUEST_SIZE, {
      jsonrpc: "2.0",
      id: requestId ?? null,
      method,
      params,
    });
  }

  return context.with(trace.setSpan(context.active(), span), async () => {
    try {
      const result = await invoke();
      finish(span, opts, classifyResult(result), result, requestId);
      return result;
    } catch (err) {
      finish(span, opts, classifyResult(undefined, err), err, requestId);
      throw err;
    }
  });
}

function finish(
  span: Span,
  opts: DispatchOptions,
  classification: ReturnType<typeof classifyResult>,
  resultOrError: unknown,
  requestId: unknown,
): void {
  span.setAttribute(ATTR.FAILURE_KIND, classification.kind);
  span.setAttribute(ATTR.FAILURE_KIND_VERSION, CLASSIFIER_VERSION);
  span.setAttribute(ATTR.FAILURE_KIND_SOURCE, classification.source);

  if (opts.capturePayloads) {
    setCapturedPayload(span, ATTR.RESPONSE, ATTR.RESPONSE_SIZE, {
      jsonrpc: "2.0",
      id: requestId ?? null,
      result: resultOrError,
    });
  }

  if (classification.kind !== FailureKind.OK) {
    span.setStatus({ code: SpanStatusCode.ERROR });
    if (opts.captureErrorDetail) {
      const text = firstText(resultOrError).slice(0, MAX_FAILURE_DETAIL);
      if (text) span.setAttribute(ATTR.FAILURE_DETAIL, text);
    }
  } else {
    span.setStatus({ code: SpanStatusCode.OK });
  }
  span.end();
}

function setCapturedPayload(
  span: Span,
  previewAttribute: string,
  sizeAttribute: string,
  value: unknown,
): void {
  const captured = capturePayload(value);
  if (captured.preview) span.setAttribute(previewAttribute, captured.preview);
  span.setAttribute(sizeAttribute, captured.size);
}

export { ATTR as SpanAttributes };
