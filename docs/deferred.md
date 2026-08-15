# Deferral register

Everything knowingly postponed, in one place, with the thing that will force it
back onto the agenda.

> **Why this file exists.** On 15 Aug 2026 three separate deferrals — all phrased
> as "test that in staging" — turned out to be concealing **two real defects** in
> the pipeline's most important correctness property: deduplication was
> half-applied, and materialized views were double-counting spans. Neither would
> have errored. Both were found within an hour of removing the deferral.
>
> A deferral is a decision to accept unknown risk for a while. That is often
> right. What is never right is losing track of which risks you accepted, so
> every entry here names **what would go wrong** and **what forces it back**.

**Statuses:** `OPEN` · `BLOCKED` (waiting on something external) · `WATCH` (fine
today, will bite at scale) · `CLOSED` (kept for the record).

---

## Correctness — carries real risk today

| # | Deferred | Risk if it stays open | Forced back by | Status |
| --- | --- | --- | --- | --- |
| DF-2 | **Multi-broker Kafka (RF=3, `min.insync.replicas=2`).** Local runs one broker at RF=1. | `acks=all` is exercised, but the ack *semantics under broker loss* are not. ADR-008's durability claim is proven for a healthy cluster only. | First staging deployment, or before any customer data. | OPEN |
| DF-3 | **`trace_summaries` has no `PARTITION BY` or TTL.** Every candidate date lives in an aggregate column whose value changes as parts merge, and a trace can straddle midnight — so no deterministic partition key was available. | The table grows without bound and cannot be dropped by partition. Fine at laptop scale; not fine at 4.3 TB/day (Architecture §9.3). | Retention policy work, or the first table that will not fit. | OPEN |
| DF-4 | **Latency percentiles are untrustworthy on this host.** OTel timestamps spans with `time.time_ns()`, whose smallest tick here is 0.749 ms, so ~40% of tool-call spans record `duration_ns = 0`. | Any latency number produced locally is wrong for fast tools. Linux is nanosecond-grade so production is very likely fine — **a customer on Windows is not**. | B8 reports it every run. Closes when measured on Linux, or when we document the customer-facing caveat. | WATCH |

## Product surface — planned, not risky

| # | Deferred | Why it waits | Status |
| --- | --- | --- | --- |
| DF-7 | Rollups (`server_metrics_1m`, `tool_metrics_1m`) | Day 4–5, deliberately **after** Day 3, so they encode query patterns the API actually validated rather than guesses. | OPEN |
| DF-9 | API keys, tenancy, Postgres control plane | Phase 1. Tenant columns exist, hard-coded to `local`. | OPEN |
| DF-10 | Alerting engine, cross-region query, RBAC/SSO | V2 §20.2 — explicitly out of scope for launch. | OPEN |

## External dependencies — cannot close alone

| # | Deferred | Blocked on | Status |
| --- | --- | --- | --- |
| DF-11 | **Upstream SDK change exposing the failure kind** (D18). Our classifier matches SDK-internal message strings; that is a bridge, not an endpoint. | An MCP Python SDK contribution/discussion. `tests/test_sdk_contract.py` fails the build if the strings move, so the risk is contained but not removed. | BLOCKED |
| DF-12 | `db.operation` / `db.collection` are empty. Note `mcp.resource.uri` was the same shape of gap and is now recovered by the helper (D50); the db fields could follow if it matters. | The installed `opentelemetry-instrumentation-dbapi` emits only `db.system` + `db.statement`. The operation is recoverable from the span name until semconv catches up. | BLOCKED |
| DF-13 | Managed Kafka provider choice (MSK / Confluent / Aiven), and whether **tiered storage** is GA on that version. | Cloud commit. Tiered storage is load-bearing for ADR-007's retention economics and was a pillar of the Kafka-over-Redpanda argument (ADR-002). | BLOCKED |
| DF-14 | S3 sink connector licence review (Confluent community vs Aiven Apache 2.0). | Provider choice. We made a licensing argument in ADR-002; applying it inconsistently here would be sloppy. | BLOCKED |
| DF-15 | Collector auth extension for real tenant resolution. | Phase 1 spike. Decides whether ADR-003 ("no bespoke ingest service") fully holds or needs a small custom extension. | BLOCKED |

## Operational — needs real traffic

| # | Deferred | Why it cannot be settled yet | Status |
| --- | --- | --- | --- |
| DF-16 | Whale-tenant salt factor and partition-skew threshold (ADR-004). | Needs a real tenant distribution. Guessing now would encode a fiction. | WATCH |
| DF-17 | Batch size / flush interval tuning. | `BATCH_MAX_SECONDS` is the **floor** on freshness — currently 5 s, and freshness measures ~5 s because of it. The trade against ClickHouse insert efficiency needs production volume. | WATCH |
| DF-18 | Capacity figures in Architecture section 9.3 are **planning estimates, not measurements**. | Replace each with an observed value once real telemetry exists. The cost model depends on them. | WATCH |

---

## Closed

| # | Was deferred | How it closed |
| --- | --- | --- |
| DF-C1 | "Test idempotency in staging" | **Deleted, not moved.** Ran ReplicatedMergeTree + embedded Keeper locally (D40). Found D38 and D39 within the hour. |
| DF-C2 | "Does `insert_deduplication_token` actually work?" | Proven by assertion B11 (D38). Works when batch boundaries match — crash recovery, not operator replay, and that distinction is correct. |
| DF-C3 | "Is the failure taxonomy recoverable?" | Yes (D17). Resolved by U1 without capturing tool content. |
| DF-C4 | "Do we stitch MRTR round-trips?" | Settled by experiment, not argument (D28): 2 spans, no shared trace_id, 135× latency understatement. |
| DF-C5 | "How is `subscriptions/listen` represented?" | Stored column `is_latency_eligible`, not a query-time filter (D29). |
| DF-C6 | "Trace-locator shape for trace-by-id" | Built in U2; reads must dedupe explicitly (D25). |
| DF-C7 | `pending_input` never exercised end to end (was DF-1) | Closed by giving the demo client an elicitation callback. Round 1 is `pending_input` and not latency-eligible; round 2 completes. |
| DF-C10 | Payload capture + redaction (was DF-8) | Built on Day 5 (D55). Off by default, truncated, redacted. The taxonomy still does not depend on it — B2b asserts 140 failures classified precisely with no payload stored. |
| DF-C9 | Trace-detail waterfall UI (was DF-6) | Built on Day 4. Surfaced two bugs no test had: freshness measured 19,820s over a wide window because it swept in replayed spans, and the test probe appeared as a second "server". |
| DF-C8 | Query service and stable DTOs (was DF-5) | Built on Day 3. Found a 500 on the empty-tenant path that no amount of local data would have surfaced (D42). |

---

*Reviewed at the end of each engineering day. An entry that has not been looked
at in two weeks is either genuinely fine or genuinely forgotten — and the
difference matters.*
