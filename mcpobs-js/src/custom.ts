/**
 * Customer-defined attributes on the current span.
 *
 * Mirrors mcpobs/custom.py (the Python SDK) exactly, including the reasoning:
 * this SDK's own attributes (`mcpobs.failure.kind` and friends, dispatch.ts)
 * are trusted unconditionally by the platform's classifier
 * (`normalizer/taxonomy.py` reads `mcpobs.failure.kind` straight off an
 * incoming span with no verification, by design -- any language's SDK can
 * classify correctly with zero server-side changes, as long as it stamps the
 * exact right attribute names). A customer calling the raw OTel API from
 * their own tool handler could, by accident, set that same key and silently
 * corrupt classification for that call.
 *
 * So this is not a plain passthrough to `setAttribute`. Every custom key is
 * namespaced under `mcpobs.custom.<key>` before it reaches the span -- a
 * customer who chooses `failure.kind` as their own key ends up on
 * `mcpobs.custom.failure.kind`, nowhere near the real one, with no way to
 * collide even by choosing an adversarial name.
 */

import { trace } from "@opentelemetry/api";

export const CUSTOM_ATTRIBUTE_PREFIX = "mcpobs.custom.";

/**
 * Attach a customer-defined attribute to the current span.
 *
 * Call this from inside your own tool handler -- mcpobs's span is active for
 * the duration of the call (dispatch.ts's `runWithSpan` runs the handler
 * inside it), so the attribute lands on the same span this call's other
 * telemetry does.
 *
 * Stored as `mcpobs.custom.<key>`, never the raw key (see module docstring).
 * A call with nothing active (outside any tool handler) is a silent no-op --
 * `getActiveSpan()` returns `undefined`, and the optional call below no-ops
 * the same way setting an attribute on any other non-recording span would.
 *
 * Never raises, matching mcpobs/custom.py's (Python) explicit guarantee --
 * called from a customer's request path, and an observability call that can
 * break the request it is observing has failed at the one thing it must
 * not do. `@opentelemetry/api`'s own Span.setAttribute is documented not to
 * throw, but that guarantee is the underlying SDK's, not this function's;
 * a non-standard Tracer/Span implementation (a different OTel-compatible
 * package, a future API version) is not this SDK's to trust blindly.
 */
export function setCustomAttribute(key: string, value: string): void {
  try {
    trace.getActiveSpan()?.setAttribute(`${CUSTOM_ATTRIBUTE_PREFIX}${key}`, value);
  } catch {
    // Swallowed on purpose -- see the "never raises" note above.
  }
}
