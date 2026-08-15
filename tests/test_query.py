"""Query layer.

The pure parts -- root resolution, depth, cursors -- plus guards on the SQL
that encode decisions someone could otherwise undo without noticing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from query.dtos import FailureBreakdown
from query.repository import (
    LATEST_SPANS,
    SpanDTO,
    _assign_depths,
    _detail,
    _number,
    _resolve_root,
    decode_cursor,
    encode_cursor,
)

BASE = datetime(2026, 8, 15, 12, 0, 0)


def span(span_id: str, parent: str = "", offset_ms: int = 0, **kw) -> SpanDTO:
    return SpanDTO(
        span_id=span_id,
        parent_span_id=parent,
        name=kw.pop("name", span_id),
        start_time=BASE + timedelta(milliseconds=offset_ms),
        duration_ms=kw.pop("duration_ms", 1.0),
        **kw,
    )


class TestRootResolution:
    """D22/D7: the root is the span whose parent is ABSENT from the trace."""

    def test_orphan_by_empty_parent(self) -> None:
        spans = [span("a"), span("b", "a", 1)]
        assert _resolve_root(spans).span_id == "a"

    def test_root_when_parent_is_outside_the_trace(self) -> None:
        """The case a `parent_span_id == ''` test gets wrong.

        An instrumented client makes the MCP span a legitimate CHILD, so its
        parent id is set but points at a span we never received.
        """
        spans = [span("mcp", "client-span-we-never-got"), span("http", "mcp", 1)]
        assert _resolve_root(spans).span_id == "mcp"

    def test_earliest_orphan_wins(self) -> None:
        spans = [span("late", offset_ms=50), span("early", offset_ms=0)]
        assert _resolve_root(spans).span_id == "early"

    def test_no_spans(self) -> None:
        assert _resolve_root([]) is None


class TestDepth:
    def test_depth_follows_parentage(self) -> None:
        spans = [span("a"), span("b", "a", 1), span("c", "b", 2)]
        _assign_depths(spans, _resolve_root(spans))
        assert [s.depth for s in spans] == [0, 1, 2]

    def test_siblings_share_depth(self) -> None:
        spans = [span("a"), span("b", "a", 1), span("c", "a", 2)]
        _assign_depths(spans, _resolve_root(spans))
        assert {s.span_id: s.depth for s in spans} == {"a": 0, "b": 1, "c": 1}

    def test_cycle_does_not_hang(self) -> None:
        """Telemetry is untrusted input; a malformed trace must not spin."""
        spans = [span("a", "b"), span("b", "a", 1)]
        _assign_depths(spans, spans[0])
        assert all(s.depth >= 0 for s in spans)


class TestCursor:
    def test_round_trip(self) -> None:
        assert decode_cursor(encode_cursor(BASE)) == BASE

    def test_opaque(self) -> None:
        """Opaque on purpose: a readable cursor invites clients to build their
        own, and then the sort key can never change."""
        assert "2026" not in encode_cursor(BASE)


class TestSqlGuards:
    """These assertions exist because the decisions are easy to undo silently."""

    def test_every_span_read_resolves_normalization_version(self) -> None:
        """D24. Without argMax a replay mixes corrected rows with buggy ones."""
        assert "argMax(" in LATEST_SPANS
        assert "normalization_version" in LATEST_SPANS
        assert "GROUP BY trace_id, span_id" in LATEST_SPANS

    def test_tenant_and_project_are_always_bound(self) -> None:
        """Rule 1: an endpoint must not be able to read across tenants."""
        assert "{tenant:String}" in LATEST_SPANS
        assert "{project:String}" in LATEST_SPANS

    def test_timestamp_alias_does_not_shadow_source_column(self) -> None:
        """Aliasing the aggregate as `timestamp` makes ClickHouse resolve the
        inner WHERE to the aggregate and reject with ILLEGAL_AGGREGATION."""
        assert "AS span_time" in LATEST_SPANS
        assert "normalization_version) AS timestamp" not in LATEST_SPANS


class TestDownstreamDetail:
    def test_http(self) -> None:
        assert _detail("http", "GET", 500, "", "") == "GET 500"

    def test_db(self) -> None:
        assert _detail("db", "", None, "sqlite", "") == "sqlite"

    def test_llm(self) -> None:
        assert _detail("llm", "", None, "", "gpt-4o-mini") == "gpt-4o-mini"

    def test_mcp_span_has_none(self) -> None:
        assert _detail("", "", None, "", "") == ""


class TestFailureBreakdown:
    def test_failures_excludes_ok_and_pending(self) -> None:
        """pending_input is an MRTR interim result, never a failure (D20)."""
        breakdown = FailureBreakdown(ok=10, pending_input=3, tool_error=2, unknown_tool=1)
        assert breakdown.failures == 3


class TestEmptyTenant:
    """A brand-new customer with no telemetry. Their first page load.

    ClickHouse returns NaN from `quantile()` over an empty set, and `value or 0`
    passes NaN straight through because NaN is truthy -- then json.dumps raises
    and the endpoint 500s. This was a real bug, found by asserting an unknown
    tenant sees nothing (C5).
    """

    def test_nan_becomes_zero(self) -> None:
        assert _number(float("nan")) == 0.0

    def test_infinity_becomes_zero(self) -> None:
        assert _number(float("inf")) == 0.0
        assert _number(float("-inf")) == 0.0

    def test_none_becomes_zero(self) -> None:
        assert _number(None) == 0.0

    def test_ordinary_values_pass_through(self) -> None:
        assert _number(1500000) == 1500000.0
        assert _number("42") == 42.0

    def test_the_naive_idiom_is_unsafe(self) -> None:
        """Documents WHY _number exists, so nobody simplifies it back."""
        nan = float("nan")
        assert (nan or 0) is nan  # truthy -> survives -> breaks json.dumps
        assert _number(nan) == 0.0


class TestSpanDetailCompleteness:
    """D1: the console previously showed 17 of 55 stored columns.

    That drift is the entire reason for this day's work, so it is asserted
    rather than trusted. If a migration adds a column and nobody surfaces it,
    this fails.
    """

    #: Columns intentionally absent from SpanDetail, each with a reason. Any
    #: OTHER stored column must appear, or the omission was an accident.
    DELIBERATELY_ABSENT = {
        "tenant_id",       # scoping, not span data -- never rendered per span
        "project_id",
        "environment",     # duplicated as `environment` from deployment_environment
        "trace_id",        # on the trace, not repeated per span
        "mcp_is_error",    # surfaced as the typed `is_error`
        "timestamp",       # surfaced as `start_time`
        "duration_ns",     # surfaced as `duration_ms`
        "ingested_at",     # present, plus derived `freshness_ms`
    }

    def test_every_stored_column_is_reachable(self) -> None:
        from query.dtos import SpanDetail
        from query.repository import SpanRepository

        exposed = set(SpanDetail.model_fields)
        renames = {
            "span_name": "name", "span_kind": "kind", "status_code": "status",
            "deployment_environment": "environment", "mcp_tool_name": "tool",
            "mcp_prompt_name": "prompt", "mcp_resource_uri": "resource_uri",
            "mcp_session_id": "session_id",
        }
        missing = []
        for column in SpanRepository._SPAN_COLUMNS:
            if column in self.DELIBERATELY_ABSENT:
                continue
            if renames.get(column, column) not in exposed:
                missing.append(column)
        assert not missing, f"stored but not exposed in SpanDetail: {missing}"

    def test_provenance_is_exposed(self) -> None:
        """Which Kafka message produced this row, and which code wrote it --
        the difference between debugging the customer's server and ours."""
        from query.dtos import SpanDetail

        for field in ("normalization_version", "kafka_partition", "kafka_offset", "freshness_ms"):
            assert field in SpanDetail.model_fields

    def test_raw_attribute_maps_are_exposed(self) -> None:
        from query.dtos import SpanDetail

        assert "span_attributes" in SpanDetail.model_fields
        assert "resource_attributes" in SpanDetail.model_fields
