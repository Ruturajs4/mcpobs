"""Typed row models.

`SpanRow` is the single declaration of the ClickHouse span schema. The column
list is *derived* from it (`SpanRow.columns()`), which removes the failure mode
the previous version had: a hand-maintained COLUMNS list drifting from the dict
keys, silently writing values into the wrong columns with no exception raised.

Field order here IS the insert column order. Keep it aligned with
schema/001_spans_raw.sql.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class _Row(BaseModel):
    """Base for anything inserted into ClickHouse."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    _columns_cache: ClassVar[list[str] | None] = None

    @classmethod
    def columns(cls) -> list[str]:
        """Insert column names, in declaration order."""
        if cls._columns_cache is None:
            cls._columns_cache = list(cls.model_fields)
        return cls._columns_cache

    def values(self) -> list[Any]:
        """Row values ordered to match `columns()`."""
        return [getattr(self, name) for name in self.columns()]


class SpanRow(_Row):
    # tenancy
    tenant_id: str = "local"
    project_id: str = "local"
    environment: str = "local"

    # trace
    timestamp: datetime
    duration_ns: int = 0
    trace_id: str
    span_id: str
    parent_span_id: str = ""
    span_name: str = ""
    span_kind: str = ""
    status_code: str = "UNSET"
    status_message: str = ""

    # service (from OTel Resource, never span attributes)
    service_name: str = ""
    service_version: str = ""
    deployment_environment: str = ""

    # MCP -- observed (docs/observed_attributes.md)
    mcp_method: str = ""
    mcp_tool_name: str = ""
    gen_ai_operation: str = ""
    protocol_version: str = ""
    jsonrpc_request_id: str = ""

    # MCP -- not emitted by mcp 2.0.0; kept so the schema is stable
    mcp_prompt_name: str = ""
    mcp_resource_uri: str = ""
    transport: str = ""
    mcp_session_id: str | None = None

    # failure
    mcp_is_error: int = 0
    result_type: str = ""
    failure_category: str = ""
    error_type: str = ""
    rpc_status_code: str | None = None

    # downstream
    http_method: str = ""
    http_status_code: int | None = None
    http_host: str = ""
    db_system: str = ""

    # payload (opt-in per V2 §15; NULL today)
    input_size: int | None = None
    output_size: int | None = None
    input_preview: str | None = None
    output_preview: str | None = None

    # raw -- nothing we did not promote is lost
    resource_attributes: dict[str, str] = Field(default_factory=dict)
    span_attributes: dict[str, str] = Field(default_factory=dict)

    normalization_version: int = 1
    kafka_partition: int = -1
    kafka_offset: int = -1


class DeadLetterRow(_Row):
    reason: str
    detail: str = ""
    kafka_partition: int = -1
    kafka_offset: int = -1
    raw_body: str = ""


class DecodedSpan(BaseModel):
    """One span flattened out of an OTLP payload, before MCP normalization."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    span_id: str
    parent_span_id: str = ""
    span_name: str = ""
    span_kind: str = ""
    start_unix_nano: int = 0
    duration_ns: int = 0
    status_code: str = "UNSET"
    status_message: str = ""
    scope: str = ""
    resource_attributes: dict[str, Any] = Field(default_factory=dict)
    span_attributes: dict[str, Any] = Field(default_factory=dict)
    event_names: list[str] = Field(default_factory=list)

    @property
    def has_exception_event(self) -> bool:
        return "exception" in self.event_names
