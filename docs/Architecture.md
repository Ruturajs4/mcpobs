# MCP Observability Platform — Architecture

> System-of-record architecture for the MCP observability data plane. Kafka-buffered, OpenTelemetry-native, ClickHouse-backed, one cell per region.

| Field | Value |
| --- | --- |
| Document | Architecture (system of record) |
| Version | 1.0 |
| Date | 14 August 2026 |
| Status | Accepted — supersedes the reference diagram in V2 §9 where they differ |
| Parent document | `MCP_Observability_Product_and_System_Design_Python_V2.docx` (v0.2) |
| Companion | `Day_01_Engineering_Doc_MCP_Observability.md` |
| MCP protocol revision | `2026-07-28` |
| Durable buffer | Apache Kafka (managed in production, KRaft single node locally) |
| Primary store | ClickHouse |

> **How to read this document.** [§3](#3-target-architecture) is the picture, [§6](#6-the-kafka-contract) is the contract everything depends on, and [§7](#7-architecture-decision-records) is *why* — ten ADRs, each with the alternatives we rejected. If you disagree with something in §3, the argument you want is in §7.

---

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [Design goals, in priority order](#2-design-goals-in-priority-order)
3. [Target architecture](#3-target-architecture)
4. [Component responsibilities](#4-component-responsibilities)
5. [Data flow](#5-data-flow)
6. [The Kafka contract](#6-the-kafka-contract)
7. [Architecture decision records](#7-architecture-decision-records)
8. [Failure modes](#8-failure-modes)
9. [Scaling model and capacity](#9-scaling-model-and-capacity)
10. [Environment ladder](#10-environment-ladder)
11. [Deliberately not in this architecture](#11-deliberately-not-in-this-architecture)
12. [Open questions](#12-open-questions)
13. [References](#13-references)

---

## 1. Purpose and scope

This document defines how the platform is built. The V2 product specification defines what it is and for whom; where the two disagree on structure, this document wins and V2 should be amended.

**In scope.** The server-side telemetry path: from a customer's MCP server emitting OpenTelemetry spans, to a queryable, MCP-aware trace in our UI. Ingestion, durability, normalization, storage, query and the control plane.

**Out of scope.** Everything in V2 §20.2 — client-side observability, MCP gateway/runtime, evaluation suites. Also out of scope here: UI component design, pricing mechanics, and go-to-market.

**Product boundary reminder.** We observe what crosses the MCP server boundary and what the server does downstream. This is a hard architectural boundary, not a temporary limitation, and it is why there is no client-side ingestion path in [§3](#3-target-architecture).

---

## 2. Design goals, in priority order

The order matters. Where two goals conflict, the higher one wins, and several decisions in [§7](#7-architecture-decision-records) are resolved purely by this ordering.

1. **Never lose acknowledged telemetry.** If we return 200, the data is durable. An observability vendor that drops data during the customer's incident has no product.
2. **Ingest survives everything downstream.** ClickHouse degradation, normalizer bugs and schema migrations must not stop accepting data.
3. **Raw data is reprocessable.** Every derived field is regenerable from what we received. Normalizer bugs are recoverable, not permanent corruption.
4. **Tenant isolation.** One customer's volume or query pattern cannot degrade another's.
5. **Operational simplicity.** Every stateful component is a component someone gets paged for. Fewer is better, and managed is better than self-hosted.
6. **Query latency.** Dashboards feel instant. Deliberately last — a fast product that lost yesterday's data is worthless.

---

## 3. Target architecture

```text
════════════════════════ CUSTOMER ENVIRONMENT ════════════════════════

  MCP server (Python MCP SDK v2)
    ├─ MCP protocol spans        (emitted by the SDK's own OTel tracing)
    ├─ HTTP / DB / Redis / LLM   (auto-instrumentation, child spans)
    └─ OTel SDK: TracerProvider → BatchSpanProcessor → OTLP exporter
         │
         │  (optional) customer-side Collector — may fan out to
         │             their existing APM as well as to us
         │
         │  OTLP  gRPC :4317  |  HTTP :4318
         ▼
═══════════════ REGIONAL DATA PLANE — one independent cell ════════════

  ┌── Edge / load balancer ─────────────────────────────────────────┐
  │   TLS termination · DDoS · rate limit · API key → tenant lookup │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌── OTel Collector gateway fleet ── stateless, autoscaled ────────┐
  │   receivers   otlp [grpc, http]                                 │
  │   extensions  bearertokenauth / custom auth  (key → tenant)     │
  │   processors  memory_limiter                                    │
  │              → attributes  (stamp TRUSTED tenant/project/region)│
  │              → batch                                            │
  │   exporters   kafka        acks=all, otlp_proto encoding        │
  │                                                                 │
  │   NO analytics, NO ClickHouse, NO payload work in this tier     │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌── Apache Kafka ── managed ── the durability boundary ───────────┐
  │                                                                 │
  │   topic  otlp.spans.raw     key=tenant_id   RF=3   48–72h       │
  │   topic  otlp.spans.dlq     key=tenant_id   RF=3   14d          │
  │   tiered storage → S3 for extended replay                       │
  │                                                                 │
  │   ◄── ACK BOUNDARY: we return 200 only past this line           │
  └──┬────────────┬─────────────────┬──────────────────┬────────────┘
     │            │                 │                  │
     │ group:     │ group:          │ group:           │ group:
     │ normalizer │ archiver        │ billing          │ (future) alerting
     ▼            ▼                 ▼                  ▼
  ┌────────────┐ ┌──────────────┐ ┌─────────────┐  ┌──────────────┐
  │ Normalizer │ │ S3 Sink      │ │ Billing     │  │ Rule eval    │
  │ STATELESS  │ │ Connector    │ │ meter       │  │              │
  │            │ │ (off-the-    │ │             │  │              │
  │ decode →   │ │  shelf)      │ │ spans/bytes │  │              │
  │ MCP fields │ │              │ │ per tenant  │  │              │
  │ → taxonomy │ │ parquet      │ │             │  │              │
  │ → batch    │ │ archive      │ │             │  │              │
  └─────┬──────┘ └──────┬───────┘ └──────┬──────┘  └──────────────┘
        │               ▼                ▼
        │          S3 / object       PostgreSQL
        │          storage           (usage counters)
        ▼
  ┌── ClickHouse ── ReplicatedMergeTree ────────────────────────────┐
  │                                                                 │
  │   spans_raw  ──MV──►  trace_summaries                           │
  │             ──MV──►  trace_locator                              │
  │             ──MV──►  server_metrics_1m                          │
  │             ──MV──►  tool_metrics_1m                            │
  │             ──MV──►  client_tool_metrics_1m                     │
  │                                                                 │
  │   trace assembly happens HERE, not in a stream processor        │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
  ┌── Query API ────────────────────────────────────────────────────┐
  │   tenant filter enforced server-side · rollup-vs-raw routing    │
  │   keyset pagination · timeouts · row caps · Redis result cache  │
  └────────────────────────────┬────────────────────────────────────┘
                               ▼
                            Web UI

══════════════════ CONTROL PLANE — global, not per-cell ═══════════════
  PostgreSQL — orgs · users · projects · API keys · plans
               retention policy · payload policy · settings
  Object storage — large payloads, raw OTLP archive
```

**The one line that defines the system.** Everything left of Kafka optimizes for *accepting data and never losing it*. Everything right of Kafka optimizes for *making data useful*. The two halves fail independently, deploy independently, and scale on different signals. That separation is the architecture; the rest is detail.

---

## 4. Component responsibilities

| Component | Owns | Explicitly does not own | State |
| --- | --- | --- | --- |
| Edge / LB | TLS, DDoS, rate limiting, region routing | Business logic, tenant resolution beyond key lookup | Stateless |
| Collector gateway | OTLP protocol, auth, trusted tenant stamping, batching, Kafka produce | Analytics, MCP semantics, storage, payload handling | Stateless |
| Kafka | Durability, replay, fan-out, backpressure | Transformation, business logic | **Stateful** |
| Normalizer | OTLP decode, MCP field extraction, failure taxonomy, ClickHouse batch insert | Trace assembly, aggregation, cross-span state | **Stateless** |
| S3 sink connector | Raw archive to parquet | Anything MCP-aware | Stateless (offsets in Kafka) |
| Billing meter | Per-tenant span and byte counters | Enforcement, invoicing | Stateless |
| ClickHouse | Span storage, trace assembly, rollups | Auth, tenant policy | **Stateful** |
| Query API | Tenant enforcement, query routing, DTO stability, caching | Storage layout leakage into DTOs | Stateless |
| PostgreSQL | Orgs, projects, keys, plans, policy | Telemetry | **Stateful** |

Four stateful components. That count is a budget, not an observation — see [ADR-010](#adr-010-kafka-is-not-a-general-purpose-internal-bus).

---

## 5. Data flow

### 5.1 Write path (the hot path)

```
receive OTLP
  → authenticate API key                    (cached, Redis-backed)
  → resolve tenant / project / environment
  → quota check                             (soft: flag; hard: reject)
  → validate OTLP structure
  → stamp TRUSTED tenant attributes         (overwrite anything customer-sent)
  → batch
  → produce to Kafka, acks=all
  → ─────────── ACK BOUNDARY ───────────
  → return 200
```

Nothing after the ack boundary can cause data loss. Nothing before it may perform analytics, touch ClickHouse, classify payloads or run any operation whose latency we do not control. This is V2 §10.2 restated as a hard rule.

**Trusted stamping matters.** Tenant, project and region attributes are written by us from the authenticated key, overwriting anything the customer's resource attributes claim. A customer must not be able to write telemetry into another tenant by setting a resource attribute.

### 5.2 Normalization path

```
consume otlp.spans.raw
  → decode OTLP protobuf
  → flatten ResourceSpans / ScopeSpans / Span
  → extract MCP fields          (mcp.method.name, gen_ai.tool.name, …)
  → derive failure_category     (versioned; raw attributes retained)
  → stamp normalization_version
  → batch 10k rows / 5s
  → INSERT … SETTINGS insert_deduplication_token = '<topic>-<part>-<offset-range>'
  → commit offsets AFTER the insert returns
```

Offsets commit after the write, never before. A crash between insert and commit replays the batch, and the deduplication token makes the replay a no-op. This is the whole of our delivery-semantics design — see [ADR-006](#adr-006-at-least-once-delivery-with-idempotent-writes-not-exactly-once).

### 5.3 Read path

```
UI → Query API
       → enforce tenant/project/environment filter (server-side, always)
       → route: dashboard → rollup tables
                trace list → spans_raw with time+tenant prefix
                trace by id → trace_locator, then spans_raw
       → Redis cache, 10–30s TTL for dashboard queries
       → stable DTO (independent of ClickHouse layout)
```

### 5.4 Replay path

The path that justifies Kafka's existence:

```
normalizer bug found / new derived field added
  → deploy normalizer vN+1
  → reset a dedicated consumer group to the desired offset or timestamp
  → reprocess into spans_raw with normalization_version = N+1
  → dedupe/select on max(normalization_version) per (trace_id, span_id)
  → no customer is asked to resend anything
```

---

## 6. The Kafka contract

Everything downstream depends on this contract. Changes here are breaking changes.

### 6.1 Topics

| Topic | Key | Value | Partitions | RF | Retention | Consumers |
| --- | --- | --- | --- | --- | --- | --- |
| `otlp.spans.raw` | `tenant_id` (salted for whales) — **not implemented, see [ADR-004](#adr-004-partition-by-tenant-never-require-trace-locality); the key is null today** | OTLP `ExportTraceServiceRequest`, protobuf | 24 to start, sized per [§9](#9-scaling-model-and-capacity) | 3 | 72h + tiered to S3 | normalizer, archiver, billing, (future) alerting |
| `otlp.spans.dlq` | `tenant_id` | Original payload + failure reason envelope | 6 | 3 | 14d | manual triage, replay tooling |

One raw topic, not one per signal or per tenant. Per-tenant topics do not scale — partition count is a cluster-wide resource and thousands of topics wreck rebalance times.

### 6.2 Producer settings

| Setting | Value | Why |
| --- | --- | --- |
| `acks` | `all` | Design goal 1. Anything less can lose acknowledged data. |
| `min.insync.replicas` | `2` (with RF=3) | Tolerates one broker loss without losing write availability. |
| `enable.idempotence` | `true` | Prevents duplicates from producer-internal retries. Free. |
| `compression.type` | `zstd` | Telemetry is highly compressible; this is the cheapest cost lever available. |
| `max.in.flight.requests` | `5` | Safe with idempotence enabled. |
| Encoding | `otlp_proto` | Keep the wire format. Never transcode to JSON — it inflates volume and loses fidelity. |

### 6.3 Consumer settings

| Setting | Value | Why |
| --- | --- | --- |
| `enable.auto.commit` | `false` | Offsets commit after the ClickHouse write, never on a timer. |
| `isolation.level` | `read_committed` | Harmless, and correct if transactions ever appear. |
| `max.poll.records` | tuned to batch size | Poll loop shape should mirror the ClickHouse batch shape. |
| Group per consumer role | `normalizer`, `archiver`, `billing` | Independent lag, independent scaling, independent replay. |

### 6.4 Invariants

1. **Ordering is per-partition only, and we depend on it nowhere.** Spans arrive out of order regardless — a child span routinely lands before its parent, because the parent has not ended yet. Any design that needs global ordering is wrong.
2. **A trace's spans may be split across partitions.** Guaranteed only if a single tenant maps to one partition, which whale-salting deliberately breaks. See [ADR-004](#adr-004-partition-by-tenant-never-require-trace-locality).
3. **Every consumer is replayable from any retained offset** without coordinating with any other consumer.
4. **The raw topic is immutable.** No consumer writes back to it. Derived data flows forward only.

---

## 7. Architecture decision records

### ADR-001: OpenTelemetry and OTLP as the telemetry contract

**Status.** Accepted (inherited from V2 §8).

**Context.** We could define a proprietary SDK and wire protocol, as several observability vendors historically did.

**Decision.** Standard OpenTelemetry in, standard OTLP over gRPC and HTTP/protobuf. The MCP Python SDK v2 already emits MCP protocol spans; we configure export and enrich downstream rather than wrapping the protocol ourselves.

**Consequences.** No SDK lock-in, so no acquisition friction and no instrumentation maintenance burden as MCP evolves. Customers can dual-export to us and to their existing APM, which lowers the barrier to trying us. The cost is that we accept whatever the SDK emits and must version our normalizer against semantic-convention drift.

**Rejected.** A proprietary SDK — faster to control, but it makes us responsible for MCP protocol tracing forever and gives customers a reason not to adopt.

---

### ADR-002: Apache Kafka as the durable ingest buffer

**Status.** Accepted. Supersedes an earlier Redpanda recommendation.

**Context.** Design goals 1–3 all require a durable, replayable buffer between accepting telemetry and storing it. The candidates were Apache Kafka, Redpanda, a cloud-native queue (SQS/Pub-Sub) and no buffer at all.

**Decision.** Apache Kafka, managed in production (MSK, Confluent Cloud, Aiven or the equivalent on whichever cloud holds our commit), single-node KRaft container for local development.

**Why not Redpanda.** It was the initial recommendation on the strength of operational simplicity, and the argument did not hold up:

- Its core is BSL-licensed, not OSI open source. We sell infrastructure and our V2 pricing anticipates an Enterprise tier with data regions and dedicated capacity — the direction that ends in "can we run this in our VPC?" A BSL component in the data path turns that into a legal review every time. Apache 2.0 never generates that conversation.
- **Tiered storage is open source in Kafka and enterprise-licensed in Redpanda.** This is load-bearing: our replay window *is* the architecture, and tiered storage is what extends retention from 72 hours to weeks without buying broker disk. (Confirm availability for the specific Kafka version deployed — this landed relatively recently.)
- The "fewer moving parts" argument was stale. KRaft removed ZooKeeper; Kafka is a single process too.
- The "avoid JVM operations" argument only applies if we self-host. Managed Kafka removes heap tuning, GC pauses and rebalance operations from our on-call surface entirely — and managed Kafka is available in far more regions and procurement paths.
- Redpanda's real advantages — thread-per-core tail latency, no GC pauses — target a problem we do not have. Our ingest is batch-oriented behind a Collector batching on a one-second timer. A p99 produce latency of 8ms versus 25ms is invisible in our SLO.

**Why not SQS / Pub-Sub.** No replay from arbitrary offsets, no independent consumer groups over the same retained log, and cloud lock-in in an architecture explicitly designed as portable regional cells. Replay is not a nice-to-have here; it is ADR-005's safety net.

**Why not "no buffer".** Then ClickHouse availability becomes ingest availability, and a schema migration or merge-pressure incident becomes customer data loss. Non-negotiable against design goal 1.

**Consequences.** We own a stateful system with lag, rebalance and partition semantics — mitigated but not eliminated by using managed Kafka. In exchange we get design goals 1–3, plus Connect ([ADR-003](#adr-003-the-collector-writes-to-kafka-directly)) and a large operational talent pool.

**Reversibility.** The decision is contained. Every ADR here is expressed at the Kafka *API* level, so switching implementations later changes an endpoint and nothing else.

---

### ADR-003: The Collector writes to Kafka directly

**Status.** Accepted.

**Context.** The Day-1 prototype used a bespoke Python service (`ingest_dev`) to receive OTLP over HTTP and write onward. The question was whether that service graduates to production.

**Decision.** It does not. The OTel Collector gateway produces to Kafka directly via `kafkaexporter`. An auth extension resolves API key to tenant; an attributes processor stamps trusted tenant metadata. No hand-written service sits in the ingest path.

**Consequences.** The most availability-critical tier of the system becomes configuration of a widely deployed, battle-tested binary rather than code we maintain. We inherit the Collector's batching, retry, queue and backpressure behaviour instead of reimplementing it — badly, on a deadline. The cost is that custom auth logic must fit the Collector's extension model, and complex tenant resolution may require a small custom extension.

**Second-order benefit.** The same reasoning applies downstream: the raw archiver becomes an off-the-shelf S3 Sink Connector rather than a service we write, monitor and page someone about. (Check the connector's licence — Confluent's carries a community licence, Aiven's is Apache 2.0. Having made a licensing argument in ADR-002, we should apply it consistently.)

**Rejected.** A custom ingest service — more control over auth and quota logic, but it puts our own code on the path where an outage means refusing customer telemetry.

---

### ADR-004: Partition by tenant, never require trace locality

**Status.** Accepted.

**Context.** The instinct is to key by `trace_id` so every span of a trace lands in one partition, enabling stream-side trace assembly.

**Decision.** Key by `tenant_id`, with a salt suffix for high-volume tenants to spread them across partitions. Design every consumer to be locality-independent and order-independent.

**Why.** An OTLP batch contains spans from many traces. Keying by `trace_id` forces exploding every batch into per-trace messages before producing — multiplying producer CPU, destroying batch efficiency and inflating message count by orders of magnitude. And it buys less than it appears to: spans arrive out of order anyway, so a stream-side assembler needs windowing and late-arrival handling regardless.

**Consequences.** Batches stay intact and cheap. Tenant-level isolation is natural, and per-tenant lag is measurable. Trace assembly moves to ClickHouse ([ADR-005](#adr-005-trace-assembly-in-clickhouse-not-a-stream-processor)). Whale tenants need salting, which requires monitoring partition skew — the one real operational burden this creates.

**Rejected.** Keying by `trace_id` (batch explosion, and it still needs windowing). Round-robin with no key (loses tenant isolation and makes per-tenant lag unmeasurable).

> **AS BUILT (2026-08-16): this decision is NOT IMPLEMENTED, and what runs today is the option rejected above.**
>
> Every message on `otlp.spans.raw` carries a **null key** and is round-robined across partitions. Measured directly from the topic, not inferred:
>
> ```
> $ kafka-console-consumer --property print.key=true --property print.value=false
> null
> null
> null
> ```
>
> The cause is upstream. The Collector's `kafkaexporter` has no option to key a trace message by a resource attribute, so nothing in the current pipeline can set it. Tested against the running images rather than assumed — `partition_by_resource_attributes`, `partition_traces_by_resource_attributes` and `partition_logs_by_resource_attributes` are all rejected as invalid keys by **0.115.1** and by **0.130.0**:
>
> ```
> '' has invalid keys: partition_by_resource_attributes
> ```
>
> **What this costs, in the terms this ADR itself uses.** There is no tenant-level isolation and no per-tenant lag — the two consequences the "Rejected" line above says round-robin loses. It also makes the whale-salting operational burden moot: there is nothing to salt until there is a key, so [DF-16](deferred.md) cannot be worked on before [DF-19](deferred.md).
>
> **The decision stands as the target.** Closing the gap needs one of: an upstream option in `kafkaexporter`, a custom exporter, or the ingest gateway producing to Kafka itself — which [ADR-003](#adr-003-the-collector-writes-to-kafka-directly) rejects, and that rejection is still right. Tracked as DF-19.

---

### ADR-005: Trace assembly in ClickHouse, not a stream processor

**Status.** Accepted.

**Context.** Turning individual spans into trace summaries — root span, server, tool, total duration, worst status, span count — is a windowed aggregation over a late-arriving, unbounded key space. The classic answer is a stateful stream processor: Flink, Kafka Streams, Spark Streaming.

**Decision.** The normalizer stays stateless: one span in, one row out. Trace assembly happens in ClickHouse via materialized views into an `AggregatingMergeTree` keyed on `trace_id`.

**Why.** Stateful stream processing is the single most reliable way for a small team to lose a quarter. It brings checkpointing, state backends, savepoint-compatible upgrades, watermark tuning and an entirely separate operational discipline. ClickHouse does incremental aggregation as a native, well-trodden feature, and we are already running it. Late-arriving spans — which are the normal case, not the exception — merge into the aggregate correctly with no windowing logic at all.

**Consequences.** Stateless consumers scale linearly, restart in seconds and replay trivially. One fewer stateful system in the budget. The trade is that assembly correctness now depends on ClickHouse merge behaviour and aggregate-function semantics, which the team must actually understand rather than treat as a black box.

**Rejected.** Flink or Kafka Streams — the right answer at ten times our scale or if we needed sub-second derived signals. We need neither, and the cost is enormous at our size.

---

### ADR-006: At-least-once delivery with idempotent writes, not exactly-once

**Status.** Accepted.

**Context.** Duplicate spans would corrupt counts and latency percentiles. Kafka offers transactional exactly-once semantics; ClickHouse is not a transactional participant.

**Decision.** At-least-once delivery. Idempotency at the storage layer: `ReplicatedMergeTree` with `insert_deduplication_token` derived from `topic-partition-offset-range`. Commit offsets only after the insert returns. Natural idempotency key is `(trace_id, span_id)`.

**Why.** A replayed batch after a consumer crash produces a byte-identical block, which ClickHouse discards natively. That gets us effectively-once outcomes with two settings instead of a transactional coordinator spanning two systems that were never designed to coordinate.

**Consequences.** Requires `ReplicatedMergeTree`, which we want for HA anyway — deduplication is a free consequence of a decision already made. The batch boundary must be deterministic on replay, which constrains the consumer to fixed offset-range batching rather than opportunistic accumulation.

**Rejected.** Kafka transactions (do not extend to ClickHouse; substantial complexity for no additional guarantee here). Application-level dedupe via a Redis seen-set (another stateful component, a new failure mode, and unbounded memory growth).

---

### ADR-007: Raw-topic retention is the replay window

**Status.** Accepted.

**Context.** Kafka retention is usually treated as an operational parameter. Here it is a product capability.

**Decision.** 72 hours hot retention on `otlp.spans.raw`, extended via tiered storage to S3. Retention is set by explicit product requirement, not by disk convenience.

**Why.** The window buys three distinct capabilities: surviving a multi-hour ClickHouse outage with zero loss; reprocessing after a normalizer bug (the `normalization_version` column exists for exactly this); and backfilling newly derived fields without asking customers to resend data they no longer have.

**Consequences.** Sizing must be deliberate. At roughly 1KB/span compressed and 50k spans/second, 72 hours is about 13TB — see [§9](#9-scaling-model-and-capacity). Tiered storage is what keeps that affordable and is a material input to ADR-002.

**Rejected.** Short retention with an S3 archive as the only replay source — cheaper, but replay then requires a separate batch pipeline reading parquet instead of the same consumer code reading the same topic. One replay mechanism is worth paying for.

---

### ADR-008: Acknowledge only after Kafka durably accepts

**Status.** Accepted (V2 §10.3, restated as a binding rule).

**Decision.** The Collector returns success only after `acks=all` from Kafka. If Kafka is unavailable, return a retryable 503; if the tenant is over a hard quota, return an explicit rejection. Never ack data we might drop.

**Why.** The customer's OTel exporter has a retry queue built in and is designed for exactly this. Silently accepting and dropping converts our incident into their invisible data loss — discovered during their outage, while they are using our product to debug it.

**Consequences.** Kafka availability becomes ingest availability, which is why it is RF=3 with `min.insync.replicas=2` and managed. A visible 503 is strictly better than invisible loss.

---

### ADR-009: One independent cell per region

**Status.** Accepted (inherited from V2 §17.3).

**Decision.** Each region runs its own Collector fleet, Kafka cluster, normalizer, ClickHouse and query API. The control plane (Postgres) is global. No cross-region queries in v1.

**Consequences.** Blast radius is one region. Data residency — an Enterprise requirement in the V2 pricing model — is satisfied structurally rather than by policy. The cost is that a customer with servers in several regions sees several projects until cross-region query arrives, which we accept for v1.

---

### ADR-010: Kafka is not a general-purpose internal bus

**Status.** Accepted.

**Context.** Once a broker exists, every service wants to publish to it. This is how a data pipeline becomes a distributed monolith.

**Decision.** Kafka carries telemetry ingest only. Control-plane operations (project creation, key rotation, settings changes) are synchronous Postgres writes. Service-to-service calls stay synchronous. New topics require an explicit architecture decision, recorded here.

**Why.** Design goal 5. Every topic is a schema, a consumer group, a lag alert and a replay story. The four stateful components in [§4](#4-component-responsibilities) are a budget, and this ADR is what defends it.

---

### ADR-011: Short-lived session tokens for client-launched servers

**Status.** Accepted. Not yet implemented.

**Context.** Roughly half of MCP servers run on **stdio**, which means the
*client* launches them — Claude Desktop, an IDE, another agent — on the end
user's own machine. The only way to give such a server an ingest credential
today is the client's config file:

```json
{"mcpServers": {"acme": {"command": "...", "env": {
  "OTEL_EXPORTER_OTLP_HEADERS": "x-api-key=mcpo_live_..."}}}}
```

That file is plaintext on a laptop. It is synced, backed up, pasted into
support tickets, and readable by every process running as that user. And the
credential in it is **org-wide and permanent**: one copy lets anyone write
telemetry into that tenant indefinitely — exhausting its quota, poisoning its
error rates, and populating its console with servers that do not exist.

The exposure is not "a key leaked". It is "a key leaked and nothing expires".

**Decision.** The customer hosts a **session endpoint**. Our SDK calls it,
receives a short-lived token (default 3h), and uses that to export. The
customer's long-lived key stays on the customer's servers and is never present
on an end user's machine.

```
end user ──auth the customer already has──▶ customer's session endpoint
                                                    │ long-lived mint key,
                                                    │ server-side only
                                                    ▼
                                          POST /api/v1/sessions
                                                    │
                                          session token (3h) ──▶ SDK
```

The contract the SDK expects from the customer's endpoint:

```json
{
  "token": "...",
  "expires_in": 10800,
  "endpoint": "https://ingest.eu.example.io",
  "attributes": {"user_id": "u_931", "workspace": "acme-eu"}
}
```

`expires_in` rather than `expires_at` because laptop clocks are wrong — the
same reason latency percentiles here carry a measured-clock caveat. A relative
TTL cannot be misread by a skewed clock.

Session tokens are **JWTs**, verified locally by the ingest gateway. They carry
`aud`, `kid`, `jti` and a revocation `epoch` from the first release.

**Why the customer hosts the endpoint.** We have no relationship with the end
user; the customer does. They already authenticate that user — session cookie,
OAuth, whatever their product uses. Minting per-user credentials ourselves would
require us to hold their user directory or share a per-user secret, which
recreates the leak we are removing. This design borrows an authentication
relationship that already exists rather than inventing a second one.

**Why JWT rather than opaque tokens in Redis.** Not storage — a session entry
is a few hundred bytes, so ten thousand concurrent users is a few megabytes in a
Redis that is already running. Two other reasons decide it:

* **Availability coupling.** Ingest is the highest-volume path in the system. An
  opaque token means a lookup on every span batch, and Redis being unavailable
  becomes total ingest failure for every customer. Local verification has no
  such dependency.
* **Cache locality collapses.** The existing 30-second principal cache works
  because an *org* key is shared by thousands of requests. A *per-user* session
  key is used by one laptop. The same cache would hit almost never, so the
  lookup would be paid nearly every time.

**Why `jti` and `epoch` ship before anything reads them.** A blacklist can only
reject a token it can name, and revocation is added precisely when it is
urgently needed. Tokens minted without these claims would be permanently
unrevocable. Two unused claims cost nothing now and cannot be retrofitted.

When revocation arrives it bumps a per-user or per-org **epoch** rather than
listing individual tokens: one entry kills every outstanding session for that
subject, which is what "deprovision this employee" actually requires. The epoch
map is small enough to cache locally for a minute, so revocation does not
reintroduce the hot-path dependency this ADR exists to avoid.

**What the gateway trusts.** Attributes are bound to the token when it is minted
and stamped onto spans **from the token**, never from what the client sends.
This is the same rule the ingest gateway already applies to tenancy (ADR-003,
and the `?tenant=` parameter removed rather than deprecated in the query API):
a value the caller can choose is not an identity.

**Scope.** Minting sessions is its own scope, distinct from `ingest`. A key that
can mint for any user is more powerful than one that can write spans, and
reusing the ingest scope would turn a leaked server-side key into a session
factory. `aud` prevents an ingest session token from being accepted by the query
API — without it, a token that can write telemetry could also read the org's
data by being pointed at the wrong host.

**This does not replace long-lived keys.** Both paths are permanent:

| Deployment | Credential |
| --- | --- |
| HTTP transport, on the customer's own infrastructure | Long-lived ingest key — it never leaves their servers |
| **stdio, on an end user's machine** | **Session token** |
| Local development | Direct key, documented as development-only |

Requiring an HTTP endpoint from customers who do not need one would be a worse
trade for them than the problem it solves.

**Considered and rejected.** The customer could run their own Collector and
forward with their key, which solves the same problem and needs no new protocol
from us. Rejected because it asks them to operate a piece of infrastructure
rather than write an HTTP handler — but it remains the right answer for a
customer who already runs one.

**Known consequences.**

* Telemetry must degrade, never block: a server whose session endpoint is
  unreachable starts **without** telemetry and retries. Our outage must not
  become their product's outage.
* Refresh happens at ~75% of TTL **with jitter**. Ten thousand laptops opened at
  09:00 would otherwise refresh together at 12:00 and flood the customer's own
  authentication service — a thundering herd we would have caused.
* Spans emitted before the first token must be buffered, with a hard bound. An
  unbounded buffer waiting on an endpoint that never answers is a memory leak
  inside the customer's process.
* That buffer inherits the stdio teardown problem: a client-launched server is
  killed when the client is finished with it, so anything held for credentials
  is lost unless it flushes on the same path (see the SIGTERM handler, currently
  unexercised on POSIX).
* Rotating credentials is not a configuration change. `OTLPSpanExporter` takes
  headers at construction, so the SDK must wrap its session rather than pass a
  header once.
* `user_id` and similar attributes are personal data and unbounded cardinality.
  They are stored and exact-match filterable, deliberately **not** offered as
  dropdown values, and they make a documented archive retention and erasure
  policy a prerequisite rather than a nicety.

---

## 8. Failure modes

| Failure | Blast radius | Behaviour | Recovery |
| --- | --- | --- | --- |
| ClickHouse down | Query and UI only | Ingest unaffected; normalizer lag grows; data safe in Kafka | Consumers drain the backlog on recovery; sized by retention |
| ClickHouse slow (merge pressure) | Query latency, ingest freshness | Normalizer backs off, lag grows | Autoscale consumers; throttle inserts; alert on lag |
| Kafka partition unavailable | Ingest for affected tenants | Producers retry; 503 returned if sustained | `min.insync.replicas=2` tolerates one broker loss |
| Kafka cluster down | Total ingest outage | 503 to all customers; their exporters buffer and retry | The one true single point of failure — hence managed, RF=3, multi-AZ |
| Normalizer bug writing bad rows | Derived data quality | Raw attributes retained; normalization_version identifies affected rows | Deploy fix, replay from offset ([§5.4](#54-replay-path)) |
| Normalizer crash-loop | Ingest freshness | Lag grows; no data loss | Offsets uncommitted, so replay is automatic and idempotent |
| Poison message | One partition stalls | Retry N times, then route to DLQ and advance | Triage the DLQ; never block a partition indefinitely |
| Whale tenant floods ingest | Partition skew, other tenants' lag | Quota check at the edge; salted partitioning spreads load | Raise the salt factor; hard-quota the tenant |
| Collector fleet saturated | Ingest latency, then 503s | Autoscale on CPU and export queue depth | Stateless tier — scaling is trivial |
| Postgres (control plane) down | New key auth, settings changes | Redis-cached keys keep ingest working | Cache TTL sets the survival window — set it deliberately |

**The point of the table.** Nine of ten rows are degradation, not loss. Only a total Kafka cluster outage stops ingest, and that failure is visible to the customer as a retryable error rather than silent loss.

---

## 9. Scaling model and capacity

### 9.1 What to scale on

| Signal | Scale | Alert when |
| --- | --- | --- |
| Collector CPU, export queue depth | Collector replicas | Queue depth sustained above zero |
| Kafka consumer lag (normalizer group) | Normalizer replicas, up to partition count | Lag exceeds 5 minutes of production |
| Partition skew across a topic | Salt factor for whale tenants | One partition exceeds 2× the median |
| ClickHouse insert latency, parts count | ClickHouse nodes; batch tuning | Insert p99 above 1s; parts growing monotonically |
| Query p95 by endpoint | Query API replicas; rollup coverage | Above the V2 SLO |
| End-to-end freshness (event time → queryable) | Whole pipeline | Above 60s |

**Freshness is the headline metric.** It is the one number that captures whether the whole pipeline is healthy, and it is what customers actually perceive.

### 9.2 Partition sizing

Partition count caps normalizer parallelism — a consumer group cannot exceed one consumer per partition. Start at 24 for `otlp.spans.raw`: enough headroom for early growth, small enough for fast rebalances. Increasing partitions later is easy; decreasing is not, so do not over-provision into thousands.

### 9.3 Rough capacity math

| Metric | Assumption | Result |
| --- | --- | --- |
| Compressed span size | ~1KB with zstd | — |
| Sustained ingest | 50k spans/s | ~50MB/s, ~4.3TB/day |
| Kafka hot retention | 72h × RF=3 | ~13TB pre-tiering |
| ClickHouse raw, 30d | 4.3TB/day with column compression | order of 30–50TB |
| Normalizer throughput | ~5k spans/s per replica | ~10 replicas at 50k/s |

These are planning figures for sizing conversations, not measurements. Replace each one with an observed value as soon as real telemetry exists — most will be wrong, and the cost model depends on them.

---

## 10. Environment ladder

The same architecture at three scales. What changes is managed-versus-container and node count, never the shape.

| | Local (Day 1) | Staging | Production |
| --- | --- | --- | --- |
| Collector | 1 container | 2 replicas | Autoscaled fleet |
| Kafka | 1 node, KRaft, RF=1 | 3 nodes, RF=3 | Managed, RF=3, multi-AZ |
| Normalizer | 1 process | 2 replicas | Autoscaled to partition count |
| ClickHouse | 1 node, `ReplicatedMergeTree` + embedded Keeper | 1 node, `ReplicatedMergeTree` | Cluster, `ReplicatedMergeTree` |
| Archiver / billing | Archiver on (MinIO) | S3 sink only | All consumer groups |
| Postgres | 1 container | 1 node | Managed, HA |
| Redis | 1 container (quota counters) | 1 node | Managed, HA |
| Auth | API keys + quotas | API keys + quotas | API keys + quotas |
| Ingest gateway | 1 container | 2 replicas | Autoscaled fleet |

> **The local rung runs the real engines, deliberately.** ClickHouse runs `ReplicatedMergeTree` with embedded Keeper, so `insert_deduplication_token` is exercised locally rather than deferred to staging; MinIO speaks the real S3 API; Redis holds the real quota counters; auth and tenancy are on. An earlier version of this note said idempotency "must be tested in staging, not on a laptop" — removing that deferral found two real defects within an hour (see `docs/deferred.md`), which is why the ladder now differs in SCALE rather than in kind.
>
> What is still genuinely different: one Kafka broker at RF=1, so `acks=all` under broker loss is untested (DF-2).

---

## 11. Deliberately not in this architecture

| Not building | Why |
| --- | --- |
| A stream processor (Flink, Kafka Streams) | [ADR-005](#adr-005-trace-assembly-in-clickhouse-not-a-stream-processor). Stateful streaming is the classic small-team quarter-killer. |
| Exactly-once semantics | [ADR-006](#adr-006-at-least-once-delivery-with-idempotent-writes-not-exactly-once). Idempotent writes achieve the outcome for a fraction of the complexity. |
| A custom ingest service | [ADR-003](#adr-003-the-collector-writes-to-kafka-directly). Do not hand-write the most availability-critical tier. |
| Kafka as an internal message bus | [ADR-010](#adr-010-kafka-is-not-a-general-purpose-internal-bus). |
| A metrics or logs pipeline | Traces first. Metrics are derivable from rollups; logs are a separate product decision. |
| Client-side ingestion | Product boundary, not a limitation. V2 §2.2. |
| Cross-region federated query | [ADR-009](#adr-009-one-independent-cell-per-region). After v1. |
| Per-tenant Kafka topics or clusters | Does not scale; partition count is a cluster-wide resource. |

---

## 12. Open questions

| Question | Blocks | Owner / when |
| --- | --- | --- |
| Does the Collector's auth extension model support our tenant resolution, or do we need a custom extension? | [ADR-003](#adr-003-the-collector-writes-to-kafka-directly) fully landing | Phase 1 spike |
| Which managed Kafka — MSK, Confluent Cloud, or Aiven? | Cost model, region roadmap | Depends on cloud commit; decide before Phase 2 |
| Is tiered storage GA on the version our provider offers? | [ADR-007](#adr-007-raw-topic-retention-is-the-replay-window) retention economics | Verify during provider selection |
| Salt factor and skew threshold for whale tenants | [ADR-004](#adr-004-partition-by-tenant-never-require-trace-locality) operability | After first real traffic |
| Do we stitch MRTR round-trips into one logical tool call? | Latency semantics of every chart in the product | Day 2 — see Day-1 doc §14 |
| How is `subscriptions/listen` represented, given it is a long-lived stream? | Latency percentiles | Day 2 |
| Trace-locator table shape for fast trace-by-id lookup | Query API, Day 3 | Day 3 |
| S3 sink connector licence (Confluent community vs Aiven Apache 2.0) | Consistency with the ADR-002 licensing argument | Before Phase 2 |

---

## 13. References

1. Model Context Protocol specification, revision 2026-07-28 — <https://modelcontextprotocol.io/specification/2026-07-28>
2. MCP 2026-07-28 key changes — <https://modelcontextprotocol.io/specification/2026-07-28/changelog>
3. OpenTelemetry Collector, gateway deployment pattern — <https://opentelemetry.io/docs/collector/deployment/gateway/>
4. OpenTelemetry Collector contrib, Kafka exporter — <https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/kafkaexporter>
5. OpenTelemetry semantic conventions for MCP spans — <https://github.com/open-telemetry/semantic-conventions-genai/blob/main/model/mcp/spans.yaml>
6. Apache Kafka documentation — <https://kafka.apache.org/documentation/>
7. Kafka KIP-405, tiered storage — <https://cwiki.apache.org/confluence/display/KAFKA/KIP-405%3A+Kafka+Tiered+Storage>
8. ClickHouse observability guidance — <https://clickhouse.com/docs/en/observability>
9. ClickHouse insert deduplication — <https://clickhouse.com/docs/en/engines/table-engines/mergetree-family/replication>
10. `MCP_Observability_Product_and_System_Design_Python_V2.docx`, v0.2 — parent product and system design.
11. `Day_01_Engineering_Doc_MCP_Observability.md` — Day 1 execution plan, aligned to this architecture.

---

*v1.0 · 14 Aug 2026 · System of record · Internal*
