/**
 * Downstream instrumentation: HTTP (default), databases/caches (opt-in).
 *
 * HTTP DETAIL CAPTURE.
 *
 *   REQUEST body   -- capturable, with a genuine complication Python did not
 *                     have. httpx hands its hook an already-serialized
 *                     `ByteStream`; Node's `http.ClientRequest` is a
 *                     WRITABLE stream instead -- the body is WRITTEN to it by
 *                     the caller (Axios, for a downstream server's own API
 *                     calls), not read from a buffered property. Captured here by
 *                     patching `.write`/`.end` on the ONE request instance
 *                     the hook was called with (never the prototype -- no
 *                     global side effect on requests this SDK never sees),
 *                     accumulating chunks up to a cap, exactly mirroring
 *                     `_request_body`'s binary/size-only fallback.
 *
 *   REQUEST headers, RESPONSE headers -- allow-listed (payload.ts's
 *                     `captureHeaders`, the exact list KEEP_HEADERS names).
 *
 *   RESPONSE body  -- CAPTURED in Node, deliberately NOT ported as a gap from
 *                     Python. mcpobs/http.py's docstring documents httpx's
 *                     real constraint: `HTTPXClientInstrumentor` wraps the
 *                     TRANSPORT, so its span ends when `handle_async_request`
 *                     returns, before `Client.send` ever reads the body --
 *                     by the time a body exists, the span is already
 *                     exported, and attributes on an ended span are dropped
 *                     in silence (this is exactly why
 *                     `normalizer/schema/011_drop_http_response_body.sql`
 *                     dropped an earlier attempt at this column: it was
 *                     ALWAYS empty, and a permanently-empty column is worse
 *                     than a missing one).
 *
 *                     That constraint does not hold here. Measured directly,
 *                     twice -- once against an instant same-process response,
 *                     once against a server that drip-feeds its body over
 *                     500ms to simulate real network latency -- the Node
 *                     CLIENT span stays `isRecording() === true` all the way
 *                     through the response stream's `'end'` event, not just
 *                     at `responseHook` time. `@opentelemetry/instrumentation-
 *                     http` does not close the span until the response
 *                     finishes, unlike httpx's transport-wrapping approach.
 *                     A passive `response.on('data'/'end')` listener,
 *                     attached inside `responseHook` alongside whatever
 *                     listener the real caller (axios) attaches, costs
 *                     nothing extra on the wire -- Node broadcasts 'data' to
 *                     every registered listener, so this never consumes
 *                     bytes the application would otherwise receive.
 *                     Verified against a real client in both probes: the
 *                     caller's own response handling still resolved
 *                     correctly with the full body in both cases.
 *
 *                     What keeps this bounded and safe, none of which
 *                     request-body capture needed:
 *                       - Content-type pre-filtered BEFORE buffering a byte:
 *                         `image/*`, `video/*`, `audio/*`, fonts, archives,
 *                         `application/octet-stream`, `application/pdf` are
 *                         skipped outright -- no point paying the
 *                         buffer-then-discard cost the binary-detection
 *                         fallback already accepts for genuinely unknown
 *                         bodies, and file/image downloads are common enough
 *                         in MCP tool responses to be worth skipping up
 *                         front.
 *                       - Capped at MAX_RESPONSE_BODY_BYTES on the RAW bytes
 *                         as they arrive -- same accumulate-with-truncation
 *                         pattern as the request-body path, but a LARGER cap
 *                         than request bodies get (see that constant's own
 *                         comment: a routine "list projects" response ran
 *                         past the request-body cap and got cut off into
 *                         invalid JSON, silently losing tree rendering for
 *                         exactly the common, worth-seeing case). Bounded
 *                         regardless of what decompression later does to
 *                         that buffer.
 *                       - Decompressed via `content-encoding` (gzip/br/
 *                         deflate -- axios sends `Accept-Encoding` for all
 *                         three, and most real APIs honour it) using Node's
 *                         built-in `zlib`, inside a try/catch. A truncated
 *                         compressed stream often fails to decompress (gzip
 *                         is not safely truncatable mid-block); on failure
 *                         this records `[compressed, N bytes, could not
 *                         decode -- likely truncated]` rather than losing
 *                         the capture or throwing -- the same category of
 *                         honest fallback the binary-detection branch
 *                         already uses.
 *
 * DATABASES/CACHES are deliberately NOT wired by `instrument()` or by
 * `instrumentDownstream()` -- same reasoning as Python's downstream.py:
 * "patching a customer's database driver as a side effect of 'observe my
 * MCP server' is not a thing to do quietly." `instrumentDatabases()` is a
 * separate, explicit call.
 *
 * Python discovers instrumentors via `importlib.metadata.entry_points()` --
 * every `pip install opentelemetry-instrumentation-X` package registers
 * itself there, so "install the package" IS the whole integration, with
 * zero mcpobs code change. Node/npm has no equivalent formal registry.
 * `@opentelemetry/auto-instrumentations-node` is the closest thing the JS
 * OTel ecosystem has -- a curated bundle exposing ~35 instrumentors,
 * including Postgres/MySQL/Redis/MongoDB, each safe to enable even when its
 * target library is never installed (they hook Node's module loader and
 * simply never fire if the target is never required).
 *
 * Kept an OPTIONAL peer dependency, NOT a regular one: installing it pulls
 * in ~35 packages, and forcing that onto every `npm install mcpobs` --
 * including a consumer that uses NONE of it -- is exactly the weight
 * Python's SDK refuses to impose ("we never pull in
 * psycopg, redis or pymysql on their behalf"). `instrumentDatabases()`
 * imports it dynamically and reports a clear, actionable outcome if it was
 * never installed, rather than throwing.
 */

import type { ClientRequest, IncomingMessage } from "node:http";
import { gunzipSync, brotliDecompressSync, inflateSync } from "node:zlib";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { HttpInstrumentation } from "@opentelemetry/instrumentation-http";
import type { Instrumentation } from "@opentelemetry/instrumentation";
import type { Span } from "@opentelemetry/api";
import { captureHeaders, capturePreview } from "./payload.js";

const ATTR = {
  REQUEST_BODY: "mcpobs.http.request.body",
  REQUEST_HEADERS: "mcpobs.http.request.headers",
  RESPONSE_HEADERS: "mcpobs.http.response.headers",
  RESPONSE_BODY: "mcpobs.http.response.body",
} as const;

/** Node buffers in memory regardless; this bounds how much of a large
 * upload this SDK holds onto at once. Matches the spirit of Python's
 * MAX_CHARS cap on the decoded/rendered side. Applied to the RAW bytes as
 * they arrive, so the bound holds regardless of what decompression later
 * does to that buffer. */
const MAX_BODY_BYTES = 64 * 1024;

/** Response bodies get a LARGER cap than request bodies, deliberately, not
 * copied from MAX_BODY_BYTES out of habit. Measured directly against a real
 * API: a routine "list projects" response ran past 64 KiB and got cut off
 * mid-object, which made it invalid JSON -- `JSON.parse` correctly refuses
 * truncated JSON, so the query UI silently lost the tree/syntax-highlighted
 * rendering for exactly the common case (a paginated list) most worth
 * seeing structured. Requests are things a customer's code chose to write;
 * responses are whatever the downstream API decided to send back, and
 * list/search endpoints routinely return more than a customer's own request
 * bodies do. Still bounded -- this is a larger cap, not an unbounded one --
 * and content-type filtering already rules out the genuinely large case
 * (file/image downloads) before a single byte is buffered. */
const MAX_RESPONSE_BODY_BYTES = 512 * 1024;

/** Skipped before a single byte is buffered -- no point paying the
 * buffer-then-discard cost the binary-detection fallback below already
 * accepts for genuinely unknown bodies, and file/image downloads are common
 * enough in MCP tool responses to be worth ruling out up front. Prefix
 * match on the media TYPE (before any `;charset=...` suffix). */
const BINARY_CONTENT_TYPE_PREFIXES = [
  "image/", "video/", "audio/", "font/",
  "application/octet-stream", "application/pdf", "application/zip",
  "application/gzip", "application/x-tar", "application/vnd.",
];

function isBinaryContentType(contentType: string): boolean {
  const mediaType = contentType.split(";", 1)[0]!.trim().toLowerCase();
  return BINARY_CONTENT_TYPE_PREFIXES.some((prefix) => mediaType.startsWith(prefix));
}

/**
 * Decode a captured buffer to a preview string, one implementation shared by
 * request- and response-body capture so the binary/truncation rules cannot
 * drift between the two. A round-trip check: invalid UTF-8 decodes to
 * replacement characters rather than throwing, so decode-then-reencode is
 * what actually detects binary content -- matching Python's decode-and-catch,
 * which has a real UnicodeDecodeError to catch and Node does not.
 */
function decodeBodyText(body: Buffer, truncated: boolean): string {
  let text: string;
  try {
    text = body.toString("utf-8");
    if (Buffer.from(text, "utf-8").length !== body.length) {
      text = `[binary, ${body.length} bytes]`;
    }
  } catch {
    text = `[binary, ${body.length} bytes]`;
  }
  if (truncated && !text.startsWith("[binary")) text += "...[truncated]";
  return text;
}

export interface DownstreamOptions {
  /** Capture request headers, response headers, and (best-effort) request
   * bodies on outbound HTTP spans -- allow-listed headers only, `authorization`
   * and `cookie` never read at all. Its OWN opt-in, separate from turning on
   * HTTP tracing at all -- matches instrument_httpx() being a distinct call
   * from base instrumentation in the Python SDK (D60). Default false. */
  captureHttpDetail?: boolean;
}

let registered = false;

export function instrumentDownstream(options: DownstreamOptions = {}): void {
  if (registered) return;
  registered = true;

  const captureDetail = options.captureHttpDetail ?? false;
  registerInstrumentations({
    instrumentations: [
      new HttpInstrumentation(
        captureDetail
          ? {
              requestHook: (span, request) => {
                if (!isClientRequest(request)) return; // IncomingMessage: this is a SERVER span, not the outbound call
                captureRequestDetail(span, request);
              },
              responseHook: (span, response) => {
                const incoming = response as IncomingMessage;
                const headers = incoming.headers;
                if (!headers) return;
                const text = captureHeaders(headers as Record<string, unknown>);
                if (text) span.setAttribute(ATTR.RESPONSE_HEADERS, text);
                captureResponseDetail(span, incoming);
              },
            }
          : {},
      ),
    ],
  });
}

function isClientRequest(req: ClientRequest | IncomingMessage): req is ClientRequest {
  return typeof (req as ClientRequest).write === "function" && "method" in req;
}

/**
 * Buffers what THIS ONE request writes, up to MAX_BODY_BYTES, then decodes
 * and stamps it once the request ends. Patches the INSTANCE, not
 * `ClientRequest.prototype` -- scoped to exactly the request the hook fired
 * for, with zero effect on any request this SDK was never asked about.
 */
function captureRequestDetail(span: import("@opentelemetry/api").Span, request: ClientRequest): void {
  const headersText = captureHeaders(request.getHeaders() as Record<string, unknown>);
  if (headersText) span.setAttribute(ATTR.REQUEST_HEADERS, headersText);

  const chunks: Buffer[] = [];
  let bytes = 0;
  let truncated = false;

  const originalWrite = request.write.bind(request);
  const originalEnd = request.end.bind(request);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  request.write = ((chunk: any, ...rest: any[]) => {
    accumulate(chunk, typeof rest[0] === "string" ? rest[0] : undefined);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (originalWrite as any)(chunk, ...rest);
  }) as typeof request.write;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  request.end = ((chunk?: any, ...rest: any[]) => {
    if (chunk !== undefined && typeof chunk !== "function") {
      accumulate(chunk, typeof rest[0] === "string" ? rest[0] : undefined);
    }
    finish();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (originalEnd as any)(chunk, ...rest);
  }) as typeof request.end;

  // `encoding` is write()/end()'s OWN optional second argument
  // (`write(chunk, encoding?, callback?)`) -- a string chunk written as
  // `request.write(base64Text, "base64")` is base64 TEXT, not UTF-8 text
  // that happens to look like base64. Ignoring this argument (as this
  // function used to) decoded it as UTF-8 regardless, producing a garbled
  // preview for any caller writing a non-UTF-8-encoded string -- the real
  // bytes sent over the wire were always correct, since `encoding` was still
  // forwarded to `originalWrite` unchanged; only the CAPTURED preview was
  // wrong. `Buffer.isEncoding` guards against a bogus/unrecognized string
  // landing where a real encoding was expected (`Buffer.from` throws on one).
  function accumulate(chunk: unknown, encoding?: string): void {
    if (bytes >= MAX_BODY_BYTES || chunk === undefined) return;
    const buf = Buffer.isBuffer(chunk)
      ? chunk
      : typeof chunk === "string"
        ? Buffer.from(chunk, encoding !== undefined && Buffer.isEncoding(encoding) ? encoding : "utf-8")
        : null;
    if (!buf) return; // not a body chunk this SDK knows how to read
    if (bytes + buf.length > MAX_BODY_BYTES) {
      chunks.push(buf.subarray(0, MAX_BODY_BYTES - bytes));
      bytes = MAX_BODY_BYTES;
      truncated = true;
    } else {
      chunks.push(buf);
      bytes += buf.length;
    }
  }

  function finish(): void {
    if (!span.isRecording()) return;
    if (chunks.length === 0) return;
    const text = decodeBodyText(Buffer.concat(chunks), truncated);
    span.setAttribute(ATTR.REQUEST_BODY, capturePreview(text));
  }
}

/**
 * Response bodies ARE capturable in Node -- see the module docstring for the
 * measurement that overturned the Python-derived assumption that they
 * aren't. A passive listener alongside whatever listener the real caller
 * (axios) attaches: Node broadcasts 'data' to every registered listener, so
 * this never consumes bytes the application would otherwise receive.
 *
 * Skips known-binary content types before buffering anything, caps the RAW
 * bytes at MAX_RESPONSE_BODY_BYTES (see that constant for why it is NOT
 * the same cap request bodies use), then decompresses based on
 * `content-encoding` -- the one piece request bodies never needed, since
 * axios both sends `Accept-Encoding` and receives compressed responses from
 * most real APIs.
 *
 * `content-encoding` is read EAGERLY, synchronously, right here -- not
 * lazily inside the 'end' handler below. Measured directly against a real
 * axios request to a real Brotli-compressed endpoint: axios's own Node
 * adapter (`lib/adapters/http.js`) sets up its OWN decompression pipe for
 * the SAME response and does `delete res.headers['content-encoding']`
 * immediately after, "to not confuse downstream operations" (axios's own
 * comment). Since `responseHook` fires from OTel's listener, registered
 * before axios attaches its own, reading the header here happens before
 * that delete; reading it again later, inside the async 'end' callback
 * (after axios has already run), silently found nothing -- exactly the bug
 * this comment exists to stop from coming back.
 */
function captureResponseDetail(span: Span, response: IncomingMessage): void {
  const contentType = String(response.headers["content-type"] ?? "");
  if (isBinaryContentType(contentType)) return;
  const contentEncoding = String(response.headers["content-encoding"] ?? "");

  const chunks: Buffer[] = [];
  let bytes = 0;
  let truncated = false;

  response.on("data", (chunk: unknown) => {
    if (bytes >= MAX_RESPONSE_BODY_BYTES) return;
    const buf = Buffer.isBuffer(chunk) ? chunk : typeof chunk === "string" ? Buffer.from(chunk) : null;
    if (!buf) return;
    if (bytes + buf.length > MAX_RESPONSE_BODY_BYTES) {
      chunks.push(buf.subarray(0, MAX_RESPONSE_BODY_BYTES - bytes));
      bytes = MAX_RESPONSE_BODY_BYTES;
      truncated = true;
    } else {
      chunks.push(buf);
      bytes += buf.length;
    }
  });

  response.on("end", () => {
    try {
      if (!span.isRecording()) return;
      if (chunks.length === 0) return;
      const raw = Buffer.concat(chunks);
      const decoded = decompressBody(raw, truncated, contentEncoding);
      span.setAttribute(ATTR.RESPONSE_BODY, capturePreview(decoded));
    } catch {
      // Never let capture failure surface into the real response consumer --
      // the response has already been delivered to the application by now.
    }
  });
}

/**
 * A truncated compressed stream often fails to decompress -- gzip/brotli are
 * not safely truncatable mid-block. Falls back to an honest marker rather
 * than losing the capture or throwing, the same category of fallback
 * decodeBodyText already uses for genuinely binary content.
 */
function decompressBody(raw: Buffer, truncated: boolean, contentEncoding: string): string {
  const encoding = contentEncoding.trim().toLowerCase();
  if (!encoding || encoding === "identity") return decodeBodyText(raw, truncated);

  try {
    const inflated =
      encoding === "gzip" || encoding === "x-gzip" ? gunzipSync(raw)
      : encoding === "br" ? brotliDecompressSync(raw)
      : encoding === "deflate" ? inflateSync(raw)
      : null;
    if (inflated === null) return `[${encoding}-encoded, ${raw.length} bytes, unsupported encoding]`;
    return decodeBodyText(inflated, truncated);
  } catch {
    return truncated
      ? `[${encoding}-compressed, ${raw.length} bytes, could not decode -- likely truncated]`
      : `[${encoding}-compressed, ${raw.length} bytes, could not decode]`;
  }
}

/**
 * Postgres, MySQL, Redis -- the exact downstream kinds this platform's own
 * demo server exercises (demo_server/backends.py: Postgres, MySQL, Redis,
 * HTTP), not an arbitrary subset of the ~35 the bundle ships. MongoDB
 * included too: common enough in the MCP server ecosystem that leaving it
 * out for no reason would just be an arbitrary gap.
 *
 * Filtered by `instrumentationName` AFTER construction, rather than passed
 * as a disable-map of ~30 string keys to `getNodeAutoInstrumentations()` --
 * fewer places to typo a package name, and a new instrumentor added to a
 * future version of the bundle is excluded by default (safe) rather than
 * silently enabled because nobody added it to a deny-list here.
 */
const DOWNSTREAM_ALLOW_LIST = new Set([
  "@opentelemetry/instrumentation-pg",
  "@opentelemetry/instrumentation-mysql",
  "@opentelemetry/instrumentation-mysql2",
  "@opentelemetry/instrumentation-redis",
  "@opentelemetry/instrumentation-ioredis",
  "@opentelemetry/instrumentation-mongodb",
]);

export type DatabaseInstrumentationOutcome = "instrumented" | "skipped: excluded" | string;
export interface DatabaseInstrumentationReport {
  [instrumentationName: string]: DatabaseInstrumentationOutcome;
}

let databasesRegistered = false;

/**
 * Turns on database/cache instrumentation for whichever of Postgres, MySQL,
 * Redis and MongoDB the customer's server actually uses. A REPORT, not
 * void -- same reasoning as Python's instrument_downstream(): a call that
 * patches an unknown set of libraries and says nothing is not something
 * anyone should be comfortable putting in a production server.
 *
 * Never throws. Requires `@opentelemetry/auto-instrumentations-node` to be
 * installed BY THE CONSUMER -- not a dependency of this package, see the
 * module docstring for why. Missing is reported, not fatal: an observability
 * library that prevents a server from booting has done more damage than the
 * telemetry was worth (the same line Python's own docstring draws).
 */
export async function instrumentDatabases(
  exclude: Iterable<string> = [],
): Promise<DatabaseInstrumentationReport> {
  if (databasesRegistered) return { "": "already instrumented" };

  let getNodeAutoInstrumentations: (
    config?: Record<string, { enabled?: boolean }>,
  ) => Instrumentation[];
  try {
    // Dynamic and by STRING, not a static top-level import: a static import
    // would require the package to exist at build/typecheck time for every
    // consumer, defeating the entire point of keeping it optional.
    const mod = await import("@opentelemetry/auto-instrumentations-node");
    getNodeAutoInstrumentations = mod.getNodeAutoInstrumentations;
  } catch (err) {
    return {
      "@opentelemetry/auto-instrumentations-node":
        `not installed -- run: npm install @opentelemetry/auto-instrumentations-node ` +
        `(${(err as Error).message})`,
    };
  }

  const skip = new Set(exclude);
  const outcomes: DatabaseInstrumentationReport = {};
  const selected: Instrumentation[] = [];

  for (const instrumentation of getNodeAutoInstrumentations()) {
    const name = instrumentation.instrumentationName;
    if (!DOWNSTREAM_ALLOW_LIST.has(name)) continue; // not a database/cache -- express, grpc, aws-sdk, etc.
    if (skip.has(name)) {
      outcomes[name] = "skipped: excluded";
      continue;
    }
    try {
      instrumentation.enable();
      selected.push(instrumentation);
      outcomes[name] = "instrumented";
    } catch (err) {
      // The common case is a version the instrumentor does not support --
      // information, not a failure, so it is reported rather than thrown.
      outcomes[name] = `skipped: ${(err as Error).message}`;
    }
  }

  databasesRegistered = true;
  return outcomes;
}

/** Names of the database/cache instrumentations this SDK would turn on, so
 * a customer can check what WOULD happen before calling
 * instrumentDatabases() -- mirrors Python's `available()` for the same
 * "silently did nothing" vs "silently did more than expected" reason. */
export function availableDatabaseInstrumentations(): string[] {
  return Array.from(DOWNSTREAM_ALLOW_LIST).sort();
}
