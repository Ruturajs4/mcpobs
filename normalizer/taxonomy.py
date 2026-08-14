"""MCP failure taxonomy v0.

Derived from mcp/server/_otel.py AND from what T3 actually observed --
which are not the same thing, and the difference is the most important
finding of Day 1.

WHAT THE SDK SOURCE SUGGESTS IS POSSIBLE
    error.type == "tool_error"    -> the tool ran and returned isError
    error.type == "<QualName>"    -> the handler raised
    error.type == "-32602" etc.   -> protocol/validation failure, with
                                     rpc.response.status_code set

WHAT ACTUALLY HAPPENS (docs/observed_attributes.md)
    All four failure modes -- isError result, raised RuntimeError, unknown
    tool, schema-violating argument -- produce an IDENTICAL span:
    status=ERROR, error.type="tool_error", no rpc.response.status_code,
    no exception event.

    Cause: MCPServer's tool handler catches everything and converts it to
    CallToolResult(isError=True) before OpenTelemetryMiddleware sees the
    result, so the middleware's `except` branches are unreachable for
    anything routed through tools/call.

CONSEQUENCE
    Only `ok` and `tool_error` are reachable today for tools/call.
    `server_exception` and `protocol_error` remain implemented because they
    ARE reachable for non-tools/call methods and for future SDK versions --
    but they must not be advertised as a working product feature yet.
    See docs/decisions.md D13.
"""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"^-?\d+$")

OK = "ok"
TOOL_ERROR = "tool_error"
SERVER_EXCEPTION = "server_exception"
PROTOCOL_ERROR = "protocol_error"
PENDING_INPUT = "pending_input"
UNCLASSIFIED = "unclassified"

# Categories that Day-1 telemetry can actually produce. Anything else appearing
# in ClickHouse means the SDK changed -- which is worth knowing.
REACHABLE_TODAY = {OK, TOOL_ERROR}


def classify(
    *,
    status_code: str,
    error_type: str | None,
    rpc_status_code: str | None,
    result_type: str | None = None,
    has_exception_event: bool = False,
) -> str:
    """Return the failure_category for one span."""
    # MRTR interim results are NOT failures. Not detectable on Day 1 -- the SDK
    # emits no resultType attribute -- but the branch is here so that the day it
    # becomes visible, it cannot be miscounted as an error.
    if result_type == "input_required":
        return PENDING_INPUT

    if not error_type and status_code != "ERROR":
        return OK

    if error_type == "tool_error":
        return TOOL_ERROR

    # A numeric error.type is a JSON-RPC code (-32020..-32022, -32602, ...):
    # the protocol layer rejected the request, no tool ever ran.
    if error_type and _NUMERIC.match(error_type):
        return PROTOCOL_ERROR
    if rpc_status_code and _NUMERIC.match(rpc_status_code):
        return PROTOCOL_ERROR

    # An exception qualname, or a recorded exception event.
    if error_type or has_exception_event:
        return SERVER_EXCEPTION

    if status_code == "ERROR":
        return UNCLASSIFIED

    return OK
