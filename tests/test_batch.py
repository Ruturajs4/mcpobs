"""SpanBatch flush semantics.

The class exists because of a real production-shaped bug: a batch whose messages
were ALL dead-lettered held offsets but no rows, and the flush gate checked rows.
The offsets were never committed, so the partition replayed the poison message
forever -- lag stuck, DLQ filling with duplicates, pipeline looking healthy.
"""

from __future__ import annotations

from normalizer.consumer import SpanBatch
from normalizer.normalize import SpanNormalizer


def batch(max_rows: int = 10, max_seconds: float = 5.0) -> SpanBatch:
    return SpanBatch(max_rows=max_rows, max_seconds=max_seconds, topic="otlp.spans.raw")


class TestPartitionStall:
    def test_offsets_with_no_rows_still_flush(self, span_factory) -> None:
        """THE regression test. All messages dead-lettered -> offsets must commit."""
        b = batch(max_seconds=0.0)
        b.track(partition=5, offset=1)  # dead-lettered: tracked, no row added
        assert not b.rows
        assert b.has_pending_offsets
        assert b.should_flush(), "a batch with only dead-lettered offsets must flush"

    def test_empty_batch_does_not_flush(self) -> None:
        assert not batch(max_seconds=0.0).should_flush()

    def test_reset_clears_rows_and_offsets(self, span_factory) -> None:
        b = batch()
        b.add_row(SpanNormalizer().to_row(span_factory()))
        b.track(0, 1)
        b.reset()
        assert not b.rows
        assert not b.has_pending_offsets


class TestFlushTriggers:
    def test_row_count_triggers_flush(self, span_factory) -> None:
        b = batch(max_rows=2, max_seconds=999)
        row = SpanNormalizer().to_row(span_factory())
        b.add_row(row)
        b.track(0, 1)
        assert not b.should_flush()
        b.add_row(row)
        assert b.should_flush()

    def test_time_triggers_flush(self, span_factory) -> None:
        # Build the row BEFORE opening the batch: constructing it took long
        # enough to consume the window and make this assertion flaky.
        row = SpanNormalizer().to_row(span_factory())
        b = batch(max_rows=999, max_seconds=5.0)
        b.add_row(row)
        b.track(0, 1)
        assert not b.should_flush()
        b.opened_at -= 5.1  # deterministic: pretend the window elapsed
        assert b.should_flush()


class TestDedupToken:
    def test_token_is_deterministic_across_replays(self) -> None:
        """ADR-006: a redelivered batch must produce a byte-identical token."""
        a, b = batch(), batch()
        for target in (a, b):
            target.track(0, 10)
            target.track(0, 19)
            target.track(3, 4)
        assert a.dedup_token() == b.dedup_token()

    def test_token_covers_the_offset_range(self) -> None:
        b = batch()
        b.track(0, 10)
        b.track(0, 19)
        assert b.dedup_token() == "otlp.spans.raw|0:10-19"

    def test_token_is_partition_order_independent(self) -> None:
        a, b = batch(), batch()
        a.track(1, 5)
        a.track(0, 2)
        b.track(0, 2)
        b.track(1, 5)
        assert a.dedup_token() == b.dedup_token()

    def test_different_offsets_produce_different_tokens(self) -> None:
        a, b = batch(), batch()
        a.track(0, 1)
        b.track(0, 2)
        assert a.dedup_token() != b.dedup_token()


class TestOffsetTracking:
    def test_tracks_min_and_max_per_partition(self) -> None:
        b = batch()
        for offset in (7, 3, 11, 5):
            b.track(2, offset)
        assert b.offsets[2] == [3, 11]

    def test_partitions_tracked_independently(self) -> None:
        b = batch()
        b.track(0, 100)
        b.track(1, 5)
        assert b.offsets == {0: [100, 100], 1: [5, 5]}
