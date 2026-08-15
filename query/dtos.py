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
