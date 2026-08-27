"""Customer-defined attributes on the current span.

Both the classifier's own attributes (`mcpobs.failure.kind` and friends,
classifier.py) and this SDK's redaction pipeline live under a `mcpobs.`
namespace the platform trusts unconditionally --
`normalizer/taxonomy.py`'s `classify()` reads `mcpobs.failure.kind` straight
off an incoming span with no verification, by design (any language's SDK can
classify correctly with zero server-side changes, as long as it stamps the
exact right attribute names). A customer calling the raw OTel API from their
own tool handler could, by accident, set that same key and silently corrupt
classification for that call.

So this is not a plain passthrough to `set_attribute`. Every custom key is
namespaced under `mcpobs.custom.<key>` before it reaches the span -- a
customer who chooses `failure.kind` as their own key ends up on
`mcpobs.custom.failure.kind`, nowhere near the real one, with no way to
collide even by choosing an adversarial name.
"""

from __future__ import annotations

import logging

from opentelemetry.trace import get_current_span

log = logging.getLogger(__name__)

CUSTOM_ATTRIBUTE_PREFIX = "mcpobs.custom."


def set_custom_attribute(key: str, value: str) -> None:
    """Attach a customer-defined attribute to the current span.

    Call this from inside your own tool handler -- mcpobs's span is active
    for the duration of the call (middleware.py runs the handler inside it),
    so the attribute lands on the same span this call's other telemetry does.

    Stored as `mcpobs.custom.<key>`, never the raw key (see module
    docstring). Never raises: called from a customer's request path, and an
    observability call that can break the request it is observing has failed
    at the one thing it must not do. A call with nothing active (outside any
    tool handler) is a silent no-op, same as setting an attribute on any
    other non-recording span.
    """
    try:
        span = get_current_span()
        if not span.is_recording():
            return
        span.set_attribute(f"{CUSTOM_ATTRIBUTE_PREFIX}{key}", value)
    except Exception as exc:  # noqa: BLE001
        log.debug("set_custom_attribute skipped: %s", exc)
