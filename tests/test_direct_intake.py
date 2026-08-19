"""DirectIntake: the lite-mode substitute for Kafka + the normalizer container.

`ClickHouseSink` is given a fake `client` (constructor already supports
dependency injection, `normalizer/clickhouse_sink.py:23`) rather than mocking
anything -- these tests never touch a real ClickHouse. What they can prove is
that the RIGHT calls happen with the RIGHT arguments; whether ClickHouse
actually deduplicates on a repeated token is `tests/test_lite_stack.py`'s job,
against a real server, matching this project's "measure, don't assume" pattern
(e.g. tests/test_stdio_transport.py).
"""

from __future__ import annotations

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from ingest.direct_intake import DirectIntake, IntakeError
from normalizer.clickhouse_sink import ClickHouseSink
from normalizer.models import SpanRow


def _mutated(payload: bytes) -> bytes:
    """A second, VALID OTLP payload with different content.

    Appending arbitrary bytes (e.g. a trailing null) breaks protobuf framing
    outright rather than producing "different but valid" content -- measured
    while writing this test. Round-tripping through the real message type is
    the only reliable way to get a payload that both decodes and differs.
    """
    message = ExportTraceServiceRequest()
    message.ParseFromString(payload)
    message.resource_spans[0].scope_spans[0].spans[0].name += "-mutated"
    return message.SerializeToString()


class FakeClient:
    """Records insert() calls; answers query() so wait_ready-style checks pass."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list, dict | None]] = []

    def insert(self, table, data, column_names=None, settings=None) -> None:
        self.inserts.append((table, list(data), settings))

    def query(self, sql: str):
        return None


@pytest.fixture
def intake() -> tuple[DirectIntake, FakeClient]:
    fake_client = FakeClient()
    di = DirectIntake()
    di.sink = ClickHouseSink(client=fake_client)
    return di, fake_client


class TestDecodeFailureRejects:
    def test_unparseable_payload_raises_intake_error(self, intake) -> None:
        di, _ = intake
        with pytest.raises(IntakeError):
            di.ingest(b"not an OTLP payload")

    def test_empty_resource_spans_raises_intake_error(self, intake) -> None:
        """An OTLP message that parses but carries zero spans.

        `OtlpDecoder._parse` treats an empty `resource_spans` as unparseable
        (normalizer/otlp_decode.py: `if message.resource_spans: return message`)
        -- this is the one real edge case where ingest's own `_parse` (which
        does not check that) succeeds but `DirectIntake.ingest` correctly
        rejects the same bytes.
        """
        di, _ = intake
        with pytest.raises(IntakeError):
            di.ingest(b"")


class TestDedupToken:
    def test_identical_payload_yields_identical_settings(self, intake, otlp_payload) -> None:
        """What a unit test CAN prove: the token is a pure function of the
        bytes, so an exporter's retry of the same POST produces the same
        `insert_deduplication_token` both times. Whether ClickHouse actually
        no-ops on the repeat is proven live, not here."""
        di, fake = intake
        di.ingest(otlp_payload)
        di.ingest(otlp_payload)
        assert len(fake.inserts) == 2
        token_a = fake.inserts[0][2]["insert_deduplication_token"]
        token_b = fake.inserts[1][2]["insert_deduplication_token"]
        assert token_a == token_b
        assert token_a  # non-empty

    def test_different_payloads_yield_different_tokens(self, intake, otlp_payload) -> None:
        di, fake = intake
        di.ingest(otlp_payload)
        di.ingest(_mutated(otlp_payload))
        tokens = {row[2]["insert_deduplication_token"] for row in fake.inserts if row[2]}
        assert len(tokens) == len(fake.inserts), "different bytes must not collide"

    def test_the_dependent_mv_dedup_setting_is_also_set(self, intake, otlp_payload) -> None:
        """The setting `ClickHouseSink.insert_spans` pairs with the token
        (normalizer/clickhouse_sink.py:55) -- without it, spans_raw dedupes
        while trace_summaries double-counts (D39). Guarding it here too since
        DirectIntake calls insert_spans exactly like the Kafka path does."""
        di, fake = intake
        di.ingest(otlp_payload)
        assert fake.inserts[0][2]["deduplicate_blocks_in_dependent_materialized_views"] == 1


class TestPartialNormalizeFailure:
    """`to_row` is defensively coded and essentially never raises in
    practice (every coercion helper falls back rather than throwing) -- but
    the guard exists in both consumer.py and here for the case it does, and
    the guard's BEHAVIOR is what this class proves: one bad span must not
    sink the rest of the batch."""

    def test_one_failing_span_is_dead_lettered_and_the_rest_still_insert(
        self, intake, otlp_payload
    ) -> None:
        di, fake = intake
        real_to_row = di.normalizer.to_row
        calls = {"n": 0}

        def flaky_to_row(span, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("simulated normalize failure")
            return real_to_row(span, **kwargs)

        di.normalizer.to_row = flaky_to_row  # type: ignore[method-assign]

        written = di.ingest(otlp_payload)

        assert calls["n"] >= 2, "fixture needs >1 span for this test to mean anything"
        assert written == calls["n"] - 1, "exactly the failing span is excluded from the insert"
        tables = [row[0] for row in fake.inserts]
        assert ClickHouseSink.DLQ_TABLE in tables
        assert ClickHouseSink.SPANS_TABLE in tables

    def test_every_span_failing_still_returns_without_inserting_an_empty_batch(
        self, intake, otlp_payload
    ) -> None:
        di, fake = intake

        def always_fails(span, **kwargs):
            raise ValueError("simulated normalize failure")

        di.normalizer.to_row = always_fails  # type: ignore[method-assign]

        written = di.ingest(otlp_payload)

        assert written == 0
        assert all(row[0] == ClickHouseSink.DLQ_TABLE for row in fake.inserts), (
            "insert_spans must not be called with an empty row list"
        )


class TestSpansTableInsert:
    def test_a_clean_payload_inserts_into_spans_raw_only(self, intake, otlp_payload) -> None:
        di, fake = intake
        written = di.ingest(otlp_payload)
        assert written > 0
        assert [row[0] for row in fake.inserts] == [ClickHouseSink.SPANS_TABLE]

    def test_row_column_alignment_survives_the_direct_path(self, intake, otlp_payload) -> None:
        """Same guard as TestColumnAlignment in test_normalize.py, exercised
        through DirectIntake specifically -- this is the path a lite
        deployment actually runs, not just SpanNormalizer in isolation."""
        di, fake = intake
        di.ingest(otlp_payload)
        table, rows, _ = fake.inserts[0]
        assert table == ClickHouseSink.SPANS_TABLE
        assert len(rows[0]) == len(SpanRow.columns())
