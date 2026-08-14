# MCP Observability Platform — Day 1 Engineering Document

> **Phase 0, Day 1** — build the local vertical slice: Python MCP SDK v2 server → OpenTelemetry SDK → OTLP → Collector → **Kafka** → stateless normalizer → ClickHouse → one queryable MCP trace.

| Field | Value |
| --- | --- |
| Document | Day 1 engineering document (execution-level) |
| Version | 0.2 — revised to put Kafka in the path from Day 1, per `Architecture.md` ADR-002 and ADR-003 |
| Date | 14 August 2026 |
| Status | Ready for execution |
| Architecture | `Architecture.md` v1.0 — this day plan implements the local rung of its environment ladder (§10) |
| Parent document | `MCP_Observability_Product_and_System_Design_Python_V2.docx` (v0.2) |
| Phase | Phase 0 — technical proof (5–7 working days). This document covers Day 1 only. |
| MCP protocol revision | `2026-07-28` (current revision targeted by this build) |
| Launch SDK | Python MCP SDK v2 |
| Telemetry contract | OpenTelemetry traces over OTLP |
| Primary store | ClickHouse |
| Team size assumed | 1–2 engineers, one working day (8 hours), time-boxed |

> **Scope note.** This is not a re-statement of the product specification. The parent V2 document defines *what the product is*; this document defines *what gets typed, run and verified today*, in what order, and what proves it worked.

---

## Contents

1. [Day-1 goal and definition of done](#1-day-1-goal-and-definition-of-done)
2. [Where Day 1 sits in Phase 0](#2-where-day-1-sits-in-phase-0)
3. [Protocol grounding: MCP revision 2026-07-28](#3-protocol-grounding-mcp-revision-2026-07-28)
4. [Telemetry contract we code against today](#4-telemetry-contract-we-code-against-today)
5. [Day-1 architecture (local slice)](#5-day-1-architecture-local-slice)
6. [Repository layout to create](#6-repository-layout-to-create)
7. [Environment prerequisites](#7-environment-prerequisites)
8. [Work breakdown: T0–T8](#8-work-breakdown-t0t8)
9. [Component specifications](#9-component-specifications)
10. [Verification and acceptance queries](#10-verification-and-acceptance-queries)
11. [Day-1 exit checklist](#11-day-1-exit-checklist)
12. [Risks that will probably bite today](#12-risks-that-will-probably-bite-today)
13. [Decisions taken on Day 1](#13-decisions-taken-on-day-1)
14. [Open questions carried into Day 2](#14-open-questions-carried-into-day-2)
15. [Day 2 preview](#15-day-2-preview)
16. [References](#16-references)

---

## 1. Day-1 goal and definition of done

### 1.1 The single goal

**By end of day, a real MCP tool call executed against a locally running Python MCP SDK v2 server must be retrievable from ClickHouse as a normalized, MCP-aware trace — including the tool name, the duration, whether it failed, and at least one downstream child span — having travelled the same shape of path it will travel in production: Collector → Kafka → stateless normalizer → ClickHouse.**

Everything in this document exists to serve that sentence. Any task that does not move that sentence forward is Day 2 work or later.

### 1.2 Definition of done for Day 1

- [ ] A Python MCP SDK v2 demo server runs locally and exposes at least three tools: one fast and successful, one that calls an external HTTP endpoint, and one that fails.
- [ ] The OpenTelemetry SDK is configured in that server (TracerProvider + BatchSpanProcessor + OTLP exporter) and MCP protocol spans are exported — not spans we hand-wrote around the SDK.
- [ ] An OpenTelemetry Collector receives OTLP over both `:4317` (gRPC) and `:4318` (HTTP) and produces to Kafka with `acks=all`, `otlp_proto` encoding, keyed by tenant.
- [ ] Kafka (single-node KRaft container) holds topic `otlp.spans.raw`; messages are observable with `kafka-console-consumer` *before* any consumer of ours exists.
- [ ] A **stateless** normalizer consumes `otlp.spans.raw`, decodes OTLP, extracts the MCP fields listed in [§4.2](#42-attributes-the-normalizer-extracts-today), and inserts rows into ClickHouse — committing offsets only after the insert returns.
- [ ] Stopping the normalizer, calling three tools, then restarting it results in zero lost spans. This is the single test that proves the buffer is real.
- [ ] ClickHouse holds a `spans_raw` table with promoted MCP columns plus raw attribute maps.
- [ ] Three verification SQL queries ([§10](#10-verification-and-acceptance-queries)) return correct results for the three demo tools.
- [ ] An observed-attribute report is produced ([§8, T3](#8-work-breakdown-t0t8)): the exact span names and attribute keys the SDK actually emitted, captured to a file and committed.
- [ ] `make up` brings the whole slice up from a clean checkout, and `README.md` documents it in under ten lines.
- [ ] Everything is committed to a repository with the layout in [§6](#6-repository-layout-to-create).

### 1.3 Explicitly not Day 1

The following are correct product requirements from the V2 specification and are deliberately excluded today. Building any of them today puts the exit criterion at risk.

| Excluded today | When | Why not today |
| --- | --- | --- |
| Multi-broker Kafka, RF=3, `min.insync.replicas` | Staging | One KRaft node with RF=1 exercises the same API and the same consumer semantics. Replication is an operational property, not a code property. |
| Tiered storage, managed Kafka provider selection | Phase 2 | A provider decision (`Architecture.md` §12), not a Day-1 one. |
| `ReplicatedMergeTree` and insert deduplication | Staging | `insert_deduplication_token` requires replication, so idempotency genuinely cannot be tested locally. See [§9.3](#93-clickhouse-day-1-schema). |
| Archiver and billing consumer groups | Phase 1–2 | The fan-out is proven by the topic existing; adding consumers is configuration, not architecture. |
| API keys, tenancy, Postgres control plane | Phase 1 | Day 1 is single-tenant localhost. Tenant columns exist in the schema and the Kafka message key, but are hard-coded to `local`. |
| Any UI | Day 3–4 | Today's user interface is `clickhouse-client`. A waterfall built before the data model is understood is rework. |
| Rollups and materialized views | Day 4–5 | Rollups encode query patterns we have not validated yet. |
| Payload capture and redaction | Phase 1 | Payload columns are created but left NULL; capture policy is a product decision, not a plumbing one. |
| gRPC exporter tuning, TLS, auth extensions | Phase 2 | Localhost, insecure, single process. |
| Any non-Python instrumentation | Post-launch | V2 §18.3 defers this explicitly. |

---

## 2. Where Day 1 sits in Phase 0

Phase 0 in the V2 specification is a 5–7 day technical proof whose exit criterion is: *"a developer can reproduce a failed MCP tool call and understand where it failed from one screen."* Day 1 owns the data path; the screen comes later in the phase.

| Day | Outcome | Depends on |
| --- | --- | --- |
| **Day 1** (this document) | End-to-end data path: MCP server → OTel → Collector → normalizer → ClickHouse. One queryable trace. | — |
| Day 2 | Normalizer hardened: MCP failure taxonomy v0, trace assembly, downstream span extraction, dead-letter path. | Day 1 attribute report |
| Day 3 | Query service: trace list and trace-by-id endpoints over the Day 1 schema. | Day 2 schema stability |
| Day 4 | Trace detail waterfall UI: MCP root span with HTTP/DB children. | Day 3 API |
| Day 5 | Failure walkthrough: `isError` vs thrown exception vs timeout distinguishable on one screen. | Days 2–4 |
| Days 6–7 | Buffer: schema rework from what we learned, demo recording, Phase 1 planning. | — |

> **Sequencing rationale.** The data model is the expensive thing to get wrong — it propagates into rollups, DTOs and UI. Day 1 therefore front-loads the empirical question (*what does the SDK actually emit?*) before any code depends on the answer.

---

## 3. Protocol grounding: MCP revision 2026-07-28

The parent specification was written against MCP `2026-07-28`. That revision is a significant break from earlier revisions, and several of its changes bear directly on an observability data model. These are the facts we code against today, taken from the specification and its changelog.

### 3.1 Facts that matter to us

| Protocol fact (2026-07-28) | Source |
| --- | --- |
| MCP is **stateless**. The `initialize` / `notifications/initialized` handshake is removed. Every request carries its own protocol version and client capabilities. | Changelog, major change 2 |
| Protocol-level **sessions and the `Mcp-Session-Id` header are removed** from Streamable HTTP. Cross-call state uses explicit, server-minted handles passed as ordinary tool arguments. | Changelog, major change 1 |
| Per-request metadata now lives in `_meta`: `io.modelcontextprotocol/protocolVersion` (required), `io.modelcontextprotocol/clientCapabilities` (required), `io.modelcontextprotocol/clientInfo` (SHOULD), `io.modelcontextprotocol/logLevel` (optional). | Base protocol, per-request protocol fields |
| Servers SHOULD identify themselves in every result's `_meta` via `io.modelcontextprotocol/serverInfo`. | Base protocol, per-response protocol fields |
| `traceparent`, `tracestate` and `baggage` are **reserved `_meta` keys** for W3C trace-context propagation — an explicit exception to the prefix rule, kept for compatibility with the OTel MCP semantic conventions. | Base protocol, `_meta` |
| All results carry a required `resultType`: `"complete"` or `"input_required"`. Absent `resultType` from an older server MUST be read as `"complete"`. | Changelog, major change 8 |
| **Multi Round-Trip Requests (MRTR)** replace server-initiated requests. A server returns `resultType: "input_required"` with `inputRequests`; the client retries the original request carrying `inputResponses`. | Changelog, major change 7 |
| `server/discover` is a new RPC that servers MUST implement, advertising supported protocol versions, capabilities and identity. | Changelog, major change 3 |
| `subscriptions/listen` replaces the HTTP GET endpoint and `resources/subscribe`/`unsubscribe`; notifications are tagged with `io.modelcontextprotocol/subscriptionId`. | Changelog, major change 4 |
| `ping`, `logging/setLevel` and `notifications/roots/list_changed` are **removed**. Log level is per-request via `_meta`. | Changelog, major change 5 |
| Streamable HTTP POSTs require `Mcp-Method` and `Mcp-Name` headers. | Changelog, minor change 4 |
| SSE resumability is removed: `Last-Event-ID` and SSE event IDs are gone. A broken stream loses the in-flight request and the client MUST re-issue it with a new request ID. | Changelog, major change 9 |
| List results carry `ttlMs` and `cacheScope` (`"public"` / `"private"`) via `CacheableResult`. | Changelog, minor change 5 |
| New error codes: `-32020` HeaderMismatch, `-32021` MissingRequiredClientCapability, `-32022` UnsupportedProtocolVersion. Resource-not-found moved from `-32002` to `-32602`. `-32020`–`-32099` is reserved for the spec. | Base protocol, error codes |
| Roots, Sampling and Logging are **deprecated**; the suggested migration for Logging is stderr or OpenTelemetry. | Changelog, deprecated 1 |
| Tasks moved out of core into the `io.modelcontextprotocol/tasks` extension, with polling via `tasks/get` and `tasks/update`. | Changelog, major change 6 |

### 3.2 What each fact forces in our design

This table is the reason Day 1 exists in this form. Several assumptions that would have been natural six months ago are now wrong.

| Protocol fact | Engineering consequence for Day 1 |
| --- | --- |
| Sessions removed | `mcp.session.id` will be absent or meaningless for 2026-07-28 servers. Do not make it a correlation key, a primary-key component, or a required column. Keep it nullable, populate it only if present, and never build a UI concept named "session" on it. |
| Statelessness; every request self-describing | Good news for us: each `tools/call` span is self-contained. Protocol version and client identity are per-request, so they belong on the span, not on a connection-level record we would otherwise have to join. |
| `_meta` carries `traceparent` | Distributed context propagation is spec-blessed. When a client also instruments, the server span's parent will be the client span. Our normalizer must **never assume the MCP span is the trace root** — use "root-most span we received", and record `parent_span_id` faithfully. |
| `resultType` and MRTR | A single logical tool call can now legitimately span multiple requests: `input_required` → client retry with `inputResponses`. Latency measured on one span is not user-perceived latency. Day 1 records the field; the product decision (do we stitch MRTR round-trips into one logical call?) is logged as an open question in [§14](#14-open-questions-carried-into-day-2). |
| `input_required` is a success, not a failure | Failure taxonomy must not classify an `input_required` result as an error. It is an interim result. This is the single most likely misclassification in a v0 taxonomy. |
| `server/discover`, `subscriptions/listen`, `tasks/*` | New method names that are **not** in the OTel `mcp.method.name` well-known value list. The normalizer must accept arbitrary method values, not an enum. `subscriptions/listen` is a long-lived request — its span duration is a stream lifetime, not a latency measurement, and must be excluded from latency percentiles. |
| `ping` and `logging/setLevel` removed | The OTel well-known method list still contains removed methods. Our normalizer must handle both older and current servers; treat the method list as data, not as validation. |
| `Mcp-Method` / `Mcp-Name` headers required | Cheap, high-value enrichment at the Collector/edge: MCP method and target are visible in HTTP metadata without body inspection. Note it today; use it in Phase 1 for edge-level routing and quota accounting. |
| SSE resumability removed | A broken stream produces a client retry with a new request ID. Expect duplicate-looking tool calls with different trace/span IDs and no protocol-level link. Do not report them as retries unless we can prove the relationship. |
| New error codes `-32020`–`-32022` | These are transport/protocol-level failures, distinct from a tool that ran and returned `isError`. The taxonomy needs both branches from the start. |
| Logging deprecated in favour of OpenTelemetry | Directly favourable to our positioning: the protocol now points server authors at OTel. Worth capturing for the launch narrative. |

### 3.3 Day-1 rule: assert, do not assume

We do not yet have verified, first-hand knowledge of the exact span names and attribute keys emitted by the installed Python MCP SDK v2 build. The parent document's code sample and the tables in [§4](#4-telemetry-contract-we-code-against-today) are our best *expectation*, not established fact. Task **T3** exists solely to replace expectation with an observed record, and every later task consumes T3's output rather than this document's guesses.

> **Rule.** No attribute key is hard-coded into the normalizer until it appears in the T3 observed-attribute report. Where the report contradicts this document, **the report wins** and this document is amended.

---

## 4. Telemetry contract we code against today

The OpenTelemetry MCP semantic conventions now live in the GenAI semantic-conventions repository. The following is what we expect and what we will assert against in T3.

### 4.1 Span naming and kind

> **AMENDED 14 Aug 2026 after execution.** The tables in §4 were written from the OTel
> semantic conventions before the SDK had been read. They are now corrected against
> `docs/observed_attributes.md` in the build repo, which is generated from spans the SDK
> actually emitted (D10). Corrections are marked **OBSERVED**.

| Aspect | Convention |
| --- | --- |
| Span kinds | **OBSERVED:** plain OTel `SpanKind.SERVER`. The semconv `mcp.client`/`mcp.server` kinds are *not* what `mcp` 2.0.0 emits — it calls `otel_span(kind=SpanKind.SERVER)` directly. Do not filter on a custom kind. |
| Primary name form | `{mcp.method.name} {target}` where target is `{gen_ai.tool.name}` or `{gen_ai.prompt.name}` — e.g. `tools/call get_weather`. |
| Fallback name form | `{mcp.method.name}` alone when no low-cardinality target exists — e.g. `tools/list`. |
| Resource URIs in names | Opt-in only; discouraged by default for cardinality reasons. Our UI must never rely on parsing the span name. |

> **Design consequence.** Because the target is embedded in the span name only by convention, the normalizer must read `gen_ai.tool.name` as the authoritative tool identity and treat the span name as a display string. Parsing names is a Day-1 trap.

### 4.2 Attributes the normalizer extracts today

**OBSERVED — `mcp` 2.0.0 emits exactly six attributes on MCP spans.** Everything else in
the semantic conventions is absent today.

| Attribute key | When | Type | Column | Note |
| --- | --- | --- | --- | --- |
| `mcp.method.name` | always | `str` | `mcp_method` | Free-form, never an enum. `server/discover` was observed — it is not in the OTel well-known list. |
| `mcp.protocol.version` | always | `str` | `protocol_version` | Observed `2026-07-28`. A6 asserts it. |
| `jsonrpc.request.id` | always | **`str`** | `jsonrpc_request_id` | String, not int. |
| `gen_ai.tool.name` | `tools/call` | `str` | `mcp_tool_name` | Authoritative tool identity. |
| `gen_ai.operation.name` | `tools/call` | `str` | `gen_ai_operation` | Observed `execute_tool`. |
| `error.type` | on failure | `str` | `error_type` | **Observed value is always `tool_error`** — see [§9.5](#95-failure-taxonomy-v0). |

**Expected by the original draft but NOT emitted.** These columns exist and stay NULL/empty:
`gen_ai.prompt.name`, `mcp.resource.uri`, `rpc.response.status_code`, `mcp.session.id`,
`network.transport`, `network.protocol.*`, `jsonrpc.protocol.version`,
`client.address`/`client.port`, `server.address`/`server.port`,
`gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`.

Two consequences worth stating plainly:

- **`mcp.session.id` really is gone**, which confirms [D4](#13-decisions-taken-on-day-1) rather than contradicting it.
- **Transport is not observable.** `network.transport` is not emitted, and stdio and
  streamable HTTP produce *identical* attribute sets (verified over both — D12). The
  normalizer cannot tell them apart on Day 1.

### 4.3 Resource attributes (not span attributes)

Per V2 §8.3, service identity lives on the OTel Resource and must be read from there, not from span attributes.

| Resource attribute | Our column | Set by |
| --- | --- | --- |
| `service.name` | `service_name` | Demo server bootstrap (`mcp-demo-server`) |
| `service.version` | `service_version` | Demo server bootstrap (`0.1.0`) |
| `deployment.environment.name` | `deployment_environment` | Demo server bootstrap (`local`) |
| `telemetry.sdk.*` | kept in `resource_attributes` map | OTel SDK automatically |

### 4.4 Things we must not depend on today

- **Client identity.** `io.modelcontextprotocol/clientInfo` is self-reported, unverified, and explicitly not to be used for behavioural or security decisions. Treat client columns as optional display metadata, exactly as V2 §24 requires.
- **Session continuity.** Removed from the protocol. See [§3.2](#32-what-each-fact-forces-in-our-design).
- **The MCP span being the trace root.** An instrumented client makes it a child.
- **Span name structure.** Read attributes, not names.
- **A closed set of method names.** Extensions add methods; the well-known list lags the protocol.

---

## 5. Day-1 architecture (local slice)

Everything runs on one laptop, in Docker, with no auth and no durability guarantees. This is deliberate.

```text
LOCAL MACHINE                          production analogue (Architecture.md §3)
                                       ---------------------------------------
  demo_server/  (Python MCP SDK v2)    customer MCP server
    |  MCP protocol spans from the SDK's built-in OTel tracing
    |  + auto-instrumented httpx child spans
    |  OTel SDK: TracerProvider -> BatchSpanProcessor -> OTLP/HTTP exporter
    |
    |  OTLP  http://localhost:4318/v1/traces      (gRPC :4317 also open)
    v
  otel-collector  (contrib image)      Collector gateway fleet
    receivers:  otlp [grpc, http]
    processors: memory_limiter
                attributes  (stamp tenant=local, project=local)
                batch
    exporters:  kafka   -> otlp.spans.raw   acks=all, otlp_proto
                debug                        (T3 eyeball path only)
    |
    v
  kafka  (single node, KRaft, RF=1)    managed Kafka, RF=3, multi-AZ
    topic: otlp.spans.raw              key = tenant_id
    |                                  ===== ACK BOUNDARY =====
    |
    v
  normalizer/  (STATELESS consumer)    normalizer consumer group
    consume otlp.spans.raw
      -> decode OTLP protobuf
      -> flatten ResourceSpans / ScopeSpans / Span
      -> extract MCP fields per section 4.2
      -> derive failure_category v0
      -> batch INSERT into ClickHouse
      -> COMMIT OFFSETS (only after the insert returns)
    |
    v
  clickhouse  :8123 / :9000            ClickHouse cluster, ReplicatedMergeTree
    db: mcpobs
    table: spans_raw          (section 9.3)
    table: ingest_dead_letter (section 9.3)
    |                                  + MVs: trace_summaries, rollups (Day 4+)
    v
  verification: clickhouse-client + scripts/verify_day1.py  (section 10)

  NOT TODAY: edge/auth, quotas, archiver + billing consumer groups,
             Postgres control plane, Redis, object storage, rollups, UI
```

*Figure 1 — Day-1 local vertical slice, with its production analogue on the right.*

Every box exists in production; what changes going up the environment ladder is node count and managed-versus-container, never the shape. That is the point of putting Kafka in on Day 1: the pieces that survive unchanged into production are the **Collector configuration**, the **normalizer's field-extraction logic** and now the **consume-insert-commit ordering**, which is the part most likely to be subtly wrong if it is bolted on later.

> **Why Kafka on Day 1 rather than Phase 2?** Because the ack boundary and offset-commit ordering are *semantics*, not infrastructure. A normalizer written first as an HTTP handler and later retrofitted as a Kafka consumer gets rewritten, and the retrofit is exactly where at-least-once bugs are introduced. One extra container today avoids that. See `Architecture.md` ADR-003 and ADR-006.

> **Why no bespoke ingest service?** The earlier draft of this document had a FastAPI `ingest_dev` receiving OTLP and writing onward. It is gone. The Collector's `kafkaexporter` does that job with batching, retry and queue behaviour we would otherwise reimplement badly, in the most availability-critical tier of the system. `Architecture.md` ADR-003.

---

## 6. Repository layout to create

```text
mcp-observability/
  README.md                     ten-line quickstart, nothing more
  Makefile                      up | down | demo | verify | attrs | lag | logs
  docker-compose.yml            collector + kafka + normalizer + clickhouse
  .env.example                  ports and endpoints only, no secrets

  demo_server/
    pyproject.toml
    server.py                   MCP SDK v2 server, three tools
    otel_bootstrap.py           provider / processor / exporter / resource
    scenarios.py                success, downstream-http, failure

  normalizer/                   STATELESS Kafka consumer -> ClickHouse
    pyproject.toml
    consumer.py                 poll loop, offset commit AFTER insert
    otlp_decode.py              protobuf -> flat span dicts
    normalize.py                MCP field extraction (section 4.2)
    taxonomy.py                 failure_category v0 (section 9.5)
    clickhouse_sink.py          batched INSERT + dead-letter
    schema/
      001_spans_raw.sql
      002_dead_letter.sql

  collector/
    config.yaml                 otlp in -> kafka out

  kafka/
    create_topics.sh            otlp.spans.raw, otlp.spans.dlq

  scripts/
    verify_day1.py              runs the section 10 assertions
    dump_observed_attrs.py      writes docs/observed_attributes.md

  docs/
    day1.md                     this document
    architecture.md             link to Architecture.md (system of record)
    observed_attributes.md      GENERATED by T3 -- the source of truth
    decisions.md                the section 13 log, kept live
```

*Figure 2 — Repository layout created in T0.*

> **Naming discipline.** The service is called `normalizer`, not `ingest_dev`, because unlike the deleted FastAPI shim it *is* the production component — the same consume-decode-extract-insert loop, just at one replica against one broker. Its batching and error handling should therefore be written as though they will be operated, because they will be.

---

## 7. Environment prerequisites

| Component | Version / image | Note |
| --- | --- | --- |
| Python | 3.11+ | Match the version the demo server and `normalizer` both use. |
| Docker Desktop / Engine | Current | Compose v2 syntax. |
| MCP Python SDK | v2, current stable line | Install as `mcp[cli]`. Record the exact resolved version in `docs/observed_attributes.md`. |
| OpenTelemetry Python | `opentelemetry-sdk`, `opentelemetry-exporter-otlp` | Pin exact versions in `pyproject.toml` today; drift changes attribute output. |
| OTel auto-instrumentation | `opentelemetry-instrumentation-httpx` | Produces the downstream child span the exit criterion requires. |
| OTel Collector | `otel/opentelemetry-collector-contrib` (pinned tag) | Contrib is required for `kafkaexporter`. |
| Kafka | `apache/kafka` (pinned tag), KRaft mode | Single node, RF=1, no ZooKeeper. One container. |
| Kafka client (Python) | `confluent-kafka` | librdkafka-backed; the same client we would run in production. |
| ClickHouse | `clickhouse/clickhouse-server` (pinned tag) | Single node, no cluster, no replication — see the [§9.3](#93-clickhouse-day-1-schema) caveat. |
| `opentelemetry-proto` | matching the Collector's OTLP version | Used by `otlp_decode.py` to parse the protobuf payload. |

> **Pin everything today.** Unpinned images and packages make the T3 attribute report unreproducible, which defeats its purpose. Every version resolved today gets written into `docs/observed_attributes.md` alongside the attributes it produced.

---

## 8. Work breakdown: T0–T8

Time-boxes assume one engineer; with two, T1 and T2 run in parallel after T0. The ordering is deliberate: **nothing is written against assumed attribute names before T3 has observed them.**

| # | Task | Box | Output | Acceptance |
| --- | --- | --- | --- | --- |
| **T0** | Repository, Makefile, docker-compose skeleton, pinned versions. | 45m | Repo per §6; `make up` starts Collector + Kafka + ClickHouse. | `docker compose ps` shows all healthy; ClickHouse answers `SELECT 1`; `otlp.spans.raw` exists. |
| **T1** | Demo MCP server on Python MCP SDK v2 with three tools. | 60m | `demo_server/server.py`, `scenarios.py`. | All three tools callable; failing tool fails in the intended way, not by accident. |
| **T2** | OTel bootstrap in the demo server: resource, provider, batch processor, OTLP exporter, httpx instrumentation. | 45m | `demo_server/otel_bootstrap.py`. | Collector `debug` exporter prints spans when a tool is called. |
| **T2b** | Collector `kafkaexporter` + attributes processor; topic creation script. | 30m | `collector/config.yaml`, `kafka/create_topics.sh`. | `kafka-console-consumer` on `otlp.spans.raw` shows messages arriving when a tool is called — **before any consumer of ours exists**. |
| **T3** | **Observed-attribute report.** Capture raw Collector output for all three tools and record every span name and attribute key emitted. | 60m | `docs/observed_attributes.md` (generated, committed). | Report lists actual span names, kinds and full attribute keys, with resolved SDK versions. §4 amended where it was wrong. |
| **T4** | ClickHouse schema from the T3 report. | 45m | `schema/001_spans_raw.sql`, `002_dead_letter.sql`. | Both tables created by `make up`; column set justified by T3, not by guesswork. |
| **T5** | Kafka consumer loop + OTLP decode + normalizer: message → flat spans → MCP fields. | 90m | `consumer.py`, `otlp_decode.py`, `normalize.py`. | Unit test over a captured OTLP fixture produces the expected row dict; `enable.auto.commit=false` is set and offsets commit after the insert. |
| **T6** | Failure taxonomy v0 and dead-letter path to `otlp.spans.dlq`. | 45m | `taxonomy.py`. | Success, `isError`, thrown exception and JSON-RPC protocol error map to four distinct categories; `input_required` maps to none of them. |
| **T7** | ClickHouse sink: batched insert, retry, dead-letter on decode failure. | 45m | `clickhouse_sink.py`. | Rows land; a deliberately malformed message lands in the DLQ, the partition advances, and the process stays up. |
| **T8** | **Buffer test** + verification script + README. | 60m | `scripts/verify_day1.py`, `README.md`. | `make verify` prints PASS for all §10 assertions, **including A8**: stop the normalizer, call all three tools, restart, and every span still arrives. |

*Total time-box: 8 hours 15 minutes. Adding Kafka costs about 45 minutes over the pre-Kafka plan (T2b, plus offset handling and the buffer test in T5 and T8) and removes the FastAPI ingest service entirely.*

### 8.1 Stretch items, only if T8 lands early

- Add a fourth demo tool that sleeps past a client timeout, so timeout-vs-error is a real, capturable case on Day 2.
- Return an `input_required` result from one tool to produce a genuine MRTR trace for the taxonomy work on Day 2.
- Run a second consumer group (a `kafka-console-consumer` counts) alongside the normalizer to demonstrate fan-out — the archiver and billing consumers of `Architecture.md` §3 are then purely additive.

> **Cut order if the day runs short.** Drop stretch items first, then T6 (taxonomy can be a single `is_error` boolean today), then the DLQ half of T7. **Never drop T3** — it is the one task whose output the rest of the week depends on — and **never drop A8**, which is the only assertion that proves the buffer does what the entire architecture claims it does.

---

## 9. Component specifications

### 9.1 docker-compose

```yaml
services:
  clickhouse:
    image: clickhouse/clickhouse-server:<pinned>
    ports: ["8123:8123", "9000:9000"]
    environment:
      CLICKHOUSE_DB: mcpobs
    volumes:
      - ./normalizer/schema:/docker-entrypoint-initdb.d:ro
      - ch-data:/var/lib/clickhouse
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8123/ping"]
      interval: 5s
      retries: 20

  kafka:
    image: apache/kafka:<pinned>          # KRaft mode, no ZooKeeper
    ports: ["9092:9092"]
    environment:
      KAFKA_NODE_ID: "1"
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: "1"
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"   # topics are explicit, always
    healthcheck:
      test: ["CMD-SHELL", "/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list"]
      interval: 5s
      retries: 20
    volumes:
      - kafka-data:/var/lib/kafka/data

  kafka-init:
    image: apache/kafka:<pinned>
    depends_on:
      kafka: { condition: service_healthy }
    volumes:
      - ./kafka/create_topics.sh:/create_topics.sh:ro
    entrypoint: ["/bin/sh", "/create_topics.sh"]   # otlp.spans.raw, otlp.spans.dlq

  normalizer:
    build: ./normalizer
    environment:
      KAFKA_BOOTSTRAP: kafka:9092
      KAFKA_TOPIC: otlp.spans.raw
      KAFKA_DLQ_TOPIC: otlp.spans.dlq
      KAFKA_GROUP_ID: normalizer
      CLICKHOUSE_URL: http://clickhouse:8123
      CLICKHOUSE_DB: mcpobs
      BATCH_MAX_ROWS: "10000"
      BATCH_MAX_SECONDS: "5"
    depends_on:
      clickhouse: { condition: service_healthy }
      kafka:      { condition: service_healthy }

  otel-collector:
    image: otel/opentelemetry-collector-contrib:<pinned>
    command: ["--config=/etc/otel/config.yaml"]
    volumes:
      - ./collector/config.yaml:/etc/otel/config.yaml:ro
    ports: ["4317:4317", "4318:4318"]
    depends_on:
      kafka: { condition: service_healthy }

volumes:
  ch-data:
  kafka-data:
```

> **`KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"` is deliberate.** Auto-created topics get default partition counts and no retention policy, and then quietly become production. Topics are a contract ([`Architecture.md` §6](./Architecture.md#6-the-kafka-contract)) and are created explicitly, even locally.

### 9.2 Collector configuration

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 75
  attributes/tenant:                 # production stamps these from the auth'd key
    actions:                          # locally they are hard-coded
      - { key: tenant.id,  value: local, action: upsert }
      - { key: project.id, value: local, action: upsert }
  batch:
    timeout: 1s
    send_batch_size: 512

exporters:
  kafka:
    brokers: [kafka:9092]
    topic: otlp.spans.raw
    encoding: otlp_proto             # never transcode to JSON
    partition_traces_by_id: false    # see ADR-004: partition by tenant, not trace
    producer:
      required_acks: -1              # acks=all -- the ack boundary
      compression: zstd
      max_message_bytes: 10000000
    retry_on_failure: { enabled: true, initial_interval: 1s, max_interval: 30s }
    sending_queue:   { enabled: true, queue_size: 1000 }
  debug:
    verbosity: detailed              # T3 reads this; turn down after T3

service:
  pipelines:
    traces:
      receivers:  [otlp]
      processors: [memory_limiter, attributes/tenant, batch]
      exporters:  [kafka, debug]
  telemetry:
    logs: { level: info }
```

- **`required_acks: -1` is the ack boundary.** It is the whole reason Kafka is here. With anything weaker, the Collector can return success for data that is not durable, which violates design goal 1 in `Architecture.md` §2.
- **`partition_traces_by_id: false`** implements [ADR-004](./Architecture.md#adr-004-partition-by-tenant-never-require-trace-locality). Keying by trace would explode every OTLP batch into per-trace messages; we partition by tenant and make the normalizer locality-independent instead. Verify the exact option name against the pinned Collector version during T2b — this setting has moved between releases.
- **`debug` with `verbosity: detailed` is the T3 instrument.** It prints every span with its full attribute set; T3 captures that output rather than reverse-engineering the SDK source. Turn it down once T3 is committed — it is expensive and noisy.
- **No sampling today.** Local volume is trivial and sampling would hide exactly the rare failure spans we want to inspect.
- **No ClickHouse exporter, not even as a backstop.** An earlier draft allowed one as a demo fallback. With Kafka in place the topic itself is the fallback: if the normalizer is not finished, the data is already durable and can be consumed later. That is precisely the property we bought.

### 9.3 ClickHouse Day-1 schema

Written **after T3**, following V2 §12: promote the MCP dimensions we query, retain everything else in maps, and version the normalizer so rows can be reprocessed.

```sql
CREATE TABLE IF NOT EXISTS mcpobs.spans_raw
(
    -- tenancy (hard-coded on Day 1, real in Phase 1)
    tenant_id             LowCardinality(String) DEFAULT 'local',
    project_id            LowCardinality(String) DEFAULT 'local',
    environment           LowCardinality(String) DEFAULT 'local',

    -- trace
    timestamp             DateTime64(9),
    duration_ns           UInt64,
    trace_id              String,
    span_id               String,
    parent_span_id        String,
    span_name             String,
    span_kind             LowCardinality(String),
    status_code           LowCardinality(String),
    status_message        String,

    -- service (from Resource, section 4.3)
    service_name          LowCardinality(String),
    service_version       LowCardinality(String),
    deployment_environment LowCardinality(String),

    -- MCP (section 4.2)
    mcp_method            LowCardinality(String),
    mcp_tool_name         LowCardinality(String),
    mcp_prompt_name       LowCardinality(String),
    mcp_resource_uri      String,
    gen_ai_operation      LowCardinality(String),
    protocol_version      LowCardinality(String),
    transport             LowCardinality(String),
    jsonrpc_request_id    String,
    mcp_session_id        Nullable(String),        -- see section 3.2; expected NULL

    -- failure
    mcp_is_error          UInt8 DEFAULT 0,
    result_type           LowCardinality(String),  -- complete | input_required
    failure_category      LowCardinality(String),
    error_type            LowCardinality(String),
    rpc_status_code       Nullable(String),  -- AMENDED (D14): the SDK sets str(code)

    -- downstream (promoted from child spans)
    http_method           LowCardinality(String),
    http_status_code      Nullable(UInt16),
    http_host             String,
    db_system             LowCardinality(String),

    -- payload (columns exist, NULL on Day 1)
    input_size            Nullable(UInt32),
    output_size           Nullable(UInt32),
    input_preview         Nullable(String),
    output_preview        Nullable(String),

    -- raw
    resource_attributes   Map(LowCardinality(String), String),
    span_attributes       Map(LowCardinality(String), String),
    normalization_version UInt16 DEFAULT 1,
    ingested_at           DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toDate(timestamp)
ORDER BY (tenant_id, project_id, toStartOfHour(timestamp), service_name, mcp_tool_name)
TTL toDate(timestamp) + INTERVAL 7 DAY;
```

- **`MergeTree` locally, `ReplicatedMergeTree` in staging and production.** This is not cosmetic: `insert_deduplication_token` — the mechanism behind [ADR-006](./Architecture.md#adr-006-at-least-once-delivery-with-idempotent-writes-not-exactly-once) — **only works on replicated tables**. A duplicate insert on this local schema will duplicate rows. Put that fact in a comment at the top of the DDL file, or someone will assume Day-1 behaviour is production behaviour and be wrong at the worst moment. Idempotency gets tested in staging, not on a laptop.
- **Ordering key.** Follows V2 §12.3 — tenant, project, time, service, tool. Trace-by-id lookup is a separate concern; on Day 1 a `WHERE trace_id = ...` full scan over a laptop-sized table is acceptable, and the trace-locator table arrives on Day 3 with the query service.
- **`mcp_session_id` is `Nullable`.** Expected to be NULL under 2026-07-28. Keeping the column proves the point rather than hiding it, and supports older servers.
- **`result_type` is first-class.** It distinguishes a completed call from an MRTR interim result and prevents the misclassification flagged in [§3.2](#32-what-each-fact-forces-in-our-design).
- **Seven-day TTL.** Prevents a forgotten laptop container from filling a disk.

```sql
CREATE TABLE IF NOT EXISTS mcpobs.ingest_dead_letter
(
    received_at   DateTime DEFAULT now(),
    reason        LowCardinality(String),
    detail        String,
    raw_body      String
)
ENGINE = MergeTree
ORDER BY received_at
TTL received_at + INTERVAL 3 DAY;
```

### 9.4 Demo MCP server and OTel bootstrap

The bootstrap follows V2 §18.1 exactly: configure the OpenTelemetry SDK and let the MCP SDK's own tracing produce the protocol spans. We add no wrapper around tool handlers and we do not create a second span per tool call.

```python
# demo_server/otel_bootstrap.py
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor


def init_telemetry(endpoint: str = "http://localhost:4318/v1/traces") -> None:
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "mcp-demo-server",
                "service.version": "0.1.0",
                "deployment.environment.name": "local",
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    # produces the downstream child span the Day-1 exit criterion requires
    HTTPXClientInstrumentor().instrument()
```

> **Verify against the installed SDK.** The exact server construction API for MCP Python SDK v2 must be read from the installed package during T1, not copied from any document — *including this one*. What matters and must not change is the principle: **the SDK emits the MCP spans; we only configure export.**

| Demo tool | Behaviour | What it proves |
| --- | --- | --- |
| `echo_fast` | Returns immediately. | Baseline latency and a clean success path; confirms span naming for `tools/call`. |
| `fetch_status` | `httpx.get` against a local endpoint returning 200 and 500. | Downstream HTTP span appears as a child of the MCP span — the V2 §25 checklist item. |
| `explode` | Raises an unhandled exception. | Distinguishes a thrown exception from a returned `isError` result — the other V2 §25 checklist item. |
| `ask_user` (stretch) | Returns `resultType: "input_required"`. | Provides a genuine MRTR trace so the taxonomy does not learn on synthetic data. |

### 9.5 Failure taxonomy v0

Deliberately small. Day 2 expands it; Day 1 only needs the branches to be structurally distinct.

| Category | Detection on Day 1 | Reachable? |
| --- | --- | --- |
| `ok` | No `error.type`, status not ERROR. | **Yes** |
| `tool_error` | `error.type == "tool_error"`. | **Yes** |
| `protocol_error` | `error.type` is numeric, or `rpc.response.status_code` set. | **Yes** — corrected on Day 2 (D19) |
| `server_exception` | `error.type` is an exception qualname, or an exception event. | No — see below |
| `pending_input` | `result_type == "input_required"`. | No — attribute not emitted (D11) |
| `unclassified` | Anything else. | Health metric only |

> ### AMENDED — the taxonomy is not reachable from span attributes
>
> Four deliberately distinct failure modes were exercised on Day 1: a tool returning
> `isError=True`, a handler raising `RuntimeError`, a call to an unknown tool, and a
> schema-violating argument. **All four produced an identical span**: status `ERROR`,
> `error.type="tool_error"`, no `rpc.response.status_code`, no exception event.
>
> `MCPServer`'s tool handler catches everything and converts it to
> `CallToolResult(isError=True)` *before* `OpenTelemetryMiddleware` observes the result,
> so the middleware's `except MCPError` and `except Exception` branches
> (`mcp/server/_otel.py:45-60`) are unreachable for anything routed through `tools/call`.
>
> **This invalidates a launch checklist item.** V2 §25 requires "MCP isError and thrown
> exception are distinguishable". That is *not achievable from span attributes* with the
> stock SDK. Assertion A3 is therefore split: A3a asserts `ok` and `tool_error` are both
> present (passes), A3b reports the gap as a documented WARN rather than a false pass.
>
> **The product stake.** V2 §6.3 sells an MCP failure taxonomy as a core differentiator.
> Today that taxonomy has one failure bucket. Options — result-content inspection (a
> payload feature, opt-in per V2 §15), an upstream SDK contribution, or shipping a single
> documented bucket for v1 — are on the Day-2 agenda, not in a backlog.
>
> The unreachable branches stay implemented: they *are* reachable for non-`tools/call`
> methods and for future SDK versions. They must simply not be advertised as working.
>
> **Corrected on Day 2 (D19).** `protocol_error` is in fact reachable: `_handle_call_tool`
> re-raises `MCPError` rather than converting it (`mcpserver/server.py:422`), so a tool that
> raises one *does* produce a numeric `error.type` and `rpc.response.status_code`. Day 1
> concluded it was unreachable only because no demo tool raised an `MCPError`. The wider
> finding stands — ordinary exceptions and `isError` results remain indistinguishable — but
> the taxonomy is one branch less broken than Day 1 reported. See
> [`Day_02`](./Day_02_Engineering_Doc_MCP_Observability.md) §3.3.

> **Do not collapse the reachable ones.** "The tool ran and said no", "the server threw" and "the protocol rejected the request" are three different on-call responses. Collapsing them into an error rate is precisely the generic-APM behaviour the product exists to avoid — which is exactly why the gap above matters rather than being a curiosity.

---

## 10. Verification and acceptance queries

`make verify` runs the demo scenarios, waits for the batch flush, then executes these assertions.

```sql
-- A1: spans arrived at all
SELECT count() FROM mcpobs.spans_raw
WHERE timestamp > now() - INTERVAL 10 MINUTE;
-- expect: > 0

-- A2: MCP tool calls are identified by attribute, not by name parsing
SELECT mcp_method, mcp_tool_name, count() AS calls,
       round(avg(duration_ns)/1e6, 2) AS avg_ms,
       sum(mcp_is_error) AS errors
FROM mcpobs.spans_raw
WHERE mcp_method = 'tools/call'
GROUP BY mcp_method, mcp_tool_name
ORDER BY calls DESC;
-- expect: rows for echo_fast, fetch_status, explode

-- A3: the three failure modes are distinguishable
SELECT failure_category, count()
FROM mcpobs.spans_raw
GROUP BY failure_category;
-- expect: ok, tool_error and server_exception all present and non-zero

-- A4: the downstream HTTP span is a child of the MCP span
SELECT p.mcp_tool_name AS tool, c.span_name AS child, c.http_status_code
FROM mcpobs.spans_raw AS c
INNER JOIN mcpobs.spans_raw AS p ON c.parent_span_id = p.span_id
WHERE p.mcp_tool_name = 'fetch_status';
-- expect: at least one HTTP child span with a status code

-- A5: full trace reconstruction from a trace id
SELECT span_id, parent_span_id, span_name, duration_ns, status_code
FROM mcpobs.spans_raw
WHERE trace_id = {trace_id:String}
ORDER BY timestamp;
-- expect: MCP root plus children, parent links resolving

-- A6: protocol revision is what we think it is
SELECT DISTINCT protocol_version FROM mcpobs.spans_raw;
-- expect: '2026-07-28'  (if empty, record it in observed_attributes.md; see 3.3)

-- A7: nothing silently dead-lettered
SELECT reason, count() FROM mcpobs.ingest_dead_letter GROUP BY reason;
-- expect: empty, except for the deliberate malformed-payload test
```

### A8 — the buffer test (the one that justifies Kafka)

Not a SQL assertion. This is the test that proves the architecture's central claim: that ingest survives everything downstream.

```bash
# 1. baseline
docker compose stop normalizer

# 2. generate traffic with NO consumer running
make demo                     # calls all three tools

# 3. the data must already be safe -- verify it is in Kafka, not lost
docker compose exec kafka /opt/kafka/bin/kafka-run-class.sh \
  kafka.tools.GetOffsetShell --bootstrap-server localhost:9092 \
  --topic otlp.spans.raw
# expect: offsets advanced while the normalizer was down

# 4. restart the consumer
docker compose start normalizer

# 5. every span arrives, from the offset where the consumer left off
#    -> re-run A2; the three demo tools are all present
```

**Pass condition:** zero spans lost, no customer-side retry involved, and the Collector returned 200 throughout. If A8 fails, the pipeline has a durability bug that no amount of downstream polish compensates for — fix it before Day 2.

**What A8 cannot prove locally:** idempotency on replay. With RF=1 and plain `MergeTree`, a redelivered batch duplicates rows ([§9.3](#93-clickhouse-day-1-schema)). Note this in the day's decision log and schedule the test for staging.

---

## 11. Day-1 exit checklist

Day 1 is done when every line below is true **from a clean checkout**, not from the state of a warm laptop.

- [ ] `git clone` → `make up` → `make demo` → `make verify` prints PASS for A1–A8, **including the buffer test**.
- [ ] Kafka topics were created explicitly by `kafka/create_topics.sh`, not auto-created.
- [ ] The normalizer sets `enable.auto.commit=false` and commits offsets only after the ClickHouse insert returns.
- [ ] `docs/observed_attributes.md` exists, is committed, and lists actual span names, attribute keys and resolved package versions.
- [ ] §4 of this document has been amended wherever the observed report contradicted it.
- [ ] `spans_raw` contains rows for all three demo tools with correct `mcp_tool_name`.
- [ ] A thrown exception and a returned `isError` produce different `failure_category` values.
- [ ] A downstream HTTP span is stored as a child of the MCP tool span, with parent linkage resolving.
- [ ] A malformed OTLP payload lands in `ingest_dead_letter` and the service stays up.
- [ ] Every image and package version is pinned.
- [ ] `docs/decisions.md` records the §13 decisions with today's date.
- [ ] §14's open questions are written down with an owner each.

> **Honest failure is a valid outcome.** If the MCP SDK's emitted attributes turn out to differ substantially from §4, Day 1 succeeds by documenting that accurately and re-planning Day 2 — not by bending the normalizer around a guess. The attribute report is the deliverable with the longest half-life.

---

## 12. Risks that will probably bite today

| Risk | Signal | Response |
| --- | --- | --- |
| MCP SDK v2 emits different span names or attribute keys than §4 expects. | T3 report disagrees with §4.2. | Expected. Amend §4, code against the report. This is why T3 precedes T5. |
| MCP SDK tracing is not enabled by default, or needs explicit configuration. | Collector `debug` shows httpx spans but no MCP spans. | Read the installed SDK's telemetry configuration; do **not** hand-roll MCP spans as a workaround — that violates V2 §18.1 and would poison the data model. |
| No spans arrive at all. | `debug` exporter silent. | Check in order: exporter endpoint path (`/v1/traces`), batch processor not yet flushed (force flush on shutdown), container networking (`localhost` inside a container is not the host). |
| Spans arrive but `duration_ns` or timestamps look wrong. | Negative or absurd durations. | OTLP times are `fixed64` nanoseconds since epoch; a Python `int`/`float` conversion is the usual culprit. |
| `protocol_version` is absent. | A6 returns empty. | Record it in the observed report and raise it as a question for Day 2 — do not synthesise the value. |
| httpx child span is not linked to the MCP span. | A4 returns nothing. | Context propagation issue: instrument before the server starts, and confirm the tool handler runs inside the MCP span's context. |
| ClickHouse rejects `Map(LowCardinality(String), String)` on the pinned version. | DDL error in T4. | Fall back to `Map(String, String)`; note it in the decision log. |
| Collector `kafkaexporter` option names differ from §9.2 on the pinned version. | Collector fails to start, or config keys ignored. | Expected — these options have moved between releases. Read the exporter README for the pinned tag; §9.2 is a sketch, the README is authoritative. |
| Kafka container unhealthy or unreachable from the Collector. | `make up` hangs on the healthcheck; connection refused. | Almost always `KAFKA_ADVERTISED_LISTENERS`. It must advertise `kafka:9092` (the compose service name), not `localhost`, or in-network clients get a loopback address back. |
| Consumer commits offsets before the ClickHouse insert. | A8 passes, but a crash mid-batch silently loses spans. | The most dangerous bug of the day because it *looks* fine. Review the commit call site explicitly; it belongs after the insert returns, never in a callback or on a timer. |
| Someone "fixes" a stalled partition by skipping the message. | Data silently disappears. | Poison messages go to the DLQ and the offset advances — that is the designed behaviour. Skipping without a DLQ write is data loss with extra steps. |
| Time sink: building UI or tenancy because it feels productive. | It is 4pm and A1 has never passed. | Enforce §1.3. The exit criterion is a queryable trace, not a pretty one. |

---

## 13. Decisions taken on Day 1

Recorded here and mirrored into `docs/decisions.md` so Day 2 does not relitigate them.

Decisions **D1–D3 are inherited from [`Architecture.md`](./Architecture.md#7-architecture-decision-records)**, which is the system of record for structural choices; the rest are local to Day 1. If you want the argument rather than the conclusion, read the ADRs.

| # | Decision | Rationale |
| --- | --- | --- |
| **D1** | Route through the OTel Collector, which produces to Kafka directly. No bespoke ingest service. | [ADR-003](./Architecture.md#adr-003-the-collector-writes-to-kafka-directly). Do not hand-write the most availability-critical tier of the system. |
| **D2** | **Kafka is in the path from Day 1** (single-node KRaft locally). *Supersedes the earlier "no Kafka today" decision.* | [ADR-002](./Architecture.md#adr-002-apache-kafka-as-the-durable-ingest-buffer). The ack boundary and offset-commit ordering are semantics, not infrastructure — retrofitting them later means rewriting the normalizer, and the retrofit is where at-least-once bugs are born. |
| **D2b** | Apache Kafka, not Redpanda. | [ADR-002](./Architecture.md#adr-002-apache-kafka-as-the-durable-ingest-buffer). Apache 2.0 licensing, open-source tiered storage, off-the-shelf Connect sinks, and managed availability in every region. |
| **D3** | The normalizer is stateless: one span in, one row out. Trace assembly happens in ClickHouse. | [ADR-005](./Architecture.md#adr-005-trace-assembly-in-clickhouse-not-a-stream-processor). No stream processor, now or later. |
| **D3b** | Partition by tenant; never require trace locality; never depend on ordering. | [ADR-004](./Architecture.md#adr-004-partition-by-tenant-never-require-trace-locality). Spans arrive out of order regardless — a child routinely lands before its parent. |

**Taken during execution:**

| # | Decision | Rationale |
| --- | --- | --- |
| **D11** | `result_type` column created but left NULL; `pending_input` dropped from taxonomy v0. | The SDK emits no `resultType` attribute, so MRTR interim results are invisible. Column kept so no migration is needed later. |
| **D12** | Capture every scenario over **both** stdio and streamable HTTP. | Needed to know whether transport changes the attribute set. It does not — the two are identical, so transport is unobservable on Day 1. |
| **D13** | Ship taxonomy v0 with only `ok` and `tool_error` reachable, and say so loudly. | See the amendment in [§9.5](#95-failure-taxonomy-v0). Inventing a distinction the telemetry cannot support would be worse than reporting the gap. |
| **D14** | `rpc_status_code` is `Nullable(String)`, not `Int32`. | The SDK sets `str(code)`. |
| **D15** | Demo server calls downstream via plain `httpx`, not the SDK's `httpx2`. | `opentelemetry-instrumentation-httpx` patches `httpx` only. With `httpx2` no child span appears and A4 passes vacuously. |
| **D16** | Kafka runs a dual listener (`kafka:9092` internal, `localhost:29092` host) with an explicit `KAFKA_LOG_DIRS`. | Both found the hard way: without the host listener, host clients get unresolvable metadata; without the log dir, the named volume is ignored and every container recreate silently destroys all topics. |
| **D4** | `mcp_session_id` is nullable and never a correlation key. | MCP 2026-07-28 removed protocol-level sessions. |
| **D5** | `result_type` is a first-class column from Day 1. | MRTR makes `input_required` a normal, non-error outcome that must not pollute error rates. |
| **D6** | Tool identity is read from `gen_ai.tool.name`, never parsed from the span name. | Span naming is conventional and target inclusion is optional. |
| **D7** | The MCP span is not assumed to be the trace root. | `traceparent` in `_meta` means an instrumented client legitimately becomes the parent. |
| **D8** | Method names are stored as free-form strings, not validated against an enum. | `server/discover`, `subscriptions/listen` and `tasks/*` are not in the OTel well-known list; extensions will add more. |
| **D9** | Payload columns are created but left NULL. | Capture is opt-in per V2 §15; creating the columns now avoids a migration, enabling it now creates a privacy question we are not ready to answer. |
| **D10** | The T3 observed-attribute report outranks this document wherever they disagree. | Empirical over assumed. See [§3.3](#33-day-1-rule-assert-do-not-assume). |

---

## 14. Open questions carried into Day 2

| Question | Why it matters | Resolve by |
| --- | --- | --- |
| **How do we recover a failure taxonomy the SDK does not expose?** Result-content inspection (payload feature), an upstream SDK contribution, or a single documented bucket for v1. | **Top of the Day-2 agenda.** V2 §6.3 sells the taxonomy as the core differentiator and V2 §25 makes it a launch gate. Today it has one failure bucket. See [§9.5](#95-failure-taxonomy-v0). | Day 2 — blocks the error-intelligence surface |
| Do we stitch MRTR round-trips into one logical tool call, or show them as separate calls? | Determines whether "tool latency" means server time or user-perceived time — and therefore what every latency chart in the product means. | Day 2, with the taxonomy work |
| How do we represent `subscriptions/listen` spans? | A long-lived stream is not a latency measurement and will destroy p95 charts if treated as one. | Day 2 |
| Does the installed SDK emit `mcp.protocol.version`, and does it read `2026-07-28`? | It is our only per-span signal of which protocol revision a server speaks — needed for version-regression views later. | T3, today |
| Are `tasks/*` extension calls traced by the SDK at all? | Async task execution is exactly the case where observability is most valuable and least visible. | Day 2 investigation |
| Can we usefully enrich at the Collector from `Mcp-Method` / `Mcp-Name` headers? | Cheap edge-level MCP awareness without body inspection; relevant to Phase 1 quota and routing. | Phase 1 design |
| What is the trace-locator table shape for fast trace-by-id lookup? | V2 §12.3 defers it; the query service on Day 3 needs it. | Day 3 |
| How do we detect client retries after a broken stream? | Resumability removal means retries look like unrelated calls; over-reporting them would be worse than not reporting them. | Phase 1 |
| Does the pinned Collector's `kafkaexporter` support the partitioning and ack options in §9.2 as written? | [ADR-004](./Architecture.md#adr-004-partition-by-tenant-never-require-trace-locality) and [ADR-008](./Architecture.md#adr-008-acknowledge-only-after-kafka-durably-accepts) both depend on it. | T2b, today |
| What batch size and flush interval balance ClickHouse insert efficiency against ingest freshness? | Freshness is the headline pipeline metric (`Architecture.md` §9.1). | Day 2, with real volume |
| Does the deterministic offset-range batching that idempotency requires conflict with time-based flushing? | [ADR-006](./Architecture.md#adr-006-at-least-once-delivery-with-idempotent-writes-not-exactly-once) needs replayed batches to be byte-identical. | Day 2 — design it before staging |

---

## 15. Day 2 preview

- Harden the normalizer against the T3 report: full attribute coverage, normalization versioning, dead-letter metrics.
- Trace assembly: derive a trace summary row (root span, server, tool, total duration, worst status) as spans arrive.
- Failure taxonomy v1, including timeout and cancellation, driven by the real cases captured today.
- Downstream dimension extraction from child spans (HTTP, DB, and LLM calls made by the server).
- Answer the MRTR question from [§14](#14-open-questions-carried-into-day-2) — it blocks the latency semantics of every later view.

---

## 16. References

1. Model Context Protocol specification, revision 2026-07-28 — <https://modelcontextprotocol.io/specification/2026-07-28>
2. MCP 2026-07-28 key changes (changelog) — <https://modelcontextprotocol.io/specification/2026-07-28/changelog>
3. MCP base protocol: messages, statelessness, `_meta`, error codes — <https://modelcontextprotocol.io/specification/2026-07-28/basic>
4. MCP message patterns: request/response, MRTR, subscribe and notify — <https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns>
5. OpenTelemetry semantic conventions for MCP spans — <https://github.com/open-telemetry/semantic-conventions-genai/blob/main/model/mcp/spans.yaml>
6. OpenTelemetry MCP attribute registry — <https://opentelemetry.io/docs/specs/semconv/registry/attributes/mcp/>
7. OpenTelemetry Collector, gateway deployment pattern — <https://opentelemetry.io/docs/collector/deployment/gateway/>
8. OpenTelemetry Collector contrib, Kafka exporter — <https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/kafkaexporter>
9. Apache Kafka documentation (KRaft, producer and consumer configuration) — <https://kafka.apache.org/documentation/>
10. ClickHouse observability guidance (attribute promotion, schema design) — <https://clickhouse.com/docs/en/observability>
11. [`Architecture.md`](./Architecture.md) v1.0 — **system of record**; ADR-001 to ADR-010 hold the reasoning behind §13's decisions.
12. `MCP_Observability_Product_and_System_Design_Python_V2.docx`, v0.2, 14 Aug 2026 — parent product and system design.

---

*v0.2 · 14 Aug 2026 · Implements [`Architecture.md`](./Architecture.md) v1.0 · Companion to MCP_Observability_Product_and_System_Design_Python_V2.docx · Internal*
