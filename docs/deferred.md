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
| DF-3 | **`trace_summaries` has no `PARTITION BY` or TTL.** | **The blocker was half-imagined.** "Every candidate date lives in an aggregate column" is true of `min(timestamp)` and irrelevant: `toDate(timestamp)` of the row being inserted is deterministic. Partitioned and TTL'd to 7 days, matching `spans_raw` so a summary never outlives the spans it links to (D78-D80). Closing it also exposed that the table had been double-counting replayed spans since Day 2 (D77). | - | CLOSED |
| DF-4 | **Latency percentiles are untrustworthy on this host.** | Closed by the second of its own exit criteria -- the customer-facing caveat. It had been reported to *us* by `make verify` every run while the console showed an unqualified number, and the entry itself said why that was not enough: **a customer on Windows is not** unaffected. The tick is now measured per service and the console marks every percentile it cannot support (D81-D83). | - | CLOSED |

## Product surface — planned, not risky

| # | Deferred | Why it waits | Status |
| --- | --- | --- | --- |
| DF-7 | Rollups (`server_metrics_1m`, `tool_metrics_1m`) | Built as ONE table, because the server view is the tool view with a column dropped (D73). Overview and Servers read it; assertion E2 checks the API's numbers against raw. The register's two-table framing would have created a second source of truth (D73-D76). | CLOSED |
| DF-9 | API keys, tenancy, Postgres control plane | **Built.** Postgres control plane (orgs, users, invites, projects, keys), invite-only with no self-service signup, an authenticating ingest gateway that overwrites customer-claimed tenancy, and a query API scoped by key with `?tenant=` removed rather than deprecated. Assertions F1-F6 (D84-D91). | CLOSED |
| DF-10 | Alerting engine, cross-region query, RBAC/SSO | V2 §20.2 — explicitly out of scope for launch. | OPEN |

## External dependencies — cannot close alone

| # | Deferred | Blocked on | Status |
| --- | --- | --- | --- |
| DF-11 | **Upstream SDK change exposing the failure kind** (D18). Our classifier matches SDK-internal message strings; that is a bridge, not an endpoint. | An MCP Python SDK contribution/discussion. `tests/test_sdk_contract.py` fails the build if the strings move, so the risk is contained but not removed. | BLOCKED |
| DF-12 | `db.operation` / `db.collection` are empty. | **Was never blocked.** Filed as waiting on semconv, but the entry itself said the operation was recoverable from data we already had — and both fields are derivable from the `db.statement` we already store, redacted, in every row. "Blocked on upstream" was a description of where the *attribute* comes from, not of whether we could answer the question. Closed by parsing the redacted statement (D72). | CLOSED |
| DF-13 | Managed Kafka provider choice (MSK / Confluent / Aiven), and whether **tiered storage** is GA on that version. | Cloud commit. Tiered storage is load-bearing for ADR-007's retention economics and was a pillar of the Kafka-over-Redpanda argument (ADR-002). | BLOCKED |
| DF-14 | S3 sink connector licence review (Confluent community vs Aiven Apache 2.0). | Provider choice. We made a licensing argument in ADR-002; applying it inconsistently here would be sloppy. | BLOCKED |
| DF-15 | Collector auth extension for real tenant resolution. | **Answered, and the answer was no.** Measured against the running image: contrib 0.115.1's auth extensions all decide *whether* a request is allowed; none writes a resolved tenant onto the Resource. ADR-003 holds for everything after auth. A thin gateway now does the stamping; replacing it with a Go authenticator extension is a future optimisation, not a correctness gap (D84). | CLOSED |

## Operational — needs real traffic

| # | Deferred | Why it cannot be settled yet | Status |
| --- | --- | --- | --- |
| DF-16 | Whale-tenant salt factor and partition-skew threshold (ADR-004). | Needs a real tenant distribution. Guessing now would encode a fiction. | WATCH |
| DF-17 | Batch size / flush interval tuning. | `BATCH_MAX_SECONDS` is the **floor** on freshness — currently 5 s, and freshness measures ~5 s because of it. The trade against ClickHouse insert efficiency needs production volume. | WATCH |
| DF-18 | Capacity figures in Architecture section 9.3 are **planning estimates, not measurements**. | Replace each with an observed value once real telemetry exists. The cost model depends on them. | WATCH |
| DF-19 | **The Kafka message key is null: ADR-004's tenant partitioning has never happened.** | **Confirmed unfixable in the current pipeline, measured against two Collector versions.** `partition_by_resource_attributes`, `partition_traces_by_resource_attributes` and `partition_logs_by_resource_attributes` are all rejected as invalid keys by contrib **0.115.1** AND **0.130.0** -- so upgrading does not solve it. ADR-004 has been amended to say plainly that what runs today is the option it explicitly REJECTED (round-robin, no key), with the evidence inline: no tenant isolation, no per-tenant lag. Blocks DF-16, which cannot be worked before a key exists to salt. | An upstream `kafkaexporter` option, a custom exporter, or the gateway producing to Kafka itself -- which ADR-003 rejects, and that rejection is still right. | OPEN |
| DF-20 | **A subscription is invisible until it ends, and we see nothing that happens on it.** | **Partly closed.** Events delivered on a stream are now individual spans via `ObservedSubscriptionBus`, so throughput and delivery gaps are visible in real time rather than not at all (D124, D127). What remains open is narrower and now filed as DF-23: an IDLE subscription -- open, carrying nothing -- still produces no telemetry until it closes. | - | CLOSED |
| DF-23 | **An IDLE subscription is still invisible until it closes.** | Narrowed from DF-20. Events flowing on a stream are now observable, so a stream that has STOPPED carrying them shows as an absence of recent event spans. But a subscription that has never carried one -- opened and quiet -- produces nothing at all until it ends, so \"is this client still listening?\" is answerable only indirectly. Closing it needs a heartbeat, which is telemetry emitted for nothing happening: a real cost to weigh against a real question, and not one to decide without a design partner who has the question. | A customer running long-lived subscriptions who asks which are still alive. | OPEN |
| DF-24 | **The console polls; it is not pushed to.** | Reduced, not solved: hidden tabs no longer refetch and only monitoring views auto-refresh (D165-D168), which removes most of the waste. What remains is that an idle tenant still gets a ClickHouse aggregate every 15s per visible tab. The design is worked out (D169): signal after the normalizer's INSERT, not off the raw Kafka topic, and fan out through the Redis already running for quotas. The step before SSE is cheaper still -- a `last_advanced:{tenant}` key the browser polls instead of the aggregate, which keeps the Query API stateless. | Concurrent viewers per tenant, or a query cost that shows up in the p95. | OPEN |
| DF-21 | **Progress notifications produce no telemetry at all.** | **Closed.** Each `report_progress()` call is now a child span of the running tool call, carrying value, total, percentage and message. Measured: a 10.3-second call produced 200 progress children, queryable while the parent had not yet landed. Capped at 200 per operation with the cap announcing itself (D124-D126). Assertions H1, H2. | - | CLOSED |
| DF-22 | **OAuth is completely invisible: no auth span has ever been stored.** | **Closed.** The gap was a missing DATA SOURCE, not a missing attribute: authorization is transport-level, so a rejected client never reaches a method. `mcpobs.instrument_asgi(app)` wraps the ASGI app, and 401/403 now arrive as `unauthorized`/`forbidden` -- surfaced, and deliberately not counted as failures, because the spec's own flow opens with a 401 (D119-D123). Assertions G1, G1b, G2 spawn an auth-enabled server and provoke a real 401 rather than asserting over whatever happens to be in the table. | - | CLOSED |

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
| DF-25 | Session tokens for client-launched (stdio) servers | ADR-011 is accepted and unimplemented. Until it ships, a stdio server can only be given a long-lived org-wide ingest key, and for stdio that key lives in a config file on the END USER's machine -- roughly half of MCP deployments. The exposure is not that a key can leak but that nothing expires when it does: quota exhaustion, poisoned error rates, and fabricated servers in the customer's console, indefinitely. Forced back by the first design partner shipping a stdio server to users they do not control. |
