"""MCP failure taxonomy.

Versioned deliberately: derived categories are additive and reproducible from
raw attributes, so a taxonomy change is a replay (Architecture.md §5.4), not a
data migration.

WHAT THE SDK SOURCE SUGGESTS IS POSSIBLE (mcp/server/_otel.py:45-60)
    error.type == "tool_error"   -> tool ran and returned isError
    error.type == "<QualName>"   -> handler raised
    error.type == "-32602" etc.  -> protocol/validation failure

WHAT ACTUALLY HAPPENS (docs/observed_attributes.md)
    All four failure modes -- isError result, raised RuntimeError, unknown
    tool, schema-violating argument -- produce an IDENTICAL span:
    status=ERROR, error.type="tool_error", no rpc.response.status_code, no
    exception event.

    MCPServer's tool handler converts every failure to
    CallToolResult(isError=True) before OpenTelemetryMiddleware sees it, so the
    middleware's `except` branches are unreachable for tools/call.

RESOLVED (D17). The distinguishing text is SDK-generated boilerplate carried in
the result, so the optional `mcpobs` helper middleware classifies it in the
customer's process -- without capturing any tool content -- and annotates the
span the SDK already opened. When that attribute is present it WINS, because the
helper saw the failure before the SDK erased the distinction.

Without the helper we degrade to the coarse `tool_error` rather than inventing a
precision the span cannot support; `failure_kind_source` records which, so the
two data qualities never silently mix (D21).
"""

from __future__ import annotations

import re
from typing import Final

from normalizer.models import DecodedSpan


class Category:
    OK: Final = "ok"
    TOOL_ERROR: Final = "tool_error"
    SERVER_EXCEPTION: Final = "server_exception"
    PROTOCOL_ERROR: Final = "protocol_error"
    PENDING_INPUT: Final = "pending_input"
    #: The client gave up before the tool finished. NOT a server failure.
    CANCELLED: Final = "cancelled"
    UNCLASSIFIED: Final = "unclassified"


class FailureTaxonomy:
    """Classifies one span into a failure category.

    Stateless and side-effect free, so it is trivially unit-testable and safe to
    call from any consumer replica.
    """

    version: Final[int] = 1

    #: Categories Day-1 telemetry can actually produce. Anything else appearing
    #: in ClickHouse means the SDK changed -- which is worth knowing.
    REACHABLE_TODAY: Final[frozenset[str]] = frozenset({Category.OK, Category.TOOL_ERROR})

    _NUMERIC: Final = re.compile(r"^-?\d+$")

    # Attribute keys read by the taxonomy.
    ERROR_TYPE: Final = "error.type"
    RPC_STATUS: Final = "rpc.response.status_code"
    RESULT_TYPE: Final = "mcp.result.type"
    MCP_METHOD: Final = "mcp.method.name"

    # Set by the optional helper middleware (mcpobs). When present it carries a
    # real distinction the raw span cannot express; when absent we degrade to
    # the coarse classification rather than inventing one.
    HELPER_KIND: Final = "mcpobs.failure.kind"
    HELPER_RESULT_TYPE: Final = "mcpobs.result.type"
    CANCELLED_ATTR: Final = "mcpobs.cancelled"
    HELPER_VERSION: Final = "mcpobs.failure.kind.version"

    SOURCE_HELPER: Final = "helper"
    SOURCE_SPAN: Final = "span"

    #: Methods whose span duration is NOT a latency measurement. A
    #: `subscriptions/listen` span lasts as long as the stream does, so one of
    #: them in a p95 destroys the chart. Matched by prefix, never an enum: the
    #: protocol keeps adding methods (D8).
    NON_LATENCY_METHODS: Final[tuple[str, ...]] = (
        # A stream lifetime, not a latency.
        "subscriptions/listen",
        # SERVER-INITIATED requests. Their duration is dominated by the time the
        # CLIENT takes to answer -- a model generating a completion, or a human
        # approving an elicitation. That is think-time, exactly like an MRTR
        # interim round, and it is not a measure of how fast this server is.
        #
        # Listed even though the current stateless transport has no back-channel
        # to issue them on, and none has ever been observed here. The cost of
        # listing them is nothing; the cost of not listing them is a p95
        # silently poisoned by human approval time the first time a customer
        # runs a stateful transport, which is the kind of defect that is only
        # ever found by not trusting the number.
        "sampling/createMessage",
        "elicitation/create",
        "roots/list",
    )

    def is_latency_eligible(self, span: DecodedSpan) -> bool:
        """False when a span's duration must never enter a latency aggregate.

        Two cases:
          * long-lived streams -- the duration is a stream lifetime;
          * MRTR interim rounds -- the duration excludes the client think-time
            that dominates the real wait, so it understates badly (D28).
        """
        attrs = span.span_attributes
        method = str(attrs.get(self.MCP_METHOD, ""))
        if any(method.startswith(prefix) for prefix in self.NON_LATENCY_METHODS):
            return False
        # A cancelled call's duration is HOW LONG THE CLIENT WAITED before
        # giving up, not how long the tool takes. Including it makes a tool that
        # is cancelled BECAUSE it is slow look fast -- an error running in the
        # most misleading possible direction.
        if attrs.get(self.CANCELLED_ATTR):
            return False
        result_type = attrs.get(self.RESULT_TYPE) or attrs.get(self.HELPER_RESULT_TYPE)
        return result_type != "input_required"

    #: Any of these means the helper middleware was attached and contributed
    #: the category. `mcpobs.failure.kind` was the only one when this was
    #: written, and a cancelled call -- classified from `mcpobs.cancelled` --
    #: was therefore reported as span-derived, i.e. as evidence the helper was
    #: MISSING from a server it was demonstrably running in.
    HELPER_MARKERS: Final[tuple[str, ...]] = (
        "mcpobs.failure.kind",
        "mcpobs.cancelled",
        "mcpobs.result.type",
    )

    def source(self, span: DecodedSpan) -> str:
        """Where the category came from -- helper middleware or the bare span."""
        attrs = span.span_attributes
        return (
            self.SOURCE_HELPER
            if any(marker in attrs for marker in self.HELPER_MARKERS)
            else self.SOURCE_SPAN
        )

    def classify(self, span: DecodedSpan) -> str:
        """Return the failure category for a span.

        Non-MCP spans (downstream httpx, db, ...) get no MCP category: giving a
        failing HTTP GET `unclassified` would pollute the taxonomy's health
        metric, which is the very signal we rely on to detect SDK drift.
        """
        attrs = span.span_attributes
        if self.MCP_METHOD not in attrs:
            return ""

        error_type = self._str(attrs.get(self.ERROR_TYPE))
        rpc_status = self._str(attrs.get(self.RPC_STATUS))
        # The SDK's own span carries no resultType; the helper middleware reads
        # it off the sealed wire form and puts it there.
        result_type = self._str(attrs.get(self.RESULT_TYPE)) or self._str(
            attrs.get(self.HELPER_RESULT_TYPE)
        )

        # Neither of these is a failure, and both are checked BEFORE anything
        # else so neither can ever be counted into an error rate.
        #
        # MRTR interim results: the round asked a question and is waiting.
        if result_type == "input_required":
            return Category.PENDING_INPUT

        # Cancellation: the CLIENT gave up. Measured to land as `ok` before
        # this existed, so a cancelled call inflated the success count while its
        # truncated duration deflated the latency percentiles.
        if attrs.get(self.CANCELLED_ATTR):
            return Category.CANCELLED

        # The helper's classification wins when present: it saw the failure
        # before the SDK erased the distinction. It is never *less* precise than
        # what we could derive from the span alone.
        helper_kind = self._str(attrs.get(self.HELPER_KIND))
        if helper_kind:
            return helper_kind

        failed = span.status_code == "ERROR"
        if not error_type and not failed:
            return Category.OK

        if error_type == "tool_error":
            return Category.TOOL_ERROR

        # A numeric error.type is a JSON-RPC code (-32020..-32022, -32602, ...):
        # the protocol layer rejected the request, no tool ever ran.
        if self._NUMERIC.match(error_type) or self._NUMERIC.match(rpc_status):
            return Category.PROTOCOL_ERROR

        if error_type or span.has_exception_event:
            return Category.SERVER_EXCEPTION

        return Category.UNCLASSIFIED if failed else Category.OK

    #: Categories that are NOT failures. Both are outcomes the client chose:
    #: one is waiting for an answer, the other stopped asking. Counting either
    #: as a server error would corrupt the single number a customer judges the
    #: product by.
    NOT_A_FAILURE: Final[tuple[str, ...]] = (Category.OK, Category.PENDING_INPUT,
                                             Category.CANCELLED)

    def is_error(self, category: str) -> bool:
        return bool(category) and category not in self.NOT_A_FAILURE

    @staticmethod
    def _str(value: object) -> str:
        return "" if value is None else str(value)
