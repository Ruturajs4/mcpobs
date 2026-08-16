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
    _detail,
    _number,
    _order_tree,
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
        ordered = _order_tree(spans, _resolve_root(spans))
        assert [s.depth for s in ordered] == [0, 1, 2]

    def test_siblings_share_depth(self) -> None:
        spans = [span("a"), span("b", "a", 1), span("c", "a", 2)]
        ordered = _order_tree(spans, _resolve_root(spans))
        assert {s.span_id: s.depth for s in ordered} == {"a": 0, "b": 1, "c": 1}

    def test_cycle_does_not_hang(self) -> None:
        """Telemetry is untrusted input; a malformed trace must not spin."""
        spans = [span("a", "b"), span("b", "a", 1)]
        ordered = _order_tree(spans, spans[0])
        assert all(s.depth >= 0 for s in ordered)


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


class TestAttributeRedaction:
    """Secrets must not reach storage. Redaction at render is too late."""

    def setup_method(self) -> None:
        from normalizer.redact import AttributeRedactor

        self.r = AttributeRedactor()

    def test_api_key_in_query_string_is_scrubbed(self) -> None:
        """The classic leak: `?api_key=…` flowing straight into http.url."""
        out = self.r.value("http.url", "https://api.example.com/v1?api_key=sk-abcdefghij123456&page=2")
        assert "sk-abcdefghij123456" not in out
        assert "page=2" in out          # the useful part survives
        assert "/v1" in out             # so does the path

    def test_url_without_query_is_untouched(self) -> None:
        url = "http://127.0.0.1:8899/status/500"
        assert self.r.value("http.url", url) == url

    def test_sql_literals_are_scrubbed_but_placeholders_survive(self) -> None:
        """A parameterised statement is what you group by; keep its shape."""
        out = self.r.value(
            "db.statement", "SELECT * FROM users WHERE email = 'alice@example.com' AND id = ?"
        )
        assert "alice@example.com" not in out
        assert "id = ?" in out
        assert "FROM users" in out

    def test_bearer_token_anywhere(self) -> None:
        out = self.r.value("db.statement", "-- auth: Bearer eyJhbGciOiJIUzI1NiJ9.body.sig")
        assert "eyJhbGciOiJIUzI1NiJ9" not in out

    def test_only_risky_keys_are_touched(self) -> None:
        """Raw fidelity matters for replay; scrub the known-risky keys only."""
        attrs = {"http.url": "https://x/?token=abc123def456", "custom.note": "token=abc123def456"}
        out = self.r.apply(attrs)
        assert "abc123def456" not in out["http.url"]
        assert out["custom.note"] == "token=abc123def456"

    def test_never_raises(self) -> None:
        from normalizer.redact import AttributeRedactor

        assert AttributeRedactor().value("http.url", "::: not a url :::")


class TestClockVerdict:
    """DF-4 as a product behaviour, not a verify-only warning.

    The register said the quiet part: Linux is nanosecond-grade so OUR
    production is probably fine, A CUSTOMER ON WINDOWS IS NOT. That makes the
    coarse-clock caveat something the console owes the operator, and the
    threshold logic is worth pinning because both halves of it are load-bearing.
    """

    def warn(self, p50_ms: float, tick_ms: float, count: int = 100, zeros: int = 0) -> str:
        from query.repository import SpanRepository

        return SpanRepository._clock_warning(p50_ms, tick_ms, count, zeros)

    def test_a_fine_clock_produces_no_caveat(self) -> None:
        assert self.warn(p50_ms=12.0, tick_ms=0.0001) == ""

    def test_p50_near_the_tick_is_quantisation(self) -> None:
        """A percentile stops meaning anything once the clock's tick approaches
        it, however few samples floored to zero."""
        assert "quantisation" in self.warn(p50_ms=1.0, tick_ms=0.5)

    def test_mostly_zero_samples_warn_even_when_p50_looks_healthy(self) -> None:
        """The second test is not redundant. A p50 comfortably above the tick
        still misleads when most calls measured zero, because the survivors are
        a biased tail rather than a sample of the whole."""
        message = self.warn(p50_ms=40.0, tick_ms=0.5, count=100, zeros=60)
        assert "60% of calls measured 0ms" in message

    def test_no_samples_never_warns(self) -> None:
        """An empty window is not a coarse clock. Warning here would put a
        scary banner on a brand-new customer's first page load, which is the
        same class of mistake as D42."""
        assert self.warn(p50_ms=0.0, tick_ms=0.0, count=0) == ""
        assert self.warn(p50_ms=0.0, tick_ms=0.5, count=0) == ""

    def test_the_message_names_the_measured_tick(self) -> None:
        """The old banner hardcoded '~0.75ms' -- measured once on one laptop and
        frozen into the UI, so it was wrong for every other host."""
        assert "0.750ms" in self.warn(p50_ms=1.0, tick_ms=0.75)

    def test_latency_stats_carry_the_verdict(self) -> None:
        from query.repository import SpanRepository

        stats = SpanRepository._latency([100, 1_000_000, 2_000_000, 3_000_000,
                                         4_000_000, 10, 500_000])
        assert stats.clock_tick_ms == 0.5
        assert stats.clock_warning
        assert stats.p50_ms == 1.0

    def test_latency_stats_without_a_tick_are_unqualified(self) -> None:
        """Older callers pass six columns and must keep working -- an absent
        tick is 'not measured', never 'measured as zero'."""
        from query.repository import SpanRepository

        stats = SpanRepository._latency([100, 50_000_000, 60_000_000, 70_000_000,
                                         80_000_000, 0])
        assert stats.clock_tick_ms == 0.0
        assert stats.clock_warning == ""


class TestWaterfallOrdering:
    """A parent must be rendered before its children, always.

    Indentation is only meaningful if the row above a child is its ancestor.
    Depths were computed correctly for days while the list came back in
    TIMESTAMP order, so a child could sit above its own parent and the tree read
    as a lie -- caught by eye in the console, not by any assertion.
    """

    def span(self, span_id: str, parent: str, offset_ms: float):
        from datetime import datetime, timedelta

        from query.dtos import SpanDTO

        return SpanDTO(
            span_id=span_id, parent_span_id=parent, name=span_id,
            start_time=datetime(2026, 8, 16) + timedelta(milliseconds=offset_ms),
            duration_ms=1.0,
        )

    def order(self, spans):
        from query.repository import _order_tree, _resolve_root

        return _order_tree(spans, _resolve_root(spans))

    def test_a_child_never_precedes_its_parent(self) -> None:
        """THE regression. `tools/list` and its parent `POST /mcp` started in
        the same millisecond, so the child sorted first."""
        spans = [
            self.span("child", "root", 0),   # same start time as its parent
            self.span("root", "", 0),
        ]
        ordered = self.order(spans)
        assert [s.span_id for s in ordered] == ["root", "child"]
        assert [s.depth for s in ordered] == [0, 1]

    def test_a_parent_that_starts_later_still_comes_first(self) -> None:
        """Architecture U2: a child routinely lands before its parent. Ordering
        by time misrenders that too, not just the tie."""
        spans = [
            self.span("child", "root", 0),
            self.span("root", "", 5),        # parent starts AFTER the child
        ]
        assert [s.span_id for s in self.order(spans)] == ["root", "child"]

    def test_siblings_stay_in_start_time_order(self) -> None:
        """Tree order is the primary key; time is what makes one level
        readable."""
        spans = [
            self.span("root", "", 0),
            self.span("late", "root", 9),
            self.span("early", "root", 1),
        ]
        assert [s.span_id for s in self.order(spans)] == ["root", "early", "late"]

    def test_a_grandchild_follows_its_own_parent_not_its_uncle(self) -> None:
        spans = [
            self.span("root", "", 0),
            self.span("a", "root", 1),
            self.span("b", "root", 2),
            self.span("a1", "a", 3),         # starts after `b`, belongs under `a`
        ]
        assert [s.span_id for s in self.order(spans)] == ["root", "a", "a1", "b"]

    def test_an_unreachable_span_is_appended_not_dropped(self) -> None:
        """A parent that has not arrived yet is normal. A span missing from the
        waterfall is worse than one drawn at the wrong indent."""
        spans = [
            self.span("root", "", 0),
            self.span("orphan", "never-arrived", 1),
        ]
        ordered = self.order(spans)
        assert {s.span_id for s in ordered} == {"root", "orphan"}

    def test_a_cycle_cannot_hang_the_renderer(self) -> None:
        """Telemetry is untrusted input; two spans naming each other as parent
        must not spin forever."""
        spans = [
            self.span("root", "", 0),
            self.span("a", "b", 1),
            self.span("b", "a", 2),
        ]
        ordered = self.order(spans)
        assert len(ordered) == 3

    def test_every_span_appears_exactly_once(self) -> None:
        spans = [self.span("root", "", 0)] + [
            self.span(f"c{i}", "root", i) for i in range(5)
        ]
        ordered = self.order(spans)
        assert len(ordered) == len(spans)
        assert len({s.span_id for s in ordered}) == len(spans)


class TestTraceHeadline:
    """The trace header names ONE operation."""

    def spans(self):
        from datetime import datetime

        from query.dtos import SpanDTO

        def make(name, method="", tool=""):
            return SpanDTO(
                span_id=name, name=name, start_time=datetime(2026, 8, 16),
                duration_ms=1.0, mcp_method=method, tool=tool,
            )

        # A real trace: the client's handshake, then the actual work.
        return [make("POST /mcp"), make("tools/list", "tools/list"),
                make("tools/call slow_export", "tools/call", "slow_export")]

    def test_the_header_describes_the_tool_call_not_the_handshake(self) -> None:
        """Picked independently, `tool` and `mcp_method` came from DIFFERENT
        spans: the header read "slow_export" with "METHOD tools/list" beside
        it -- a method that tool was never called by."""
        from query.repository import _headline

        headline = _headline(self.spans())
        assert (headline.tool, headline.mcp_method) == ("slow_export", "tools/call")

    def test_a_trace_with_no_tool_falls_back_to_its_method(self) -> None:
        from datetime import datetime

        from query.dtos import SpanDTO
        from query.repository import _headline

        spans = [SpanDTO(span_id="a", name="tools/list", start_time=datetime(2026, 8, 16),
                         duration_ms=1.0, mcp_method="tools/list")]
        assert _headline(spans).mcp_method == "tools/list"

    def test_a_trace_with_no_mcp_span_has_no_headline(self) -> None:
        from datetime import datetime

        from query.dtos import SpanDTO
        from query.repository import _headline

        spans = [SpanDTO(span_id="a", name="GET", start_time=datetime(2026, 8, 16),
                         duration_ms=1.0)]
        assert _headline(spans) is None
