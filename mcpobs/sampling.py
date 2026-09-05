"""Sampling: choose which spans actually get EXPORTED, not which get created.

WHY A SPAN PROCESSOR, NOT AN OTEL SAMPLER
    OTel's own `Sampler` decides at span START whether to record at all --
    before the tool has run, before anyone knows whether it will fail. Errors
    are the traces an operator actually opens; deciding to drop most traffic
    before knowing which slice would have been the failures is backwards for
    exactly this product. So this decides at END instead, wrapping the REAL
    span processor: `on_end()` sees the finished span's status and decides
    whether to forward it, never whether to have recorded it.

WHY IT BUFFERS PER TRACE (TAIL, NOT HEAD)
    The decision has to be made on the ROOT span (the `tools/call` span) using
    its own final status, but a root span's CHILDREN (db, http, llm) always
    finish first -- a child cannot outlive the call it happened inside. So
    every span for a trace is held here until that trace's root span ends,
    at which point the whole group is forwarded together or dropped together.
    A kept trace is never missing the child spans that explain it; a dropped
    trace never leaks an orphaned db span with no parent to give it context.

WHAT "ERROR" MEANS HERE, AND WHAT IT DOES NOT
    A root span whose OTel status is ERROR -- exactly what the MCP SDK's own
    middleware sets for a genuine tool_error or an unhandled exception (the
    two categories reachable without the classifier attached; see
    mcpobs/classifier.py's own docstring). This is a SIMPLER signal than the
    query-side taxonomy's: it does not know about 401/403-as-non-failure or
    cancelled-as-non-failure, because those distinctions live in spans this
    processor never sees as the trace's root (a 401 is its own root, an HTTP
    span with no MCP method). Treating that separately is future work, not a
    silent gap: it means a self-hosted customer sampling `errors_only` keeps
    slightly more than the console would call "real" failures, never less.

MEMORY IS BOUNDED, NOT UNBOUNDED
    A trace whose root span never ends (the process crashed mid-call, a
    stdio client vanished) would buffer forever without a limit. `max_traces`
    caps how many incomplete traces are held at once; past that, the OLDEST
    incomplete trace is evicted and its spans dropped -- losing one trace's
    telemetry is the correct trade against a customer's memory growing
    without bound because of spans that were never going to complete anyway.
"""

from __future__ import annotations

import random
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Final

from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import StatusCode

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter

ALL: Final = "all"
ERRORS_ONLY: Final = "errors_only"
PERCENTAGE: Final = "percentage"

_MODES: Final = frozenset({ALL, ERRORS_ONLY, PERCENTAGE})

DEFAULT_MAX_TRACES: Final = 1000
"""Incomplete traces held at once. ~1000 root spans' worth of children is a
few MB at most -- generous for what should be seconds of in-flight calls,
not a number anyone needs to tune."""


class SamplingSpanProcessor(SpanProcessor):
    """Wraps a real `SpanProcessor` (or `SpanExporter`) and forwards only the
    traces `mode` says to keep.

        provider.add_span_processor(
            SamplingSpanProcessor(BatchSpanProcessor(exporter), mode="errors_only")
        )

    `mode`:
        "all"          -- (default) forward everything. Identical to not
                          wrapping the processor at all.
        "errors_only"  -- forward only traces whose root span ended in error.
        "percentage"   -- forward `rate` (0.0-1.0) of successful traces, but
                          ALWAYS forward errors regardless of rate. A customer
                          turning this on to cut cost should never lose the
                          one thing they actually open traces to look at.

    Accepts a `SpanProcessor` OR a bare `SpanExporter` for `wrapped` -- the
    latter is wrapped in a `BatchSpanProcessor` for you, since "an exporter
    and a mode" is the shape most customers reach for.
    """

    def __init__(
        self,
        wrapped: SpanProcessor | SpanExporter,
        mode: str = ALL,
        rate: float = 1.0,
        max_traces: int = DEFAULT_MAX_TRACES,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {sorted(_MODES)}, got {mode!r}")
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"rate must be between 0.0 and 1.0, got {rate!r}")

        if isinstance(wrapped, SpanProcessor):
            self._processor = wrapped
        else:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            self._processor = BatchSpanProcessor(wrapped)

        self._mode = mode
        self._rate = rate
        self._max_traces = max_traces
        self._lock = threading.Lock()
        # Insertion-ordered so eviction can drop the OLDEST incomplete trace,
        # not an arbitrary one -- the trace most likely to never complete.
        self._buffers: OrderedDict[int, list[ReadableSpan]] = OrderedDict()

    def on_start(self, span: Any, parent_context: Any = None) -> None:  # type: ignore[override]
        pass  # Sampling here never influences whether a span is RECORDED.

    def on_end(self, span: ReadableSpan) -> None:
        if self._mode == ALL:
            self._processor.on_end(span)
            return

        trace_id = span.context.trace_id  # type: ignore[union-attr]
        is_root = span.parent is None

        with self._lock:
            buffer = self._buffers.setdefault(trace_id, [])
            buffer.append(span)
            if not is_root:
                self._evict_if_over_capacity(skip=trace_id)
                return
            spans = self._buffers.pop(trace_id)

        if self._keep(span):
            for s in spans:
                self._processor.on_end(s)

    def _keep(self, root_span: ReadableSpan) -> bool:
        is_error = (
            root_span.status is not None
            and root_span.status.status_code == StatusCode.ERROR
        )
        if is_error:
            return True  # Errors are never sampled away, in either mode.
        if self._mode == ERRORS_ONLY:
            return False
        return random.random() < self._rate  # PERCENTAGE

    def _evict_if_over_capacity(self, skip: int) -> None:
        """Drop the oldest incomplete trace once the buffer is full.

        Called only from on_end() while holding self._lock. `skip` is the
        trace whose span was JUST added -- never evict the one the caller is
        actively adding to.
        """
        while len(self._buffers) > self._max_traces:
            oldest_id = next(iter(self._buffers))
            if oldest_id == skip and len(self._buffers) == 1:
                break
            self._buffers.pop(oldest_id)

    def shutdown(self) -> None:
        self._processor.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._processor.force_flush(timeout_millis)
