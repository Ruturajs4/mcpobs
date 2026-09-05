"""SamplingSpanProcessor.

Spans are real: a TracerProvider wired to the processor under test, not
hand-built ReadableSpan mocks -- proves the tail-buffering actually reacts to
the real on_start/on_end order the SDK produces (children end before the
parent they happened inside), not to an order a mock happened to be handed in.
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.trace import Status, StatusCode, set_span_in_context

from mcpobs.sampling import DEFAULT_MAX_TRACES, SamplingSpanProcessor


class RecordingProcessor(SpanProcessor):
    """A real `SpanProcessor` (inherits the actual ABC, not just duck-typed --
    `SamplingSpanProcessor` isinstance-checks this, and a duck-typed fake
    would silently take the wrong branch and get wrapped in a
    BatchSpanProcessor expecting an exporter interface instead) that just
    remembers what reached it, in order."""

    def __init__(self) -> None:
        self.ended: list[ReadableSpan] = []
        self.shutdown_called = False
        self.flush_called = False

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        self.ended.append(span)

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_called = True
        return True


def make_provider(sampler: SamplingSpanProcessor) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(sampler)
    return provider


def run_call(tracer: Any, *, fail: bool, children: int = 1) -> None:
    """One simulated tools/call: a root span with `children` nested spans,
    ended in the real order the SDK produces -- every child closes before
    the root it happened inside does."""
    with tracer.start_as_current_span("tools/call demo") as root:
        for i in range(children):
            with tracer.start_as_current_span(f"child-{i}"):
                pass
        if fail:
            root.set_status(Status(StatusCode.ERROR))


def start_orphan_child(tracer: Any) -> ReadableSpan:
    """A genuinely incomplete trace, built through the real public span API:
    a root that is started but deliberately never ended, and one child of it
    that is. Simulates a crashed process or a vanished stdio client -- the
    exact case the buffer's eviction has to survive."""
    root = tracer.start_span("orphan-root")  # never ended, on purpose
    child = tracer.start_span("orphan-child", context=set_span_in_context(root))
    child.end()
    return child


class TestModeAll:
    def test_forwards_every_span_immediately(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(SamplingSpanProcessor(recorder, mode="all"))
        run_call(provider.get_tracer("t"), fail=False, children=2)
        # root + 2 children, all forwarded -- "all" is a pure pass-through.
        assert len(recorder.ended) == 3

    def test_is_the_default(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(SamplingSpanProcessor(recorder))
        run_call(provider.get_tracer("t"), fail=False)
        assert len(recorder.ended) == 2


class TestModeErrorsOnly:
    def test_drops_a_successful_trace_entirely(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(SamplingSpanProcessor(recorder, mode="errors_only"))
        run_call(provider.get_tracer("t"), fail=False, children=3)
        assert recorder.ended == []

    def test_keeps_a_failing_trace_entirely(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(SamplingSpanProcessor(recorder, mode="errors_only"))
        run_call(provider.get_tracer("t"), fail=True, children=3)
        # root + 3 children -- the whole trace, not just the errored span.
        assert len(recorder.ended) == 4

    def test_a_kept_traces_children_are_not_missing(self) -> None:
        """The one property that makes tail sampling worth the buffering at
        all: opening a kept error trace must show the downstream calls that
        explain it, not just the bare failing root."""
        recorder = RecordingProcessor()
        provider = make_provider(SamplingSpanProcessor(recorder, mode="errors_only"))
        run_call(provider.get_tracer("t"), fail=True, children=2)
        names = {s.name for s in recorder.ended}
        assert names == {"tools/call demo", "child-0", "child-1"}

    def test_successes_and_failures_side_by_side_only_failures_pass(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(SamplingSpanProcessor(recorder, mode="errors_only"))
        tracer = provider.get_tracer("t")
        run_call(tracer, fail=False)
        run_call(tracer, fail=True)
        run_call(tracer, fail=False)
        # root + 1 child from the ONE failing call; both successes' spans
        # (2 each) are entirely absent.
        assert len(recorder.ended) == 2
        roots = [s for s in recorder.ended if s.parent is None]
        assert len(roots) == 1
        assert roots[0].status.status_code == StatusCode.ERROR


class TestModePercentage:
    def test_errors_always_kept_regardless_of_rate(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(
            SamplingSpanProcessor(recorder, mode="percentage", rate=0.0)
        )
        tracer = provider.get_tracer("t")
        for _ in range(20):
            run_call(tracer, fail=True)
        # rate=0.0 would drop every SUCCESS, but nothing here was one --
        # 20 calls x (root + 1 child) each, all of them errors.
        assert len(recorder.ended) == 40

    def test_rate_zero_drops_all_successes(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(
            SamplingSpanProcessor(recorder, mode="percentage", rate=0.0)
        )
        tracer = provider.get_tracer("t")
        for _ in range(20):
            run_call(tracer, fail=False)
        assert recorder.ended == []

    def test_rate_one_keeps_all_successes(self) -> None:
        recorder = RecordingProcessor()
        provider = make_provider(
            SamplingSpanProcessor(recorder, mode="percentage", rate=1.0)
        )
        tracer = provider.get_tracer("t")
        for _ in range(20):
            run_call(tracer, fail=False)
        assert len(recorder.ended) == 40  # 20 calls x (root + 1 child)

    def test_rate_is_approximately_respected_over_many_trials(self) -> None:
        random.seed(1234)  # deterministic: this test asserts a real property,
        # not "did today's randomness happen to land in range".
        recorder = RecordingProcessor()
        provider = make_provider(
            SamplingSpanProcessor(recorder, mode="percentage", rate=0.3)
        )
        tracer = provider.get_tracer("t")
        trials = 2000
        for _ in range(trials):
            run_call(tracer, fail=False)
        # Count TRACES kept, not raw spans -- each kept trace contributes a
        # root plus its child.
        kept = len([s for s in recorder.ended if s.parent is None])
        # Binomial(2000, 0.3): mean 600, std ~20.5. Five std is a wide,
        # not-flaky margin for a seeded RNG.
        assert 500 < kept < 700, f"kept {kept}/{trials}, expected ~{trials * 0.3:.0f}"


class TestValidation:
    def test_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValueError, match="mode must be one of"):
            SamplingSpanProcessor(RecordingProcessor(), mode="sometimes")

    def test_rejects_rate_outside_unit_interval(self) -> None:
        with pytest.raises(ValueError, match="rate must be between"):
            SamplingSpanProcessor(RecordingProcessor(), mode="percentage", rate=1.5)

    def test_rejects_negative_rate(self) -> None:
        with pytest.raises(ValueError, match="rate must be between"):
            SamplingSpanProcessor(RecordingProcessor(), mode="percentage", rate=-0.1)


class TestMemoryBound:
    def test_incomplete_traces_do_not_grow_the_buffer_without_bound(self) -> None:
        """Feeding far more than max_traces worth of genuinely incomplete
        traces (root never ends) must not let the buffer grow past the cap."""
        recorder = RecordingProcessor()
        sampler = SamplingSpanProcessor(recorder, mode="errors_only", max_traces=5)
        provider = make_provider(sampler)
        tracer = provider.get_tracer("t")

        for _ in range(50):
            start_orphan_child(tracer)

        assert len(sampler._buffers) <= 5

    def test_evicts_oldest_incomplete_trace_first(self) -> None:
        recorder = RecordingProcessor()
        sampler = SamplingSpanProcessor(recorder, mode="errors_only", max_traces=2)
        provider = make_provider(sampler)
        tracer = provider.get_tracer("t")

        # Three genuinely incomplete traces (each a real, distinct root that
        # never ends), exceeding max_traces=2.
        children = [start_orphan_child(tracer) for _ in range(3)]
        trace_ids = [c.get_span_context().trace_id for c in children]

        assert len(sampler._buffers) == 2
        # The first trace seen was evicted; the two more recent remain.
        assert trace_ids[0] not in sampler._buffers
        assert trace_ids[1] in sampler._buffers
        assert trace_ids[2] in sampler._buffers

    def test_a_completing_trace_is_unaffected_by_eviction_pressure(self) -> None:
        """Eviction only ever touches INCOMPLETE traces -- a trace whose root
        ends normally must be forwarded (per `mode`), never silently dropped
        by capacity pressure from unrelated abandoned ones."""
        recorder = RecordingProcessor()
        sampler = SamplingSpanProcessor(recorder, mode="errors_only", max_traces=2)
        provider = make_provider(sampler)
        tracer = provider.get_tracer("t")

        for _ in range(10):
            start_orphan_child(tracer)
        run_call(tracer, fail=True, children=1)

        names = {s.name for s in recorder.ended}
        assert names == {"tools/call demo", "child-0"}

    def test_default_cap_is_a_thousand(self) -> None:
        assert DEFAULT_MAX_TRACES == 1000


class TestWrapsExporterOrProcessor:
    def test_wraps_a_bare_exporter_in_a_batch_processor(self) -> None:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        sampler = SamplingSpanProcessor(InMemorySpanExporter())
        assert isinstance(sampler._processor, BatchSpanProcessor)

    def test_uses_a_processor_directly_without_rewrapping(self) -> None:
        recorder = RecordingProcessor()
        sampler = SamplingSpanProcessor(recorder)
        assert sampler._processor is recorder


class TestDelegation:
    def test_shutdown_delegates_to_the_wrapped_processor(self) -> None:
        recorder = RecordingProcessor()
        sampler = SamplingSpanProcessor(recorder)
        sampler.shutdown()
        assert recorder.shutdown_called

    def test_force_flush_delegates_to_the_wrapped_processor(self) -> None:
        recorder = RecordingProcessor()
        sampler = SamplingSpanProcessor(recorder)
        assert sampler.force_flush() is True
        assert recorder.flush_called
