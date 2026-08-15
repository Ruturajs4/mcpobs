"""Failure taxonomy branches.

Includes the branches that are currently UNREACHABLE from the SDK (D13). They
are tested anyway: they are reachable for non-tools/call methods and for future
SDK versions, and the day the SDK starts emitting them we want the mapping to
already be correct rather than discovered in production.
"""

from __future__ import annotations

from normalizer.taxonomy import Category, FailureTaxonomy


class TestClassify:
    def setup_method(self) -> None:
        self.taxonomy = FailureTaxonomy()

    def test_clean_success_is_ok(self, span_factory) -> None:
        assert self.taxonomy.classify(span_factory()) == Category.OK

    def test_tool_error(self, span_factory) -> None:
        span = span_factory(
            status_code="ERROR", span_attributes={"error.type": "tool_error"}
        )
        assert self.taxonomy.classify(span) == Category.TOOL_ERROR

    def test_numeric_error_type_is_protocol_error(self, span_factory) -> None:
        span = span_factory(status_code="ERROR", span_attributes={"error.type": "-32602"})
        assert self.taxonomy.classify(span) == Category.PROTOCOL_ERROR

    def test_rpc_status_code_alone_is_protocol_error(self, span_factory) -> None:
        span = span_factory(
            status_code="ERROR", span_attributes={"rpc.response.status_code": "-32022"}
        )
        assert self.taxonomy.classify(span) == Category.PROTOCOL_ERROR

    def test_exception_qualname_is_server_exception(self, span_factory) -> None:
        span = span_factory(status_code="ERROR", span_attributes={"error.type": "RuntimeError"})
        assert self.taxonomy.classify(span) == Category.SERVER_EXCEPTION

    def test_exception_event_without_error_type(self, span_factory) -> None:
        span = span_factory(status_code="ERROR", event_names=["exception"])
        assert self.taxonomy.classify(span) == Category.SERVER_EXCEPTION

    def test_input_required_is_not_a_failure(self, span_factory) -> None:
        """MRTR interim results must never be counted as errors."""
        span = span_factory(span_attributes={"mcp.result.type": "input_required"})
        category = self.taxonomy.classify(span)
        assert category == Category.PENDING_INPUT
        assert not self.taxonomy.is_error(category)

    def test_error_status_with_no_signal_is_unclassified(self, span_factory) -> None:
        span = span_factory(status_code="ERROR")
        assert self.taxonomy.classify(span) == Category.UNCLASSIFIED

    def test_non_mcp_span_gets_no_category(self, span_factory) -> None:
        """A failing downstream GET must not pollute the taxonomy health metric."""
        span = span_factory(
            status_code="ERROR",
            span_name="GET",
            span_attributes={"http.request.method": "GET"},
        )
        span.span_attributes.pop("mcp.method.name")
        category = self.taxonomy.classify(span)
        assert category == ""
        assert not self.taxonomy.is_error(category)


class TestIsError:
    def setup_method(self) -> None:
        self.taxonomy = FailureTaxonomy()

    def test_ok_and_pending_are_not_errors(self) -> None:
        assert not self.taxonomy.is_error(Category.OK)
        assert not self.taxonomy.is_error(Category.PENDING_INPUT)
        assert not self.taxonomy.is_error("")

    def test_failures_are_errors(self) -> None:
        for category in (
            Category.TOOL_ERROR,
            Category.SERVER_EXCEPTION,
            Category.PROTOCOL_ERROR,
            Category.UNCLASSIFIED,
        ):
            assert self.taxonomy.is_error(category)

    def test_reachable_set_documents_the_known_gap(self) -> None:
        """If this starts failing, the SDK changed and D13 needs revisiting."""
        assert {Category.OK, Category.TOOL_ERROR} == FailureTaxonomy.REACHABLE_TODAY


class TestLatencyEligibility:
    """Stream lifetimes and MRTR interim rounds must never enter a p95."""

    def setup_method(self) -> None:
        self.taxonomy = FailureTaxonomy()

    def test_ordinary_tool_call_is_eligible(self, span_factory) -> None:
        assert self.taxonomy.is_latency_eligible(span_factory())

    def test_subscriptions_listen_is_not_eligible(self, span_factory) -> None:
        """Its duration is a stream lifetime, not a latency."""
        span = span_factory(span_attributes={"mcp.method.name": "subscriptions/listen"})
        assert not self.taxonomy.is_latency_eligible(span)

    def test_mrtr_interim_round_is_not_eligible(self, span_factory) -> None:
        """Measured at ~135x understatement: it excludes client think-time."""
        span = span_factory(span_attributes={"mcpobs.result.type": "input_required"})
        assert not self.taxonomy.is_latency_eligible(span)

    def test_completing_mrtr_round_is_eligible(self, span_factory) -> None:
        span = span_factory(span_attributes={"mcpobs.mrtr.state.in": "abc123"})
        assert self.taxonomy.is_latency_eligible(span)
