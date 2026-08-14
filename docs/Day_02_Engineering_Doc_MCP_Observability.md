# MCP Observability Platform — Day 2 Engineering Document

> **Phase 0, Day 2** — make the data *mean* something: resolve the failure-taxonomy gap Day 1 found, assemble traces in ClickHouse, and prove the two architectural claims that are currently unproven — replay and freshness.

| Field | Value |
| --- | --- |
| Document | Day 2 engineering document (execution-level) |
| Version | 1.0 |
| Date | 14 August 2026 |
| Status | Ready for execution |
| Architecture | `Architecture.md` v1.0 — ADR-005 and ADR-007 get exercised for the first time today |
| Predecessor | `Day_01_Engineering_Doc_MCP_Observability.md` v0.2 — **all 13 assertions green** |
| Build repo | `C:\Users\admin\ABC_Projects\mcp-observability` @ `7c7fa37` |
| Team size assumed | 1–2 engineers, one working day (8 hours), time-boxed |

---

## Contents

1. [Where Day 1 left us](#1-where-day-1-left-us)
2. [Day-2 goal and definition of done](#2-day-2-goal-and-definition-of-done)
3. [The taxonomy gap is recoverable — findings](#3-the-taxonomy-gap-is-recoverable--findings)
4. [Work breakdown: U0–U6](#4-work-breakdown-u0u6)
5. [U1 — failure taxonomy v1](#5-u1--failure-taxonomy-v1)
6. [U2 — trace assembly in ClickHouse](#6-u2--trace-assembly-in-clickhouse)
7. [U3 — prove the replay path](#7-u3--prove-the-replay-path)
8. [U4 — freshness, the headline metric](#8-u4--freshness-the-headline-metric)
9. [U5 — long-lived and multi-round-trip spans](#9-u5--long-lived-and-multi-round-trip-spans)
10. [U6 — downstream dimensions beyond HTTP](#10-u6--downstream-dimensions-beyond-http)
11. [Acceptance: B1–B9](#11-acceptance-b1b9)
12. [Risks](#12-risks)
13. [Decisions to record](#13-decisions-to-record)
14. [Deferred to Day 3](#14-deferred-to-day-3)

---

## 1. Where Day 1 left us

**Working and verified.** MCP SDK v2 server → OTel → Collector → Kafka → stateless normalizer → ClickHouse, with 13 acceptance assertions green including the A8 buffer test and the A9 dead-letter path. 62 unit tests, ruff and mypy clean over `normalizer/`.

**What Day 1 proved that matters today:** spans are durable before they are useful (the ack boundary works), the normalizer is genuinely stateless, and offsets commit only after the insert.

**What Day 1 left unproven.** Three architectural claims are still assertions on paper:

| Claim | Where | Status |
| --- | --- | --- |
| Trace assembly belongs in ClickHouse, not a stream processor | ADR-005 | **No trace summary exists yet.** The decision is untested. |
| Retention is the replay window; a normalizer bug is recoverable | ADR-007 | **Never replayed anything.** This is the entire justification for Kafka. |
| Freshness (event time → queryable time) is the headline metric | `Architecture.md` §9.1 | **Not measured.** |

**The open wound.** D13: every MCP tool failure collapses to `error.type="tool_error"`. The product's stated differentiator (V2 §6.3) has one failure bucket, and a V2 §25 launch gate is unmet.

Day 2 closes the wound and converts all three claims into tested behaviour.

---

## 2. Day-2 goal and definition of done

### 2.1 The goal

**A failed MCP tool call is classifiable by *why* it failed, a trace can be retrieved as a single assembled summary rather than a pile of spans, and a normalizer bug is provably recoverable by replay rather than by asking customers to resend.**

### 2.2 Definition of done

- [ ] `failure_category` distinguishes at least `tool_error`, `server_exception`, `protocol_error` and `unknown_tool` on real telemetry — not fixtures.
- [ ] No tool result content is stored to achieve that (§5.3).
- [ ] `trace_summaries` is populated incrementally by a materialized view; one row per trace with span count, duration, worst status, server and tool.
- [ ] `trace_locator` resolves a `trace_id` to its time partition without scanning `spans_raw`.
- [ ] A deliberate normalizer bug is shipped, detected, fixed, and the affected rows corrected **by replaying from Kafka**, with `normalization_version` proving which rows were reprocessed.
- [ ] End-to-end freshness is measured and exposed as a number.
- [ ] `subscriptions/listen` spans are excluded from latency aggregates by construction, not by hope.
- [ ] A decision is recorded on MRTR stitching, backed by an experiment rather than an opinion.
- [ ] `make check` and `make verify` green; new assertions B1–B9 added.

### 2.3 Explicitly not Day 2

| Excluded | Why |
| --- | --- |
| Query API and DTOs | Day 3. Today produces the tables Day 3 reads. |
| Any UI | Day 4. |
| Rollups (`server_metrics_1m`, `tool_metrics_1m`) | Day 4–5, and they should encode query patterns Day 3 validates — not guesses. |
| Payload capture | Deliberately avoided; §5.3 exists precisely so the taxonomy does *not* depend on it. |
| Multi-node ClickHouse / `ReplicatedMergeTree` | Staging. Idempotency remains untestable locally. |

---

## 3. The taxonomy gap is recoverable — findings

Investigated before planning, per the Day-1 rule: assert, don't assume.

### 3.1 The result content distinguishes every case

Running each failure mode and inspecting the `CallToolResult` the client receives:

| Scenario | `isError` | Content text |
| --- | --- | --- |
| `echo_fast` | `False` | `ok` |
| `soft_fail` | `True` | `upstream said no` |
| `explode` | `True` | `Error executing tool explode: deliberate failure: downstream credentials expired` |
| `no_such_tool` | `True` | `Unknown tool: no_such_tool` |
| `echo_fast` (bad arg type) | `True` | `Error executing tool echo_fast: 1 validation error for echo_fastArguments...` |

All four failure modes are distinguishable. The gap is **not** a missing signal — it is a signal that never reaches the span.

### 3.2 The distinguishing prefixes are SDK-generated, not user data

| String | Source |
| --- | --- |
| `Error executing tool {name}: {e}` | `mcp/server/mcpserver/tools/base.py:181` |
| `Unknown tool: {name}` | `mcp/server/mcpserver/tools/tool_manager.py:72,85` |

This matters enormously: the classifying substring is **structural boilerplate the SDK writes**, not content the tool author or end user produced. A classifier can read the prefix and discard the rest.

### 3.3 Where the signal is lost, and where it can be recovered

```
tools/call
  │
  ├─ OpenTelemetryMiddleware        <- span opens here; sees only the final result
  │    ├─ RequestStateBoundary
  │    │    ├─ USER MIDDLEWARE      <- span still OPEN; result already converted
  │    │    │    └─ _handle_call_tool
  │    │    │         try: call_tool(...)
  │    │    │         except MCPError:  raise          <- propagates, sets rpc.response.status_code
  │    │    │         except Exception: return CallToolResult(isError=True)   <- SIGNAL LOST HERE
```

Two consequences:

1. **`MCPError` propagates** (`mcpserver/server.py:422`). So `protocol_error` *is* reachable — for tools that raise `MCPError` and for non-`tools/call` methods. Day 1 concluded it was unreachable because no demo tool raised one. Worth correcting.
2. **User middleware runs inside the OTel middleware but outside the handler.** It sees the converted `CallToolResult` while the OTel span is still open — so it can **annotate the existing span** with a derived category. It cannot recover the original exception object, but it does not need to.

---

## 4. Work breakdown: U0–U6

| # | Task | Box | Acceptance |
| --- | --- | --- | --- |
| **U0** | Add MCP failure scenarios: a tool raising `MCPError`, a timeout, a cancellation. | 45m | Each produces a distinguishable span or a documented reason why not. |
| **U1** | **Failure taxonomy v1** — span-annotating middleware in the helper package + normalizer support. | 120m | B1–B3. Four categories on real telemetry, zero content stored. |
| **U2** | **Trace assembly** — `trace_summaries` MV + `trace_locator`. | 120m | B4–B5. One row per trace; trace-by-id needs no raw scan. |
| **U3** | **Replay proof** — ship a bug, detect it, fix it, reprocess from Kafka. | 75m | B6. Corrected rows carry `normalization_version = 2`. |
| **U4** | **Freshness metric** — event time → queryable time, exposed. | 45m | B7. A number, and an assertion that it is under 60s. |
| **U5** | Long-lived + MRTR spans: exclusion rule, and the stitching experiment. | 60m | B8. `subscriptions/listen` cannot enter latency aggregates. |
| **U6** | Downstream dimensions beyond HTTP (`db.*`, `gen_ai.*`). | 45m | B9. A DB child span promotes to `db_system`. |
| **U7** | **Self-instrumentation of the pipeline** (V2 §19) — see below. | 90m | B10. Consumer lag, insert latency, DLQ rate and freshness emitted as OTel metrics. |

Total **7h30** without U7, **9h** with it. **Cut order:** U6 → U5's stitching experiment (keep the exclusion rule) → U0's cancellation case. **Never cut U1 or U3** — U1 is the product differentiator and U3 is the justification for the entire Kafka tier.

### 4.1 U7 — a gap worth naming

V2 §19 requires that the ingest pipeline itself be instrumented with OpenTelemetry, because queue lag, dropped telemetry and ClickHouse insert failures are product-impacting incidents. **The normalizer currently emits no telemetry at all** — it imports `opentelemetry.proto` to *decode* spans and nothing else. We are building an observability product that cannot observe itself.

The irony is not the point; the operational exposure is. Today the only way to know the pipeline is healthy is to run `make verify` by hand. U4's freshness number is the first half of the fix; U7 is the rest — and it is the natural place to dogfood our own product, since the normalizer is itself a service worth tracing.

U7 pushes the day to ~9h, so it is listed as a decision rather than an assumption. If Day 2 stays at 7h30, U7 becomes the first item of Day 3.

---

## 5. U1 — failure taxonomy v1

### 5.1 The mechanism

A small middleware, shipped in **our optional helper package** (V2 §18.2 already contemplates one), that inspects the outgoing `CallToolResult` and sets one extra attribute on the span the SDK already opened.

```python
class FailureClassifierMiddleware(ServerMiddleware):
    """Annotates the SDK's existing span with a derived failure category.

    Does NOT create a span, does NOT wrap the protocol, and does NOT record
    tool content. Reads SDK-generated boilerplate prefixes only and emits a
    single low-cardinality enum value.
    """

    async def __call__(self, ctx, call_next):
        result = await call_next(ctx)
        if getattr(result, "is_error", False):
            span = get_current_span()          # the SDK's span, still open
            span.set_attribute(
                "mcpobs.failure.kind",
                self.classify(self._first_text(result)),
            )
        return result

    def classify(self, text: str) -> str:
        if text.startswith("Unknown tool:"):
            return "unknown_tool"
        if _VALIDATION.search(text):           # "N validation error for ..."
            return "invalid_arguments"
        if text.startswith("Error executing tool "):
            return "server_exception"
        return "tool_error"                    # the tool's own message
```

### 5.2 Why this is the right shape

| Property | Consequence |
| --- | --- |
| Annotates, never wraps | V2 §18.1's launch principle holds: the SDK still owns the protocol span. No duplicate `tools/call` spans. |
| Runs in the customer's process | Classification happens where the content already exists. Only a derived enum crosses the network. |
| Emits one low-cardinality attribute | Cheap to store, cheap to group by, safe to put on a dashboard axis. |
| Opt-in via the helper package | Customers on vanilla OTel keep working; they get `tool_error` and nothing breaks. |

### 5.3 The privacy property, stated explicitly

**No tool input or output is captured, transmitted, or stored to make this work.** The classifier reads a prefix in the customer's own process and emits an enum. This is what makes the taxonomy a *core* feature rather than one gated behind payload capture and its redaction/retention machinery (V2 §15).

That distinction is worth defending in the product narrative: competitors that classify errors by shipping payloads inherit a privacy review; we do not.

### 5.4 The honest weakness

We are string-matching on SDK-internal message formats. They can change in any release.

Mitigations, all required:

1. **Pin and test.** The classifier's expected prefixes are asserted against the installed SDK in CI. An SDK bump that changes them fails the build rather than silently degrading data.
2. **Version the output.** `mcpobs.failure.kind` carries a classifier version so reclassification is a replay (U3), not a migration.
3. **`unclassified` is the health metric.** A rising count of `isError` results the classifier could not place is the alarm that the SDK moved.
4. **Pursue upstream in parallel.** The durable fix is for the SDK to expose the original failure kind. Open a discussion referencing `tools/base.py:181` — until then, treat this middleware as a bridge, not an endpoint.

### 5.5 Normalizer changes

- Promote `mcpobs.failure.kind` to a column and let it **override** the coarse category when present.
- Correct the Day-1 conclusion: `protocol_error` is reachable via propagated `MCPError` (§3.3).
- Keep `unclassified` counted and alertable.
- Bump `normalization_version` to 2 — which U3 then exercises.

---

## 6. U2 — trace assembly in ClickHouse

This is ADR-005 becoming real: incremental aggregation in the store instead of a stateful stream processor.

### 6.1 Shape

```sql
CREATE TABLE mcpobs.trace_summaries
(
    tenant_id     LowCardinality(String),
    project_id    LowCardinality(String),
    trace_id      String,
    start_time    AggregateFunction(min, DateTime64(9)),
    last_end      AggregateFunction(max, DateTime64(9)),
    span_count    AggregateFunction(count),
    error_spans   AggregateFunction(sum, UInt8),
    first_name    AggregateFunction(argMin, String, DateTime64(9)),
    service_name  AggregateFunction(argMin, LowCardinality(String), DateTime64(9)),
    tool_name     AggregateFunction(argMin, LowCardinality(String), DateTime64(9))
)
ENGINE = AggregatingMergeTree
ORDER BY (tenant_id, project_id, trace_id);
```

fed by a materialized view over `spans_raw` grouping on `(tenant_id, project_id, trace_id)`.

### 6.2 The design decision to make today

**"Root span" is not computable incrementally.** The true root is the span whose parent is absent from the trace — and per D7 the MCP span may legitimately be a *child* of an instrumented client, so `parent_span_id = ''` is not a reliable test either. An incremental aggregate cannot see the whole trace.

Three options, pick one and record it:

| Option | Cost |
| --- | --- |
| `argMin(name, timestamp)` — earliest span as a proxy for root | Cheap, incremental, occasionally wrong when a child starts first under clock skew |
| Resolve the true root at query time over `spans_raw` for trace *detail* only | Correct where it matters; the summary keeps the cheap proxy for lists |
| Second-pass job that finalises summaries after a delay | Correct everywhere, reintroduces the stateful machinery ADR-005 rejected |

**Recommendation: the middle option.** Trace *lists* tolerate a proxy; trace *detail* already reads raw spans, so compute the true root there. This keeps ADR-005 intact and is the cheapest correct thing.

### 6.3 `trace_locator`

`spans_raw` is ordered by `(tenant, project, time, service, tool)` — deliberately, per V2 §12.3 — so a bare `WHERE trace_id = ...` scans. A locator maps `trace_id → (tenant, project, min timestamp)` so trace-by-id becomes a point lookup plus a partition-pruned read. Day 3's API depends on it.

---

## 7. U3 — prove the replay path

The most valuable 75 minutes of the day, because it is the only thing that validates the Kafka tier.

### 7.1 The drill

1. Ship a normalizer with a deliberate, plausible bug — e.g. `mcp_tool_name` populated from the span *name* instead of `gen_ai.tool.name`, which is exactly the mistake D6 warns against.
2. Run traffic. Observe wrong rows in ClickHouse.
3. Fix the normalizer; bump `normalization_version` to 2.
4. **Reset a dedicated consumer group to the earliest offset** and reprocess — without touching the demo server, and without asking any producer to resend.
5. Verify corrected rows carry `normalization_version = 2`, and that querying `argMax(...)` by version yields only corrected data.

### 7.2 What this exercises

Retention as a replay window (ADR-007), the versioned-normalizer design (V2 §12.2), and the independent-consumer-group property (§6.4 invariant 3). If any of those is wrong, today is a far better day to find out than during an incident.

### 7.3 Expected finding

Duplicates. Local ClickHouse is `MergeTree`, so `insert_deduplication_token` is inert and a replay will write second copies. That is *expected and documented* (D-caveat in `001_spans_raw.sql`). The correct Day-2 outcome is a query pattern that selects the highest `normalization_version` per `(trace_id, span_id)`, plus a staging ticket to verify true deduplication on `ReplicatedMergeTree`.

---

## 8. U4 — freshness, the headline metric

`Architecture.md` §9.1 names end-to-end freshness — span event time to queryable time — as the one number that tells you whether the pipeline is healthy. It has never been measured.

`spans_raw` already carries both `timestamp` (event time) and `ingested_at` (write time), so this is a query, not a schema change:

```sql
SELECT
  quantile(0.50)(dateDiff('millisecond', timestamp, ingested_at)) AS p50_ms,
  quantile(0.95)(dateDiff('millisecond', timestamp, ingested_at)) AS p95_ms,
  max(ingested_at) AS newest
FROM mcpobs.spans_raw
WHERE timestamp > now() - INTERVAL 15 MINUTE;
```

Expose it via `make freshness`, assert p95 < 60s in B7, and record the observed baseline. A number nobody looks at is not a metric — wire it into `verify` so it is checked every run.

---

## 9. U5 — long-lived and multi-round-trip spans

### 9.1 `subscriptions/listen` — an exclusion rule, by construction

A `subscriptions/listen` span's duration is a *stream lifetime*, not a latency. One such span in a p95 calculation destroys the chart.

Implement as a normalizer-set boolean column — `is_latency_eligible` — rather than a query-time `WHERE` clause. Query-time filters get forgotten in exactly one place and the bug ships. A column makes the default safe and forces an explicit opt-in to include stream spans.

Methods excluded today: `subscriptions/listen`, and any method whose span is still open when observed. Free-form matching, never an enum (D8).

### 9.2 The MRTR stitching experiment

The Day-1 open question — *do we stitch MRTR round-trips into one logical tool call?* — has been argued but never tested. The stretch tool `ask_user` returning `resultType: "input_required"` was never built.

Build it, then answer empirically:

- Does the client's retry carry the same `traceparent`, making both round-trips one trace?
- Does `requestState` appear anywhere observable?
- Is the total user-perceived latency reconstructible from the spans we receive?

**Only then** decide. The answer determines what "tool latency" means in every chart the product will ever draw, and it is not a decision to make from first principles when a 30-minute experiment can settle it.

---

## 10. U6 — downstream dimensions beyond HTTP

Day 1 promotes `http_*` from child spans. The V2 trace-waterfall promise (§6.1) covers DB and LLM calls too. Add a SQLite or Redis call to a demo tool, instrument it, and confirm `db_system` promotes. `gen_ai.*` follows the same path when a server calls an LLM — worth wiring now, since the column already exists.

---

## 11. Acceptance: B1–B9

Added to `scripts/verify.py` (rename to `verify.py`; the assertions are cumulative, not per-day).

| # | Assertion |
| --- | --- |
| **B1** | `server_exception`, `tool_error`, `unknown_tool` and `invalid_arguments` all present and non-zero on real telemetry. |
| **B2** | Zero rows contain tool result content — `input_preview` and `output_preview` remain NULL. |
| **B3** | Classifier prefixes still match the installed SDK; an SDK bump fails this. |
| **B4** | `trace_summaries` has exactly one row per distinct `trace_id` in `spans_raw`. |
| **B5** | Trace-by-id via `trace_locator` reads fewer rows than a raw scan (`EXPLAIN`/`system.query_log`). |
| **B6** | After the U3 drill, corrected rows carry `normalization_version = 2` and no producer resent anything. |
| **B7** | Freshness p95 < 60s. |
| **B8** | No `subscriptions/listen` span is `is_latency_eligible`. |
| **B9** | A DB child span promotes to `db_system`. |
| — | A1–A9 still green. **Day 2 must not regress Day 1.** |

---

## 12. Risks

| Risk | Signal | Response |
| --- | --- | --- |
| Middleware cannot reach the open span from its context | `set_attribute` no-ops | Verify `get_current_span()` inside middleware first, before building on it. If the span is not current, fall back to returning the category via `_meta` and classifying server-side. |
| Classifier prefixes differ from §3.1 in another SDK build | B3 fails | That is B3 working. Pin, update, and record — do not loosen the match into something that silently mislabels. |
| `AggregatingMergeTree` state functions mis-specified | `trace_summaries` counts wrong | Use `-State`/`-Merge` consistently; test against a known trace before trusting aggregates. |
| Replay produces duplicates | Row count doubles | **Expected locally** (§7.3). Document, use `argMax` by version, ticket the staging verification. |
| U1 expands into a payload-capture project | Talking about redaction | §5.3 is the boundary. If the taxonomy needs content, the design is wrong. |
| Day 2 regresses Day 1 | A1–A9 fail | They are in the same suite for exactly this reason. |

---

## 13. Decisions to record

| # | Decision |
| --- | --- |
| D17 | Failure classification happens in the customer's process via an opt-in middleware that annotates the SDK's span. No content leaves the process. |
| D18 | The classifier is a bridge, not an endpoint; an upstream SDK change is the durable fix and is pursued in parallel. |
| D19 | `protocol_error` **is** reachable via propagated `MCPError` — correcting Day 1's conclusion. |
| D20 | Trace-list summaries use an incremental proxy for the root span; trace *detail* resolves the true root at query time. ADR-005 stands. |
| D21 | Latency eligibility is a stored column, not a query-time filter. |
| D22 | MRTR stitching is decided by experiment, not argument (U5.2). |

---

## 14. Deferred to Day 3

Query service and stable DTOs over today's tables; the trace-detail waterfall payload; rollups once Day 3 shows the real query patterns; and the staging work that Day 1 and Day 2 both keep deferring — `ReplicatedMergeTree`, real deduplication, and multi-broker Kafka.

---

*v1.0 · 14 Aug 2026 · Implements [`Architecture.md`](./Architecture.md) v1.0 · Follows [`Day_01_Engineering_Doc_MCP_Observability.md`](./Day_01_Engineering_Doc_MCP_Observability.md) · Internal*
