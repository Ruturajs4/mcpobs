"""Flat OTLP span -> ClickHouse row.

Stateless by design (Architecture.md ADR-005): one span in, one row out. No
cross-span state, no windowing, no trace assembly -- that happens in ClickHouse.

Attribute keys come from docs/observed_attributes.md (T3), which outranks any
document where they disagree (Day-1 doc D10).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from normalizer.taxonomy import classify

NORMALIZATION_VERSION = 1

# Attributes we promote to columns; everything else stays in the raw maps.
MCP_METHOD = "mcp.method.name"
MCP_TOOL = "gen_ai.tool.name"
MCP_PROMPT = "gen_ai.prompt.name"
MCP_OPERATION = "gen_ai.operation.name"
MCP_RESOURCE_URI = "mcp.resource.uri"
MCP_PROTOCOL_VERSION = "mcp.protocol.version"
MCP_SESSION_ID = "mcp.session.id"
JSONRPC_REQUEST_ID = "jsonrpc.request.id"
ERROR_TYPE = "error.type"
RPC_STATUS = "rpc.response.status_code"
TRANSPORT = "network.transport"

RES_SERVICE_NAME = "service.name"
RES_SERVICE_VERSION = "service.version"
RES_ENVIRONMENT = "deployment.environment.name"
RES_TENANT = "tenant.id"
RES_PROJECT = "project.id"


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _http(attrs: dict) -> tuple[str, int | None, str]:
    """Downstream HTTP dimensions, tolerating old and new semconv names."""
    method = attrs.get("http.request.method") or attrs.get("http.method") or ""
    status = attrs.get("http.response.status_code") or attrs.get("http.status_code")
    host = (
        attrs.get("server.address")
        or attrs.get("net.peer.name")
        or attrs.get("http.host")
        or ""
    )
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return str(method), status, str(host)


def to_row(span: dict, *, partition: int = -1, offset: int = -1) -> dict[str, Any]:
    attrs: dict = span.get("span_attributes") or {}
    res: dict = span.get("resource_attributes") or {}

    error_type = attrs.get(ERROR_TYPE)
    rpc_status = attrs.get(RPC_STATUS)
    result_type = attrs.get("mcp.result.type")  # not emitted today

    # Only MCP protocol spans get an MCP failure category. Downstream child
    # spans (httpx, db, ...) are classified by their own semantics on Day 2;
    # giving a failing HTTP GET a category of `unclassified` would pollute the
    # taxonomy's health metric, which is precisely the signal we rely on.
    is_mcp_span = MCP_METHOD in attrs
    failure_category = (
        classify(
            status_code=span.get("status_code", "UNSET"),
            error_type=_s(error_type) or None,
            rpc_status_code=_s(rpc_status) or None,
            result_type=_s(result_type) or None,
            has_exception_event="exception" in (span.get("event_names") or []),
        )
        if is_mcp_span
        else ""
    )

    http_method, http_status, http_host = _http(attrs)
    session_id = attrs.get(MCP_SESSION_ID)

    timestamp = datetime.fromtimestamp(
        span.get("start_unix_nano", 0) / 1e9, tz=timezone.utc
    ).replace(tzinfo=None)

    return {
        "tenant_id": _s(res.get(RES_TENANT)) or "local",
        "project_id": _s(res.get(RES_PROJECT)) or "local",
        "environment": _s(res.get(RES_ENVIRONMENT)) or "local",
        "timestamp": timestamp,
        "duration_ns": int(span.get("duration_ns", 0)),
        "trace_id": span["trace_id"],
        "span_id": span["span_id"],
        "parent_span_id": span.get("parent_span_id", ""),
        "span_name": span.get("span_name", ""),
        "span_kind": span.get("span_kind", ""),
        "status_code": span.get("status_code", "UNSET"),
        "status_message": span.get("status_message", ""),
        "service_name": _s(res.get(RES_SERVICE_NAME)),
        "service_version": _s(res.get(RES_SERVICE_VERSION)),
        "deployment_environment": _s(res.get(RES_ENVIRONMENT)),
        # MCP -- free-form strings, never validated against an enum (ADR: D8).
        # server/discover, subscriptions/listen and tasks/* are not in the OTel
        # well-known list and must pass through untouched.
        "mcp_method": _s(attrs.get(MCP_METHOD)),
        "mcp_tool_name": _s(attrs.get(MCP_TOOL)),
        "gen_ai_operation": _s(attrs.get(MCP_OPERATION)),
        "protocol_version": _s(attrs.get(MCP_PROTOCOL_VERSION)),
        "jsonrpc_request_id": _s(attrs.get(JSONRPC_REQUEST_ID)),
        "mcp_prompt_name": _s(attrs.get(MCP_PROMPT)),
        "mcp_resource_uri": _s(attrs.get(MCP_RESOURCE_URI)),
        "transport": _s(attrs.get(TRANSPORT)),
        "mcp_session_id": _s(session_id) if session_id is not None else None,
        "mcp_is_error": 1 if is_mcp_span and failure_category not in ("ok", "pending_input") else 0,
        "result_type": _s(result_type),
        "failure_category": failure_category,
        "error_type": _s(error_type),
        "rpc_status_code": _s(rpc_status) if rpc_status is not None else None,
        "http_method": http_method,
        "http_status_code": http_status,
        "http_host": http_host,
        "db_system": _s(attrs.get("db.system")),
        "input_size": None,
        "output_size": None,
        "input_preview": None,
        "output_preview": None,
        "resource_attributes": {k: _s(v) for k, v in res.items()},
        "span_attributes": {k: _s(v) for k, v in attrs.items()},
        "normalization_version": NORMALIZATION_VERSION,
        "kafka_partition": partition,
        "kafka_offset": offset,
    }


COLUMNS = [
    "tenant_id", "project_id", "environment", "timestamp", "duration_ns",
    "trace_id", "span_id", "parent_span_id", "span_name", "span_kind",
    "status_code", "status_message", "service_name", "service_version",
    "deployment_environment", "mcp_method", "mcp_tool_name", "gen_ai_operation",
    "protocol_version", "jsonrpc_request_id", "mcp_prompt_name",
    "mcp_resource_uri", "transport", "mcp_session_id", "mcp_is_error",
    "result_type", "failure_category", "error_type", "rpc_status_code",
    "http_method", "http_status_code", "http_host", "db_system",
    "input_size", "output_size", "input_preview", "output_preview",
    "resource_attributes", "span_attributes", "normalization_version",
    "kafka_partition", "kafka_offset",
]
