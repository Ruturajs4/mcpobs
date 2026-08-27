/**
 * Payload capture and redaction. Off by default (`capturePayloads: false`),
 * mirroring the Python SDK's `mcpobs/payload.py` -- capturing arguments and
 * results is a deliberate opt-in, not a thing that happens because
 * instrumentation exists.
 *
 * Redacted BEFORE storage, not before display: a secret that reaches the
 * span cannot be recalled later. Same reasoning as the Python module's own
 * comment on this.
 */

const MAX_PREVIEW = 2_000;

const SECRET_KEY_PATTERN = /(api[_-]?key|token|password|secret|authorization|access[_-]?key)/i;

function redactValue(key: string, value: unknown): unknown {
  if (SECRET_KEY_PATTERN.test(key)) return "[redacted]";
  if (typeof value === "string") return redactString(value);
  if (value instanceof Error) return asDict(value);
  if (Array.isArray(value)) return value.map((item) => redactValue(key, item));
  if (value && typeof value === "object") return redactObject(value as Record<string, unknown>);
  return value;
}

/**
 * Best-effort for a model rather than the sealed wire form -- mirrors
 * mcpobs/payload.py's `_as_dict` docstring exactly, for the same reason.
 *
 * Error is the concrete case that motivated this: `message` and `stack` are
 * OWN but NON-ENUMERABLE properties, so `JSON.stringify(new Error("x"))`
 * silently produces `"{}"` -- measured directly, not assumed. A thrown
 * exception reaching payload capture (the tools/call exception path, or a
 * customer's own tool returning one) would have its actual text discarded
 * without this. `stack` is deliberately excluded, not just missed -- a stack
 * trace is exactly the kind of internal-structure/file-path detail this
 * project has been conservative about letting leave the process elsewhere
 * (docs/decisions.md's payload-capture boundaries make the same call).
 */
function asDict(err: Error): Record<string, unknown> {
  return { name: err.name, message: err.message };
}

// Recognizable secret FORMATS embedded in an otherwise plain string -- the
// only redaction an opaque string (an HTTP body, a URL) can get, since there
// is no key name to check. Matches mcpobs/payload.py's SENSITIVE_VALUES
// exactly, patterns and all: a raw request body with a `password` field does
// NOT get key-based redaction here, deliberately, same as Python (D56) --
// key-based redaction only applies to structured objects (tool args/results),
// where a real key name exists to check.
const SENSITIVE_VALUE_PATTERNS: readonly RegExp[] = [
  /\bBearer\s+[\w.~+/=-]{8,}/gi,
  /\bsk-[A-Za-z0-9]{16,}/g, // OpenAI-style
  /\bgh[pousr]_[A-Za-z0-9]{16,}/g, // GitHub
  /\bAKIA[0-9A-Z]{16}\b/g, // AWS access key id
  /\beyJ[\w-]{10,}\.[\w-]{10,}\./g, // JWT
  /([?&](?:api[_-]?key|token|access[_-]?key)=)[^&\s]+/gi,
];

function redactString(value: string): string {
  let out = value;
  for (const pattern of SENSITIVE_VALUE_PATTERNS) {
    out = out.replace(pattern, (match, group1?: string) => (group1 ? `${group1}[redacted]` : "[redacted]"));
  }
  return out;
}

function redactObject(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj)) {
    out[key] = redactValue(key, value);
  }
  return out;
}

export interface CapturedPayload {
  preview: string;
  size: number;
}

export function capturePayload(value: unknown): CapturedPayload {
  let redacted: unknown;
  try {
    // Always through redactValue, never redactObject directly -- redactValue
    // already falls through to redactObject for plain objects on its own,
    // and going through it uniformly is what makes the Error special-case
    // apply at the TOP LEVEL too, not just when an Error is nested inside a
    // wrapper object (dispatch.ts's actual usage; a future direct call with
    // a bare Error must not regress the same bug for lack of this).
    redacted = redactValue("", value);
  } catch {
    return { preview: "", size: 0 };
  }
  const serialized = typeof redacted === "string" ? redacted : JSON.stringify(redacted);
  const text = typeof serialized === "string" ? serialized : "";
  return {
    preview: text.length > MAX_PREVIEW ? `${text.slice(0, MAX_PREVIEW)}...[truncated]` : text,
    size: text.length,
  };
}

export function capturePreview(value: unknown): string {
  return capturePayload(value).preview;
}

/**
 * ALLOW-listed HTTP headers, matching mcpobs/http.py's KEEP_HEADERS exactly
 * -- an allow-list, not a deny-list, on purpose. A deny-list is wrong by
 * default: it captures every header nobody thought of, and the one nobody
 * thought of is where the next bespoke `x-acme-session` credential lives.
 * `authorization` and `cookie` are never READ at all, not merely redacted.
 */
const KEEP_HEADERS = new Set([
  "content-type",
  "content-length",
  "accept",
  "accept-encoding",
  "user-agent",
  "x-request-id",
  "traceparent",
  "retry-after",
  "cache-control",
  "location",
  "server",
]);

/**
 * Renders only the allow-listed headers, through the same redaction/preview
 * pipeline as everything else -- one implementation, so header capture and
 * MCP payload capture cannot drift into different redaction rules (same
 * reasoning as http.py's module docstring).
 */
export function captureHeaders(headers: Record<string, unknown> | undefined | null): string {
  if (!headers) return "";
  const kept: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(headers)) {
    if (KEEP_HEADERS.has(key.toLowerCase())) kept[key] = value;
  }
  if (Object.keys(kept).length === 0) return "";
  return capturePreview(kept);
}
