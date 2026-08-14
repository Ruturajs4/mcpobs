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

Only `ok` and `tool_error` are reachable today. The other branches stay
implemented because they ARE reachable for non-tools/call methods and for
future SDK versions -- but must not be advertised as working. See
docs/decisions.md D13.
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
        result_type = self._str(attrs.get(self.RESULT_TYPE))

        # MRTR interim results are NOT failures. Undetectable today -- the SDK
        # emits no resultType -- but the branch exists so that the day it
        # becomes visible it cannot be miscounted as an error.
        if result_type == "input_required":
            return Category.PENDING_INPUT

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

    def is_error(self, category: str) -> bool:
        return bool(category) and category not in (Category.OK, Category.PENDING_INPUT)

    @staticmethod
    def _str(value: object) -> str:
        return "" if value is None else str(value)
