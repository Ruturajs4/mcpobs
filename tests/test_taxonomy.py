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


class TestServerInitiatedRequests:
    """`sampling/createMessage`, `elicitation/create`, `roots/list`.

    These are SERVER-INITIATED: the server asks the client, and the client may
    take as long as a model generation or a human decision to answer. That
    duration is think-time, not a measure of how fast the server is -- the same
    reasoning that excludes MRTR interim rounds (D28).

    None has ever been observed in this stack, because the stateless 2026-07-28
    transport has no back-channel to issue them on. They are handled anyway: the
    cost is nothing, and the cost of NOT handling them is a p95 poisoned by
    human approval time the first time a customer runs a stateful transport.
    """

    def span(self, method: str, **attrs: object):
        from normalizer.models import DecodedSpan

        return DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={"mcp.method.name": method, **attrs},
        )

    def setup_method(self) -> None:
        from normalizer.taxonomy import FailureTaxonomy

        self.taxonomy = FailureTaxonomy()

    def test_sampling_never_enters_a_latency_aggregate(self) -> None:
        """A model generating a completion can take tens of seconds. One of
        those in a p95 destroys the chart exactly as a stream lifetime does."""
        assert not self.taxonomy.is_latency_eligible(self.span("sampling/createMessage"))

    def test_elicitation_never_enters_a_latency_aggregate(self) -> None:
        """Bounded by a human deciding, which is not a server metric."""
        assert not self.taxonomy.is_latency_eligible(self.span("elicitation/create"))

    def test_roots_list_never_enters_a_latency_aggregate(self) -> None:
        assert not self.taxonomy.is_latency_eligible(self.span("roots/list"))

    def test_ordinary_methods_are_still_eligible(self) -> None:
        """The exclusion must stay narrow. `tools/list` runs on every client
        connect and a slow one is a real customer symptom (D53)."""
        for method in ("tools/call", "tools/list", "prompts/get", "resources/read",
                       "server/discover"):
            assert self.taxonomy.is_latency_eligible(self.span(method)), method

    def test_they_are_not_treated_as_failures(self) -> None:
        """Excluded from LATENCY, not from success. Classifying a sampling
        round as an error would corrupt the error rate, which is the single
        most likely way to discredit the product (Day-1 doc)."""
        category = self.taxonomy.classify(self.span("sampling/createMessage"))
        assert not self.taxonomy.is_error(category), category


class TestResultTypeReachesTheColumn:
    def test_the_helper_attribute_populates_result_type(self) -> None:
        """The SDK emits no `mcp.result.type`, so the helper sets
        `mcpobs.result.type`. The taxonomy always read both; the COLUMN read
        only the SDK name, so `result_type` was empty on every MRTR round while
        `failure_category` beside it correctly said `pending_input`. Present
        data, discarded on the way to the column."""
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        row = SpanNormalizer().to_row(DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={
                "mcp.method.name": "tools/call",
                "mcpobs.result.type": "input_required",
            },
        ))
        assert row.result_type == "input_required"
        assert row.failure_category == "pending_input"
        assert row.is_latency_eligible == 0

    def test_the_sdk_attribute_still_wins_if_it_ever_appears(self) -> None:
        from normalizer.models import DecodedSpan
        from normalizer.normalize import SpanNormalizer

        row = SpanNormalizer().to_row(DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={
                "mcp.method.name": "tools/call",
                "mcp.result.type": "complete",
                "mcpobs.result.type": "input_required",
            },
        ))
        assert row.result_type == "complete"


class TestCancellation:
    """A cancelled call is neither a success nor a server failure.

    Measured before this existed: a cancelled `tools/call` produced
    `status_code=UNSET`, `failure_category=ok`, `is_latency_eligible=1`. So it
    inflated the success count AND deflated the latency percentiles, because its
    duration measures how long the client waited before giving up.
    """

    def span(self, **attrs: object):
        from normalizer.models import DecodedSpan

        return DecodedSpan(
            trace_id="a" * 32, span_id="b" * 16,
            span_attributes={"mcp.method.name": "tools/call", **attrs},
        )

    def setup_method(self) -> None:
        from normalizer.taxonomy import FailureTaxonomy

        self.taxonomy = FailureTaxonomy()

    def test_a_cancelled_call_is_its_own_category(self) -> None:
        assert self.taxonomy.classify(self.span(**{"mcpobs.cancelled": True})) == "cancelled"

    def test_a_cancelled_call_is_not_an_error(self) -> None:
        """The client stopping is not the server failing. Counting it would
        corrupt the one number a customer judges the product by."""
        assert not self.taxonomy.is_error("cancelled")

    def test_a_cancelled_call_is_not_a_latency_sample(self) -> None:
        """Its duration is how long the CLIENT waited, not how long the tool
        takes -- so a tool cancelled because it is slow would look fast."""
        assert not self.taxonomy.is_latency_eligible(self.span(**{"mcpobs.cancelled": True}))

    def test_an_ordinary_call_is_unaffected(self) -> None:
        assert self.taxonomy.classify(self.span()) == "ok"
        assert self.taxonomy.is_latency_eligible(self.span())

    def test_cancellation_outranks_an_error_status(self) -> None:
        """A cancelled handler often leaves an ERROR status behind it. The
        cancellation is the true cause and must win, or every cancellation
        reappears as a server_exception."""
        span = self.span(**{"mcpobs.cancelled": True, "error.type": "tool_error"})
        span.status_code = "ERROR"
        assert self.taxonomy.classify(span) == "cancelled"
