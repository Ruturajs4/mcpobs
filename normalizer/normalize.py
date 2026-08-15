"""DecodedSpan -> SpanRow.

Stateless by design (Architecture.md ADR-005): one span in, one row out. No
cross-span state, no windowing, no trace assembly -- that happens in ClickHouse.

Attribute keys come from docs/observed_attributes.md, which outranks any
document where they disagree (Day-1 doc D10).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from normalizer.models import DecodedSpan, SpanRow
from normalizer.taxonomy import FailureTaxonomy


class SpanNormalizer:
    """Extracts MCP fields and derives the failure category."""

    normalization_version: Final[int] = 7

    # span attributes
    MCP_METHOD: Final = "mcp.method.name"
    MCP_TOOL: Final = "gen_ai.tool.name"
    MCP_PROMPT: Final = "gen_ai.prompt.name"
    MCP_OPERATION: Final = "gen_ai.operation.name"
    MCP_RESOURCE_URI: Final = "mcp.resource.uri"
    MCP_PROTOCOL_VERSION: Final = "mcp.protocol.version"
    MCP_SESSION_ID: Final = "mcp.session.id"
    JSONRPC_REQUEST_ID: Final = "jsonrpc.request.id"
    ERROR_TYPE: Final = "error.type"
    RPC_STATUS: Final = "rpc.response.status_code"
    RESULT_TYPE: Final = "mcp.result.type"
    TRANSPORT: Final = "network.transport"
    HELPER_VERSION: Final = "mcpobs.failure.kind.version"
    MRTR_STATE_OUT: Final = "mcpobs.mrtr.state.out"
    MRTR_STATE_IN: Final = "mcpobs.mrtr.state.in"
    FAILURE_DETAIL: Final = "mcpobs.failure.detail"
    REQUEST: Final = "gen_ai.tool.call.arguments"
    RESPONSE: Final = "gen_ai.tool.call.result"
    REQUEST_SIZE: Final = "mcpobs.request.size"
    RESPONSE_SIZE: Final = "mcpobs.response.size"

    # Downstream (U6). Both current and legacy semconv names are read: OTel
    # instrumentation libraries migrate at their own pace, and a customer's
    # pinned version decides which they emit.
    DB_SYSTEM: Final = ("db.system.name", "db.system")
    DB_OPERATION: Final = ("db.operation.name", "db.operation")
    DB_COLLECTION: Final = ("db.collection.name", "db.sql.table", "db.name")
    GEN_AI_SYSTEM: Final = ("gen_ai.system",)
    GEN_AI_MODEL: Final = ("gen_ai.request.model", "gen_ai.response.model")
    GEN_AI_IN_TOKENS: Final = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
    GEN_AI_OUT_TOKENS: Final = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")

    # resource attributes
    RES_SERVICE_NAME: Final = "service.name"
    RES_SERVICE_VERSION: Final = "service.version"
    RES_ENVIRONMENT: Final = "deployment.environment.name"
    RES_TENANT: Final = "tenant.id"
    RES_PROJECT: Final = "project.id"

    def __init__(self, taxonomy: FailureTaxonomy | None = None) -> None:
        self.taxonomy = taxonomy or FailureTaxonomy()

    def to_row(self, span: DecodedSpan, *, partition: int = -1, offset: int = -1) -> SpanRow:
        attrs = span.span_attributes
        res = span.resource_attributes

        is_mcp = self.MCP_METHOD in attrs
        category = self.taxonomy.classify(span)
        http_method, http_status, http_host = self._http(attrs)
        session_id = attrs.get(self.MCP_SESSION_ID)
        rpc_status = attrs.get(self.RPC_STATUS)

        return SpanRow(
            tenant_id=self._str(res.get(self.RES_TENANT)) or "local",
            project_id=self._str(res.get(self.RES_PROJECT)) or "local",
            environment=self._str(res.get(self.RES_ENVIRONMENT)) or "local",
            timestamp=self._timestamp(span.start_unix_nano),
            duration_ns=span.duration_ns,
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            span_name=span.span_name,
            span_kind=span.span_kind,
            status_code=span.status_code,
            status_message=span.status_message,
            service_name=self._str(res.get(self.RES_SERVICE_NAME)),
            service_version=self._str(res.get(self.RES_SERVICE_VERSION)),
            deployment_environment=self._str(res.get(self.RES_ENVIRONMENT)),
            # Method names are free-form strings, never validated against an
            # enum: server/discover, subscriptions/listen and tasks/* are not in
            # the OTel well-known list and must pass through untouched.
            mcp_method=self._str(attrs.get(self.MCP_METHOD)),
            mcp_tool_name=self._str(attrs.get(self.MCP_TOOL)),
            gen_ai_operation=self._str(attrs.get(self.MCP_OPERATION)),
            protocol_version=self._str(attrs.get(self.MCP_PROTOCOL_VERSION)),
            jsonrpc_request_id=self._str(attrs.get(self.JSONRPC_REQUEST_ID)),
            mcp_prompt_name=self._str(attrs.get(self.MCP_PROMPT)),
            mcp_resource_uri=self._str(attrs.get(self.MCP_RESOURCE_URI)),
            transport=self._str(attrs.get(self.TRANSPORT)),
            mcp_session_id=self._str(session_id) if session_id is not None else None,
            mcp_is_error=int(self.taxonomy.is_error(category)),
            result_type=self._str(attrs.get(self.RESULT_TYPE)),
            failure_category=category,
            error_type=self._str(attrs.get(self.ERROR_TYPE)),
            rpc_status_code=self._str(rpc_status) if rpc_status is not None else None,
            http_method=http_method,
            http_status_code=http_status,
            http_host=http_host,
            db_system=self._first(attrs, self.DB_SYSTEM),
            db_operation=self._first(attrs, self.DB_OPERATION),
            db_collection=self._first(attrs, self.DB_COLLECTION),
            gen_ai_system=self._first(attrs, self.GEN_AI_SYSTEM),
            gen_ai_model=self._first(attrs, self.GEN_AI_MODEL),
            gen_ai_input_tokens=self._opt_int(attrs, self.GEN_AI_IN_TOKENS),
            gen_ai_output_tokens=self._opt_int(attrs, self.GEN_AI_OUT_TOKENS),
            downstream_kind=self._downstream_kind(attrs, is_mcp),
            failure_detail=self._str(attrs.get(self.FAILURE_DETAIL)),
            # Populated only when the customer enabled payload capture. Empty
            # string means "not captured", which is different from an empty
            # payload -- so NULL rather than "" (DF-8).
            input_preview=self._str(attrs.get(self.REQUEST)) or None,
            output_preview=self._str(attrs.get(self.RESPONSE)) or None,
            input_size=self._opt_int(attrs, (self.REQUEST_SIZE,)),
            output_size=self._opt_int(attrs, (self.RESPONSE_SIZE,)),
            resource_attributes={k: self._str(v) for k, v in res.items()},
            span_attributes={k: self._str(v) for k, v in attrs.items()},
            normalization_version=self.normalization_version,
            kafka_partition=partition,
            kafka_offset=offset,
            failure_kind_source=self.taxonomy.source(span) if is_mcp else "",
            classifier_version=self._int(attrs.get(self.HELPER_VERSION)),
            is_latency_eligible=int(self.taxonomy.is_latency_eligible(span)),
            mrtr_state_out=self._str(attrs.get(self.MRTR_STATE_OUT)),
            mrtr_state_in=self._str(attrs.get(self.MRTR_STATE_IN)),
        )

    # -- internals ---------------------------------------------------------
    def _downstream_kind(self, attrs: dict[str, Any], is_mcp: bool) -> str:
        """What explains this span's time.

        Derived once here rather than per query, so "unexplained server time" is
        a value you can filter on instead of an absence you have to infer.
        """
        if is_mcp:
            return ""
        if self._first(attrs, self.GEN_AI_SYSTEM):
            return "llm"
        if self._first(attrs, self.DB_SYSTEM):
            return "db"
        if attrs.get("http.request.method") or attrs.get("http.method"):
            return "http"
        if attrs.get("messaging.system"):
            return "messaging"
        return "internal"

    def _first(self, attrs: dict[str, Any], keys: tuple[str, ...]) -> str:
        """First present key, so old and new semconv names both work."""
        for key in keys:
            value = attrs.get(key)
            if value is not None and value != "":
                return self._str(value)
        return ""

    def _opt_int(self, attrs: dict[str, Any], keys: tuple[str, ...]) -> int | None:
        raw = self._first(attrs, keys)
        return self._int(raw) if raw else None

    def _http(self, attrs: dict[str, Any]) -> tuple[str, int | None, str]:
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
            status_int = int(status) if status is not None else None
        except (TypeError, ValueError):
            status_int = None
        return str(method), status_int, str(host)

    @staticmethod
    def _timestamp(unix_nano: int) -> datetime:
        """OTLP nanoseconds -> naive-UTC datetime for ClickHouse DateTime64.

        Integer division, not `unix_nano / 1e9`: float64 carries ~15-16
        significant digits and an epoch-nanosecond value needs 19, so the float
        path rounds the sub-second part before `datetime` ever sees it.

        Python `datetime` caps at MICROSECONDS, so the final three digits of an
        OTLP timestamp cannot be represented at all. The column is DateTime64(9)
        because OTLP is nanosecond-native and we would rather widen the value
        later than migrate the column; but do not read nanosecond precision out
        of it. Sub-microsecond span ordering within a trace is not available.
        """
        seconds, remainder_ns = divmod(unix_nano, 1_000_000_000)
        return datetime.fromtimestamp(seconds, tz=UTC).replace(
            tzinfo=None, microsecond=remainder_ns // 1000
        )

    @staticmethod
    def _str(value: object) -> str:
        return "" if value is None else str(value)

    @staticmethod
    def _int(value: object) -> int:
        """Attribute values arrive as Any; coerce defensively, never raise."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, (str, float)):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0
        return 0
