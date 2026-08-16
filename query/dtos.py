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

#: Categories that are NOT failures. The read plane's single answer to "did this
#: fail", and it has to be single: before this constant existed there were
#: THREE definitions in one product. The overview's error rate excluded 401s and
#: cancellations; `?status=error` excluded cancellations but counted 401s; and
#: the /errors list counted both. Measured over 24h of real data, the Errors
#: page listed 62 traces that the headline error rate said were not errors.
#:
#: Why each one is here:
#:   ok             -- succeeded
#:   ''             -- unclassified at write time, not a verdict
#:   pending_input  -- an MRTR interim round, the single most likely way to
#:                     corrupt an error rate (Day-1 3.2, D20)
#:   cancelled      -- the client gave up; not a success, not a server fault
#:   unauthorized / forbidden -- transport-level authorization outcomes. The
#:                     spec's own flow OPENS with an unauthenticated request
#:                     answered by a 401, and 403 insufficient_scope drives the
#:                     routine step-up flow. Counting either as a server failure
#:                     would make every correctly-behaving client look broken.
#:
#: MIRRORS `FailureTaxonomy.NOT_A_FAILURE`. The query image does not ship
#: normalizer/, so this cannot import it; `test_failure_definition_matches_the
#: _taxonomy` fails the build if the two ever diverge.
NOT_A_FAILURE: tuple[str, ...] = (
    "", "ok", "pending_input", "cancelled", "unauthorized", "forbidden",
)


class FailureBreakdown(BaseModel):
    """Counts by failure category, the product's core differentiator (V2 §6.3)."""

    ok: int = 0
    tool_error: int = 0
    server_exception: int = 0
    unknown_tool: int = 0
    invalid_arguments: int = 0
    protocol_error: int = 0
    pending_input: int = 0
    #: The client gave up. Counted separately from `ok` because it is not a
    #: success, and separately from the failures because it is not a server
    #: fault -- `failures` below deliberately excludes it.
    cancelled: int = 0
    #: Transport-level authorization outcomes. Neither is a failure: the
    #: spec's own flow OPENS with an unauthenticated request answered by a
    #: 401, and `403 insufficient_scope` drives the routine step-up flow.
    unauthorized: int = 0
    forbidden: int = 0
    unclassified: int = 0

    @property
    def failures(self) -> int:
        """Everything that is not in NOT_A_FAILURE.

        Derived rather than enumerated: the old version listed the six failing
        categories by hand, so adding a category meant remembering to add it
        here too -- and forgetting meant a real failure quietly missing from the
        error rate, which is the one number nobody re-derives.
        """
        return sum(
            count
            for name, count in self.model_dump().items()
            if name not in NOT_A_FAILURE
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

    #: The smallest non-zero duration observed, i.e. the host clock's actual
    #: tick. MEASURED, not assumed: OTel timestamps spans with `time.time_ns()`,
    #: whose granularity belongs to the customer's host, not to our pipeline.
    #: 0.0 means no sample yet.
    clock_tick_ms: float = 0.0

    #: Set when the clock is too coarse to support the numbers above. Empty
    #: means the percentiles can be trusted. This is a STRING rather than a
    #: boolean because the console shows it to the operator verbatim -- a flag
    #: would have to be translated somewhere, and that somewhere would be the
    #: browser, where the reason gets lost (DF-4).
    clock_warning: str = ""


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
    #: Messaging: the queue or topic a publish/consume touched.
    messaging_system: str = ""
    messaging_destination: str = ""
    messaging_operation: str = ""
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
    #: "stdio" | "streamable-http". Shown as a tag in the list, because which
    #: transport a call arrived on changes what you check next -- a stdio server
    #: is spawned per client, an HTTP one is long-lived and shared.
    transport: str = ""
    tool: str = ""
    mcp_method: str = ""
    start_time: datetime
    duration_ms: float
    span_count: int
    error_count: int
    failure_category: str = ""


class CapabilityPage(BaseModel):
    """Capability rows, plus whether the list was cut short.

    An envelope rather than a bare list because truncation has to be VISIBLE.
    Capabilities are aggregated by name, so there is no time cursor to page
    through -- the natural bound is "the top N by whatever you sorted on", and
    the console says so when it applies. A truncated table that looks complete
    is how someone concludes a tool is not being called.
    """

    items: list[CapabilityRow] = Field(default_factory=list)
    #: True when more rows matched than `cap`. The extra row that proves it is
    #: fetched and discarded server-side.
    truncated: bool = False
    cap: int = 0


class Page(BaseModel):
    """Keyset pagination, never OFFSET (V2 §13.1).

    `next_cursor` is an opaque token: encoding it as a timestamp would invite
    clients to construct their own, and then we could never change the key.
    """

    items: list[TraceSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class FilterOptions(BaseModel):
    """The values a filter dropdown can offer, from the data actually present.

    Fetched rather than hardcoded, so a dropdown never offers a server that
    stopped reporting three weeks ago, and never omits one that appeared an
    hour ago. Scoped to the same tenant and window as the list it filters.
    """

    servers: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class TenantRow(BaseModel):
    """One tenant, joining ClickHouse volume to Postgres identity.

    Two databases, joined in Python rather than pretending one can see the
    other. A tenant present on only one side is a finding, not a gap to hide:
    an org with no telemetry has not onboarded, and telemetry with no org
    should be impossible now that the gateway authenticates.
    """

    tenant: str
    name: str = ""
    plan: str = ""
    projects: int = 0
    users: int = 0
    active_keys: int = 0
    open_invites: int = 0

    spans: int = 0
    errors: int = 0
    servers: int = 0
    last_seen: datetime | None = None

    #: Spans that arrived while the tenant was over its SOFT threshold.
    soft_quota_spans: int = 0
    #: 0 means unlimited, matching `control/quota.py`.
    limit_minute: int = 0
    limit_day: int = 0
    #: True when an operator has overridden the plan for this org.
    limit_overridden: bool = False

    #: Has any telemetry ever arrived? Distinguishes "quiet" from "never set up".
    onboarded: bool = False
    #: Telemetry under a tenant with no org row -- should be impossible.
    orphaned: bool = False


class PipelineHealth(BaseModel):
    freshness_p50_seconds: float = 0.0
    freshness_p95_seconds: float = 0.0
    spans_recent: int = 0
    dead_letters_24h: int = 0
    dead_letter_reasons: dict[str, int] = Field(default_factory=dict)
    #: Several live versions means a deploy is rolling or a replay is in
    #: flight. Neither is wrong; both are worth knowing before trusting an
    #: aggregate, because argMax resolution is what hides the difference (D24).
    normalization_versions: dict[str, int] = Field(default_factory=dict)


class AdminOverview(BaseModel):
    window_minutes: int
    tenants: list[TenantRow] = Field(default_factory=list)
    pipeline: PipelineHealth = Field(default_factory=PipelineHealth)
    total_spans: int = 0
    total_errors: int = 0
    orphaned: int = 0
    never_onboarded: int = 0
