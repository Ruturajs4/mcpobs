"""Response contracts.

These are DELIBERATELY not the ClickHouse row shape. V2 §13.1 requires DTOs
that are stable independent of the storage layout, because the schema will keep
moving -- six migrations in two days -- and every one of those would otherwise
be a breaking API change.

Field names are the product's vocabulary, not the table's.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FailureBreakdown(BaseModel):
    """Counts by failure category, the product's core differentiator (V2 §6.3)."""

    ok: int = 0
    tool_error: int = 0
    server_exception: int = 0
    unknown_tool: int = 0
    invalid_arguments: int = 0
    protocol_error: int = 0
    pending_input: int = 0
    unclassified: int = 0

    @property
    def failures(self) -> int:
        return (
            self.tool_error
            + self.server_exception
            + self.unknown_tool
            + self.invalid_arguments
            + self.protocol_error
            + self.unclassified
        )


class LatencyStats(BaseModel):
    """Latency over LATENCY-ELIGIBLE spans only (D29).

    Stream lifetimes and MRTR interim rounds are excluded upstream, so these
    numbers cannot be poisoned by a `subscriptions/listen` span.
    """

    count: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    #: Spans whose duration was measured as exactly zero. Surfaced rather than
    #: hidden: on a coarse clock this can be most of them, and a percentile
    #: computed over zeros is not a latency (D27, DF-4).
    zero_duration: int = 0


class ToolSummary(BaseModel):
    tool: str
    server: str
    calls: int
    errors: int
    failure_breakdown: FailureBreakdown
    latency: LatencyStats
    last_seen: datetime | None = None


class ServerSummary(BaseModel):
    server: str
    version: str = ""
    environment: str = ""
    calls: int
    errors: int
    tools: int
    failure_breakdown: FailureBreakdown
    latency: LatencyStats
    last_seen: datetime | None = None


class Overview(BaseModel):
    window_minutes: int
    servers: int
    tools: int
    calls: int
    errors: int
    failure_breakdown: FailureBreakdown
    latency: LatencyStats
    #: Share of failures classified by the helper middleware rather than
    #: guessed from the bare span. Below 1.0 means some servers are not running
    #: the helper and their errors are coarser -- two data qualities that must
    #: never silently mix (D21).
    classified_ratio: float = 0.0
    freshness_p95_seconds: float = 0.0


class CapabilityRow(BaseModel):
    """One tool, prompt, resource or protocol method.

    All four are the same shape deliberately: they are the same question asked
    of different `mcp_method` values, and giving them one row type is what stops
    the UI growing four near-identical tables.
    """

    kind: str          # tool | prompt | resource | protocol
    name: str          # tool name, prompt name, resource uri, or method
    method: str
    server: str = ""
    calls: int = 0
    errors: int = 0
    failure_breakdown: FailureBreakdown = Field(default_factory=FailureBreakdown)
    latency: LatencyStats = Field(default_factory=LatencyStats)
    last_seen: datetime | None = None


class SpanDTO(BaseModel):
    span_id: str
    parent_span_id: str = ""
    name: str
    kind: str = ""
    start_time: datetime
    duration_ms: float
    status: str = "UNSET"
    #: "" for non-MCP spans -- a failing downstream GET is not an MCP failure.
    failure_category: str = ""
    mcp_method: str = ""
    tool: str = ""
    #: http | db | llm | messaging | internal -- what explains this span's time.
    downstream_kind: str = ""
    downstream_detail: str = ""
    is_latency_eligible: bool = True
    depth: int = 0
    #: Time this span spent on its OWN work: total minus the sum of its
    #: children. The difference between "this tool is slow" and "this tool is
    #: waiting on something slow", which duration alone cannot distinguish.
    self_ms: float = 0.0
    #: Milliseconds after the trace started. Drives bar placement.
    offset_ms: float = 0.0
    #: Error text, when the helper captured it. Failing spans only.
    failure_detail: str = ""


class SpanDetail(BaseModel):
    """EVERY stored field for one span.

    Nothing is omitted for being uninteresting. The console previously showed 17
    of 55 columns, and the fields that were dropped -- `status_message`,
    `error_type`, the raw attribute maps, the Kafka offsets -- are exactly the
    ones an operator needs when something is wrong. Assertion D1 checks this
    stays complete.
    """

    # identity
    span_id: str
    parent_span_id: str = ""
    trace_id: str
    name: str
    kind: str = ""
    depth: int = 0

    # service
    service_name: str = ""
    service_version: str = ""
    environment: str = ""
    service_instance: str = ""

    # timing
    start_time: datetime
    duration_ms: float = 0.0
    self_ms: float = 0.0
    offset_ms: float = 0.0
    pct_of_trace: float = 0.0

    # status
    status: str = "UNSET"
    status_message: str = ""
    failure_category: str = ""
    failure_detail: str = ""
    failure_kind_source: str = ""
    classifier_version: int = 0
    error_type: str = ""
    rpc_status_code: str | None = None
    is_error: bool = False

    # MCP
    mcp_method: str = ""
    tool: str = ""
    prompt: str = ""
    resource_uri: str = ""
    gen_ai_operation: str = ""
    protocol_version: str = ""
    jsonrpc_request_id: str = ""
    transport: str = ""
    session_id: str | None = None
    result_type: str = ""
    mrtr_state_in: str = ""
    mrtr_state_out: str = ""
    is_latency_eligible: bool = True

    # downstream
    downstream_kind: str = ""
    http_method: str = ""
    http_status_code: int | None = None
    http_host: str = ""
    db_system: str = ""
    db_operation: str = ""
    db_collection: str = ""
    #: The downstream analogue of a request: the URL called, the SQL run.
    #: Redacted at normalize time, not at render (D59).
    http_url: str = ""
    db_statement: str = ""
    #: Downstream HTTP detail. Empty means `instrument_httpx()` was not
    #: called; the UI says which, because blank and "not captured" are
    #: different facts. There is no response body: the client span ends before
    #: httpx reads one (mcpobs/http.py).
    http_request_body: str = ""
    http_request_headers: str = ""
    http_response_headers: str = ""
    #: Self-reported by the client, never verified. Display only.
    client_name: str = ""
    client_version: str = ""
    gen_ai_system: str = ""
    gen_ai_model: str = ""
    gen_ai_input_tokens: int | None = None
    gen_ai_output_tokens: int | None = None

    # payload -- NULL unless payload capture is enabled (DF-8)
    input_size: int | None = None
    output_size: int | None = None
    input_preview: str | None = None
    output_preview: str | None = None

    # raw
    span_attributes: dict[str, str] = Field(default_factory=dict)
    resource_attributes: dict[str, str] = Field(default_factory=dict)

    # provenance -- which message produced this row, and which code wrote it.
    # The difference between debugging the customer's server and debugging ours.
    normalization_version: int = 0
    kafka_partition: int = -1
    kafka_offset: int = -1
    ingested_at: datetime | None = None
    freshness_ms: float = 0.0


class TraceDetail(BaseModel):
    trace_id: str
    server: str = ""
    tool: str = ""
    mcp_method: str = ""
    start_time: datetime
    duration_ms: float
    span_count: int
    error_count: int
    failure_category: str = ""
    #: The span whose parent is absent from the trace. Resolved HERE, at query
    #: time, because it is not computable incrementally -- and the MCP span may
    #: legitimately be a child of an instrumented client (D7, D22).
    root_span_id: str = ""
    spans: list[SpanDTO] = Field(default_factory=list)
    #: Full detail keyed by span_id, so selecting a span in the waterfall needs
    #: no second request. A trace is small; a round trip per click is not worth
    #: saving a few kilobytes.
    detail: dict[str, SpanDetail] = Field(default_factory=dict)


class TraceSummary(BaseModel):
    trace_id: str
    server: str = ""
    tool: str = ""
    mcp_method: str = ""
    start_time: datetime
    duration_ms: float
    span_count: int
    error_count: int
    failure_category: str = ""


class Page(BaseModel):
    """Keyset pagination, never OFFSET (V2 §13.1).

    `next_cursor` is an opaque token: encoding it as a timestamp would invite
    clients to construct their own, and then we could never change the key.
    """

    items: list[TraceSummary] = Field(default_factory=list)
    next_cursor: str | None = None
