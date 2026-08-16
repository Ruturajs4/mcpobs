"""Query layer.

The pure parts -- root resolution, depth, cursors -- plus guards on the SQL
that encode decisions someone could otherwise undo without noticing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from query.dtos import NOT_A_FAILURE, FailureBreakdown, Page
from query.filters import HAVING, WHERE, catalog, parse
from query.repository import (
    CAPABILITY_ROW_CAP,
    LATEST_SPANS,
    TRACE_DETAIL_CAP,
    TRACE_SPAN_CAP,
    SpanDTO,
    SpanRepository,
    _detail,
    _number,
    _order_tree,
    _resolve_root,
    decode_cursor,
    encode_cursor,
)

BASE = datetime(2026, 8, 15, 12, 0, 0)


def test_capability_drilldown_keeps_the_active_server_filter() -> None:
    """Server -> Tool -> Traces must retain both identity dimensions."""
    source = (Path(__file__).parents[1] / "query" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    handler = source.split('bindAll("[data-item]"', 1)[1].split("\n  });", 1)[0]

    assert 'get("server")' in handler
    assert 'go("traces", { tool: n.dataset.item, server })' in handler


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
        assert decode_cursor(encode_cursor(BASE, "trace-b")) == (BASE, "trace-b")

    def test_opaque(self) -> None:
        """Opaque on purpose: a readable cursor invites clients to build their
        own, and then the sort key can never change."""
        assert "2026" not in encode_cursor(BASE, "trace-b")

    def test_malformed_cursor_is_a_value_error(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="invalid cursor"):
            decode_cursor("definitely-not-a-cursor")


class TestTracePagination:
    def test_cursor_uses_trace_id_to_seek_across_equal_timestamps(self) -> None:
        """Timestamp-only pagination drops every tied row after the boundary."""
        repo = object.__new__(SpanRepository)
        captured: dict[str, object] = {}

        def rows(sql: str, params: dict[str, object]) -> list[tuple[object, ...]]:
            captured["sql"] = sql
            captured["params"] = params
            return []

        repo._rows = rows  # type: ignore[method-assign]
        repo.traces(
            "tenant",
            "project",
            BASE,
            cursor=encode_cursor(BASE, "trace-b"),
        )

        assert (
            "start_time = {cursor_time:DateTime64(9)} "
            "AND trace_id < {cursor_trace_id:String}"
        ) in str(captured["sql"])
        assert "ORDER BY start_time DESC, trace_id DESC" in str(captured["sql"])
        assert captured["params"]["cursor_trace_id"] == "trace-b"  # type: ignore[index]


class TestFilterEndpointValidation:
    def test_invalid_number_and_cursor_are_422_responses(self) -> None:
        from fastapi.testclient import TestClient

        import query.app as query_app

        class Repo:
            def traces(self, *args: object, **kwargs: object) -> Page:
                return Page(items=[])

        scope = SimpleNamespace(
            tenant="tenant",
            project="project",
            since=BASE,
            window_minutes=60,
        )
        query_app.app.dependency_overrides[query_app.Scope] = lambda: scope
        query_app.app.dependency_overrides[query_app.repository] = Repo
        try:
            client = TestClient(query_app.app)
            bad_number = client.get("/api/v1/traces?min_duration_ms=not-a-number")
            bad_cursor = client.get("/api/v1/traces?cursor=definitely-not-a-cursor")
        finally:
            query_app.app.dependency_overrides.clear()

        assert bad_number.status_code == 422
        assert bad_number.json() == {"detail": "min_duration_ms must be a number"}
        assert bad_cursor.status_code == 422
        assert bad_cursor.json() == {"detail": "invalid cursor"}

    def test_advanced_category_filter_overrides_the_default_error_set(self) -> None:
        from fastapi.testclient import TestClient

        import query.app as query_app

        captured: dict[str, object] = {}

        class Repo:
            def traces(self, *args: object, **kwargs: object) -> Page:
                captured.update(kwargs)
                return Page(items=[])

        scope = SimpleNamespace(
            tenant="tenant",
            project="project",
            since=BASE,
            window_minutes=60,
        )
        query_app.app.dependency_overrides[query_app.Scope] = lambda: scope
        query_app.app.dependency_overrides[query_app.repository] = Repo
        try:
            response = TestClient(query_app.app).get(
                "/api/v1/errors?where=failure_category:is:pending_input"
            )
        finally:
            query_app.app.dependency_overrides.clear()

        assert response.status_code == 200
        assert captured["failures_only"] is False


def test_rollup_replacement_waits_for_all_active_replicas() -> None:
    """The repair command must not return while old aggregate parts are visible."""
    from scripts.recompute_rollups import RollupRecomputer

    class Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.result_rows = rows

    class Client:
        def __init__(self) -> None:
            self.commands: list[tuple[str, dict[str, int] | None]] = []

        def query(self, sql: str, parameters: object = None) -> Result:
            if "sorting_key" in sql:
                return Result([("tenant_id, project_id, bucket",)])
            return Result([(1,)])

        def command(
            self,
            sql: str,
            parameters: object = None,
            settings: dict[str, int] | None = None,
        ) -> None:
            self.commands.append((sql, settings))

    recomputer = RollupRecomputer.__new__(RollupRecomputer)
    recomputer._client = Client()
    recomputer.recompute_table("tool_metrics_1m", "2026-08-16")
    replace = next(item for item in recomputer.client.commands if "REPLACE PARTITION" in item[0])
    assert replace[1] == {"alter_sync": 2}


class TestGenericFilters:
    """The browser receives a catalog, while SQL receives only bound values."""

    def test_catalog_contains_controls_and_resolved_options(self) -> None:
        options = type("Options", (), {"servers": ["alpha"], "tools": ["fetch"],
                                        "methods": [], "categories": ["tool_error"]})()
        result = catalog("traces", options)
        server = next(f for g in result["groups"] for f in g["filters"] if f["key"] == "server")
        assert server["label"] == "Server"
        assert server["options"] == [{"value": "alpha", "label": "alpha"}]
        minimum = next(
            f for g in result["groups"] for f in g["filters"]
            if f["key"] == "min_duration_ms"
        )
        assert minimum["minimum"] == 0
        assert "column" not in server

    def test_one_config_drives_parsing_and_bound_sql(self) -> None:
        filters = parse("traces", {"server": "'; DROP", "min_duration_ms": "5", "q": "fetch"})
        params: dict[str, object] = {}
        clauses = filters.clauses(WHERE, params)
        assert "service_name = {f_server:String}" in clauses
        assert params["f_server"] == "'; DROP"
        assert params["f_min_duration_ms"] == 5.0
        assert "DROP" not in " ".join(clauses)

    def test_capability_having_and_sort_are_allowlisted(self) -> None:
        filters = parse("capabilities", {"min_calls": "3", "sort": "p95"})
        params: dict[str, object] = {}
        assert filters.clauses(HAVING, params) == ["calls >= {f_min_calls:UInt32}"]
        assert params["f_min_calls"] == 3
        assert filters.order_by() == "p95_sort DESC"

    def test_invalid_numeric_input_is_rejected_before_sql(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="min_calls is too large"):
            parse("capabilities", {"min_calls": "1000001"})


class TestQueryClientConcurrency:
    def test_shared_client_does_not_create_a_shared_clickhouse_session(self, monkeypatch) -> None:
        """FastAPI runs sync endpoints in parallel worker threads.

        clickhouse-connect rejects overlapping requests when one client sends
        the same generated session_id for both, so the shared query client must
        use the HTTP pool without a server session.
        """
        import query.app as query_app

        captured: dict[str, object] = {}

        class FakeRepository:
            def __init__(self, **connect: object) -> None:
                captured.update(connect)

        monkeypatch.setattr(query_app, "SpanRepository", FakeRepository)
        monkeypatch.setattr(query_app, "_repository", None)

        query_app.repository()

        assert captured["autogenerate_session_id"] is False


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


class TestCapabilityQueryCost:
    """The capability tables must not re-introduce a per-row query.

    They used to run a failure-breakdown query AND a latency query for every
    row, so a page cost 2N+1 round trips: measured at ~16ms each, a tenant with
    a thousand capabilities crossed the 20s `max_execution_time` and the view
    simply stopped working. The fix is only a fix while it stays constant, and
    nothing about three grouped queries LOOKS different from three per-row ones
    at twelve rows -- which is why this asserts the count rather than the shape.
    """

    @staticmethod
    def _repo_returning(row_count: int) -> tuple[SpanRepository, list[str]]:
        repo = object.__new__(SpanRepository)
        issued: list[str] = []
        names = [f"tool_{i}" for i in range(row_count)]

        def rows(sql: str, params: dict[str, object]) -> list[tuple[object, ...]]:
            issued.append(sql)
            if "GROUP BY item, failure_category" in sql:
                return [(n, "tool_error", 1) for n in names]
            # Not `is_latency_eligible` -- the main aggregate mentions it too,
            # inside the p95 sort key. `countIf(duration_ns = 0)` appears only
            # in the latency select.
            if "countIf(duration_ns = 0)" in sql:
                return [(n, 1, 1e6, 2e6, 3e6, 4e6, 0, 1e6) for n in names]
            # The main aggregate: item, service, method, calls, errors, seen, p95
            return [(n, "svc", "tools/call", 5, 1, BASE, 1.0) for n in names]

        repo._rows = rows  # type: ignore[method-assign]
        return repo, issued

    def test_query_count_is_constant_in_the_number_of_rows(self) -> None:
        for row_count in (1, 12, 200):
            repo, issued = self._repo_returning(row_count)
            page = repo.capabilities("tenant", "project", BASE, kind="tool")
            assert len(page.items) == row_count
            assert len(issued) == 3, (
                f"{row_count} rows issued {len(issued)} queries; "
                "capabilities must stay at three regardless of row count"
            )

    def test_follow_up_queries_reapply_the_row_filter(self) -> None:
        """Otherwise a breakdown counts spans the row's own total excludes.

        The per-row versions omitted the WHERE, so with two servers exposing the
        same tool name a row's failure bar could sum to more than its `calls`.
        """
        repo, issued = self._repo_returning(2)
        repo.capabilities("tenant", "project", BASE, kind="tool")
        follow_ups = [s for s in issued if "IN {items:Array(String)}" in s]
        assert len(follow_ups) == 2
        for sql in follow_ups:
            assert "mcp_method = {method:String}" in sql

    def test_rows_beyond_the_cap_are_dropped_and_reported(self) -> None:
        repo, _ = self._repo_returning(CAPABILITY_ROW_CAP + 1)
        page = repo.capabilities("tenant", "project", BASE, kind="tool")
        assert page.truncated is True
        assert len(page.items) == CAPABILITY_ROW_CAP
        assert page.cap == CAPABILITY_ROW_CAP

    def test_an_exactly_full_page_is_not_reported_as_truncated(self) -> None:
        """`cap` rows is ambiguous unless one extra row is fetched to prove it."""
        repo, _ = self._repo_returning(CAPABILITY_ROW_CAP)
        page = repo.capabilities("tenant", "project", BASE, kind="tool")
        assert page.truncated is False
        assert len(page.items) == CAPABILITY_ROW_CAP


class TestOneFailureDefinition:
    """"Did this fail" must have exactly ONE answer in the product.

    It had three. The overview's error rate excluded 401s and cancellations;
    `?status=error` excluded cancellations but counted 401s; the /errors list
    counted both. Measured over 24h of real data, the Errors page listed 62
    traces the headline error rate said were not errors -- and an error view
    that disagrees with the error rate is worse than either being wrong alone,
    because it makes both untrustworthy.
    """

    def test_failure_definition_matches_the_taxonomy(self) -> None:
        """The read plane mirrors the writer's definition.

        The query image does not ship normalizer/, so query.dtos cannot import
        FailureTaxonomy -- the constant is duplicated across a deployment
        boundary on purpose. This is what stops the duplicate rotting.
        """
        from normalizer.taxonomy import FailureTaxonomy

        # The taxonomy has no "" entry: it classifies, so it never emits blank.
        # The read plane sees unclassified rows and must treat them as verdicts
        # withheld rather than failures.
        assert set(NOT_A_FAILURE) - {""} == set(FailureTaxonomy.NOT_A_FAILURE)

    def test_status_filter_and_error_list_use_the_same_set(self) -> None:
        from query.filters import parse

        params: dict[str, object] = {}
        clauses = parse("traces", {"status": "error"}).clauses(WHERE, params)
        assert len(clauses) == 1
        for category in NOT_A_FAILURE:
            assert f"'{category}'" in clauses[0], f"{category} missing from ?status=error"

    def test_errors_list_excludes_everything_the_overview_excludes(self) -> None:
        repo = object.__new__(SpanRepository)
        captured: dict[str, str] = {}

        def rows(sql: str, params: dict[str, object]) -> list[tuple[object, ...]]:
            captured["sql"] = sql
            return []

        repo._rows = rows  # type: ignore[method-assign]
        repo.traces("tenant", "project", BASE, failures_only=True)
        for category in NOT_A_FAILURE:
            assert f"'{category}'" in captured["sql"], f"{category} would appear in /errors"

    def test_breakdown_failures_counts_exactly_the_rest(self) -> None:
        counts = dict.fromkeys(FailureBreakdown().model_dump(), 1)
        breakdown = FailureBreakdown(**counts)
        expected = len([n for n in counts if n not in NOT_A_FAILURE])
        assert breakdown.failures == expected


class TestDownstreamHostFallback:
    """A client HTTP span must say which host it called.

    Measured against the running stack: the httpx instrumentor emits only
    http.method, http.url and http.status_code -- no host attribute of any
    kind. So every outbound call stored a blank host while the host sat inside
    a field beside it, and the console could not distinguish three partner APIs
    without the reader parsing a URL.
    """

    @staticmethod
    def _host(attrs: dict[str, object]) -> str:
        from normalizer.normalize import SpanNormalizer

        return SpanNormalizer.__new__(SpanNormalizer)._http(attrs)[2]

    def test_host_is_recovered_from_the_url(self) -> None:
        assert self._host({"http.url": "http://127.0.0.1:8801/v1/charges"}) == "127.0.0.1:8801"
        assert self._host({"url.full": "https://api.example.com/v1?k=1"}) == "api.example.com"

    def test_an_explicit_host_attribute_still_wins(self) -> None:
        attrs = {"server.address": "explicit.host", "http.url": "http://other.host/x"}
        assert self._host(attrs) == "explicit.host"

    def test_credentials_in_the_url_are_never_stored(self) -> None:
        """`https://user:pw@host/` is a legal URL and a credential.

        This value is a low-cardinality dimension people group by -- the worst
        possible place for a secret to surface.
        """
        assert self._host({"http.url": "https://user:pw@internal.example/x"}) == "internal.example"

    def test_a_missing_or_unparseable_url_yields_empty(self) -> None:
        assert self._host({}) == ""
        assert self._host({"http.url": "not a url"}) == ""


class TestTraceSpanCap:
    """One trace must not be able to return an unbounded response.

    `GET /traces/{id}` had no LIMIT at all: every span, plus a full ~60-column
    SpanDetail for each. Measured at 2.4 KB per span, so a 10,000-span trace
    would have been a 24 MB response and a browser asked to draw 10,000
    waterfall rows.

    Not a hypothetical shape here. Progress reports were already capped at 200
    because "a tool can generate spans faster than it does work" -- that
    reasoning was simply never applied anywhere else. The unbounded case is
    ordinary: a tool that loops. `for row in rows: cur.execute(...)` over ten
    thousand rows is ten thousand child spans in one trace.
    """

    @staticmethod
    def _repo_with(span_count: int) -> SpanRepository:
        repo = object.__new__(SpanRepository)
        base = datetime(2026, 8, 15, 12, 0, 0)

        def rows(sql: str, params: dict[str, object]) -> list[tuple[object, ...]]:
            if "trace_locator" in sql:
                return [(base.date(),)]
            # The cap is applied in SQL; emulate it so the test exercises the
            # same +1 detection the query relies on.
            limit = int(params.get("cap", span_count))
            out = []
            for i in range(min(span_count, limit)):
                row = {c: None for c in SpanRepository._SPAN_COLUMNS}
                row["span_id"] = f"s{i:05d}"
                row["parent_span_id"] = "" if i == 0 else "s00000"
                row["span_name"] = f"span-{i}"
                row["timestamp"] = base + timedelta(milliseconds=i)
                row["duration_ns"] = 1_000_000
                row["span_kind"] = "INTERNAL"
                row["status_code"] = "OK"
                row["normalization_version"] = 17
                row["kafka_partition"] = 0
                row["kafka_offset"] = i
                out.append(tuple(row[c] for c in SpanRepository._SPAN_COLUMNS))
            return out

        repo._rows = rows  # type: ignore[method-assign]
        return repo

    def test_the_query_asks_for_one_span_past_the_cap(self) -> None:
        """Exactly `cap` rows is ambiguous between "all of it" and "there is more"."""
        repo = object.__new__(SpanRepository)
        seen: dict[str, object] = {}

        def rows(sql: str, params: dict[str, object]) -> list[tuple[object, ...]]:
            if "trace_locator" in sql:
                return [(datetime(2026, 8, 15).date(),)]
            seen.update(params)
            return []

        repo._rows = rows  # type: ignore[method-assign]
        repo.trace("tenant", "project", "abc")
        assert seen["cap"] == TRACE_SPAN_CAP + 1

    def test_a_trace_within_the_cap_is_not_marked_truncated(self) -> None:
        detail = self._repo_with(10).trace("tenant", "project", "abc")
        assert detail is not None
        assert detail.truncated is False
        assert detail.span_count == 10

    def test_a_trace_over_the_cap_is_truncated_and_says_so(self) -> None:
        detail = self._repo_with(TRACE_SPAN_CAP + 500).trace("tenant", "project", "abc")
        assert detail is not None
        assert detail.truncated is True
        assert detail.span_count == TRACE_SPAN_CAP
        assert detail.span_cap == TRACE_SPAN_CAP

    def test_truncation_keeps_the_earliest_spans(self) -> None:
        """The TAIL is dropped, so a truncated waterfall still reads forwards.

        Ordered by time in SQL before the slice; dropping an arbitrary middle
        would make the remaining tree nonsense.
        """
        detail = self._repo_with(TRACE_SPAN_CAP + 500).trace("tenant", "project", "abc")
        assert detail is not None
        starts = [s.start_time for s in detail.spans]
        assert min(starts) == datetime(2026, 8, 15, 12, 0, 0)
        assert max(starts) < datetime(2026, 8, 15, 12, 0, 0) + timedelta(
            milliseconds=TRACE_SPAN_CAP
        )

    def test_detail_is_omitted_on_large_traces_but_spans_are_not(self) -> None:
        """Size, not correctness: the waterfall stays complete."""
        detail = self._repo_with(TRACE_DETAIL_CAP + 50).trace("tenant", "project", "abc")
        assert detail is not None
        assert detail.detail_omitted is True
        assert detail.detail == {}
        assert detail.detail_cap == TRACE_DETAIL_CAP
        assert len(detail.spans) == TRACE_DETAIL_CAP + 50

    def test_detail_is_present_on_ordinary_traces(self) -> None:
        detail = self._repo_with(12).trace("tenant", "project", "abc")
        assert detail is not None
        assert detail.detail_omitted is False
        assert len(detail.detail) == 12
