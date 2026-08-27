"""Make long-running work visible WHILE it runs (closes DF-20 and DF-21).

THE ONE PROBLEM BEHIND BOTH
    A span is exported when it ENDS. So anything modelled as a single
    long-lived span reports nothing until it is over:

      * a `subscriptions/listen` stream open for six hours produced one span,
        six hours late, carrying three attributes;
      * a tool that called `report_progress()` three times produced one span of
        155ms and no record that progress was ever reported.

    In both cases the operator's actual question -- "where is it now?" -- was
    unanswerable, which is the question those protocol features exist to answer.

WHY CHILD SPANS AND NOT SPAN EVENTS
    `span.add_event()` is the obvious reflex and it does not work here. Events
    ride on their span, so they are exported when the span ends -- which is
    precisely the moment that is too late. A child span is exported as soon as
    IT ends, while the parent is still running, so progress shows up in the
    console within a batch interval of being reported.

    Our pipeline already handles a child arriving before its parent: trace
    assembly aggregates incrementally and late-arriving parents merge with no
    watermark logic (Architecture.md U2). This relies on that rather than
    needing anything new.

WHY THEY ARE BOUNDED
    One span per progress report is fine at three reports and a denial of
    service at three million. A tool in a loop reporting every row would
    generate spans faster than it does work. So emission is capped per
    operation, and the cap being hit is itself recorded -- a silently truncated
    stream would be worse than no stream, because the gap would look like the
    tool stopping.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from opentelemetry import trace

log = logging.getLogger(__name__)

PROGRESS_SPAN: Final = "mcp.progress"
EVENT_SPAN: Final = "mcp.subscription.event"

MAX_PROGRESS_SPANS: Final = 200
"""Per operation. Beyond this, reports still reach the client -- only our
telemetry stops, which is the correct thing to sacrifice."""

_tracer = trace.get_tracer("mcpobs.streaming")
_patched = False


MAX_TRACKED_SPANS: Final = 4096
"""How many parent spans the counter remembers before resetting.

Bounds memory in a long-lived server. The reset gives every ACTIVE parent a
fresh budget, which is acceptable because it can only trigger with thousands of
distinct parents -- the memory case. The flood case is one parent emitting
millions of reports, where the dict holds a single entry and never resets, so
the cap holds exactly where it matters.
"""


class _ProgressCounter:
    """Per-span emission count, keyed by the parent span id.

    Keyed by span id rather than a context variable because `report_progress`
    can be called from a task the middleware never saw.
    """

    def __init__(self) -> None:
        self._counts: dict[int, int] = {}

    def take(self, span_id: int) -> int | None:
        """Next index for this span, or None once the cap is reached.

        The entry is deliberately NOT deleted when the cap is hit. An earlier
        version forgot it there, to bound memory -- which reset the count and
        handed the same span a fresh budget, so a flooding tool emitted 201
        spans over and over instead of stopping. Caught by the cap test, which
        is the only reason it was not shipped.
        """
        count = self._counts.get(span_id, 0)
        if count > MAX_PROGRESS_SPANS:
            return None
        if len(self._counts) > MAX_TRACKED_SPANS:
            self._counts.clear()
        self._counts[span_id] = count + 1
        return count


_counter = _ProgressCounter()


def instrument_progress() -> bool:
    """Emit a span for every `ctx.report_progress()` call.

    Patches the SDK's `Context.report_progress`, which is the only seam: the
    call goes straight to the session and produces no telemetry of its own.
    Idempotent, and returns False if the SDK is not importable.

    The customer's call is made FIRST and its result returned unchanged. If the
    notification fails, that failure reaches them exactly as it would have --
    instrumentation must not change the semantics of the thing it observes.
    """
    global _patched
    if _patched:
        return True
    try:
        from mcp.server.mcpserver.context import Context
    except ImportError:
        log.info("MCP SDK not importable; progress instrumentation unavailable")
        return False

    original = Context.report_progress

    async def report_progress(
        self: Any,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        # The customer's call FIRST, and its exception, if any, propagates
        # unchanged. Instrumentation must not alter the semantics of the thing
        # it observes -- if the notification fails, that failure must reach them
        # exactly as it would have.
        await original(self, progress, total, message)
        try:
            _emit_progress(progress, total, message)
        except Exception as exc:  # noqa: BLE001
            log.debug("progress span skipped: %s", exc)

    Context.report_progress = report_progress  # type: ignore[method-assign]
    _patched = True
    return True


def _emit_progress(progress: float, total: float | None, message: str | None) -> None:
    parent = trace.get_current_span()
    context = parent.get_span_context()
    if not context.is_valid:
        return

    index = _counter.take(context.span_id)
    if index is None:
        return
    if index == MAX_PROGRESS_SPANS:
        # Say so, once. A stream that just stops looks like the tool stopping,
        # which is a worse lie than admitting the cap.
        with _tracer.start_as_current_span(PROGRESS_SPAN) as span:
            span.set_attribute("mcp.progress.truncated", True)
            span.set_attribute("mcp.progress.limit", MAX_PROGRESS_SPANS)
        return

    with _tracer.start_as_current_span(PROGRESS_SPAN) as span:
        span.set_attribute("mcp.progress.value", float(progress))
        span.set_attribute("mcp.progress.index", index)
        if total is not None:
            span.set_attribute("mcp.progress.total", float(total))
            if total:
                span.set_attribute("mcp.progress.percent", round(100 * progress / total, 2))
        if message:
            # Truncated like every other captured string. A progress message is
            # written by the customer's code and can carry anything.
            span.set_attribute("mcp.progress.message", str(message)[:256])


class ObservedSubscriptionBus:
    """Wraps a `SubscriptionBus` so every published event becomes a span.

    A WRAPPER, NOT A PATCH, because the customer already hands the bus to
    `MCPServer(subscriptions=...)` -- so there is an explicit seam and no reason
    to monkey-patch a class. It also means a customer with their own bus
    implementation gets this for free:

        bus = ObservedSubscriptionBus(InMemorySubscriptionBus())
        mcp = MCPServer("srv", subscriptions=bus)

    WHAT IT FIXES
        A `subscriptions/listen` span covers the whole stream and is exported
        when the stream ends, so events delivered on it were invisible --
        throughput, delivery gaps and starvation all unanswerable. Each event is
        now its own span, exported immediately, so a stream that has stopped
        carrying events is visible as an absence of recent spans rather than as
        nothing at all.
    """

    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self.published = 0

    async def publish(self, event: Any) -> Any:
        # The real publish FIRST. Delivery is the customer's function; telemetry
        # is ours, and ours must never delay or fail theirs.
        result = await self._bus.publish(event)
        try:
            self.published += 1
            with _tracer.start_as_current_span(EVENT_SPAN) as span:
                span.set_attribute("mcp.subscription.event.type", type(event).__name__)
                span.set_attribute("mcp.subscription.event.index", self.published)
        except Exception as exc:  # noqa: BLE001
            log.debug("subscription event span skipped: %s", exc)
        return result

    def __getattr__(self, name: str) -> Any:
        """Everything else passes through untouched.

        `subscribe`/`unsubscribe` and any method a future SDK adds keep working
        without this wrapper needing to know about them -- a wrapper that has to
        enumerate its subject's API breaks on the next SDK release.
        """
        return getattr(self._bus, name)
