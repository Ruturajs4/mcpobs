# Alpha readiness

**Reviewed:** 2026-08-16 (second review)
**Candidate:** `4a3af2b`, clean working tree
**Verdict:** not ready for an external alpha — **but the reason has changed**

The first review found blockers in two categories: product correctness, and
deployment/operations. **The product-correctness lane is now closed.** Every
P1 defect it listed has been fixed and verified against a running stack, and
the live acceptance suite is green and stable across consecutive runs.

What remains is almost entirely **environment and policy**. That is a different
kind of work — infrastructure, not features — and none of it depends on further
product engineering.

## Evidence for this review

| | |
| --- | --- |
| Unit tests | **319 passed** |
| Ruff / mypy | clean (34 source files) |
| Live acceptance | **82 passed, 0 warn, 0 failed**, stable over consecutive runs |
| Docs site | 22 pages, builds `--strict` |
| CI | runs the same package lists as `make check`, plus JS syntax and a strict docs build |

## Readiness by audience

| Audience | Status | Change since first review |
| --- | --- | --- |
| Local developer demo | Ready | Now exercises real Postgres, MySQL, Redis and HTTP dependencies |
| Internal technical preview | **Ready** | Was *conditional* on the filter and pagination P1 defects; those are fixed |
| External design-partner alpha | Not ready | Unchanged — blocked on deployment and recovery, not on the product |
| Production | Not assessed | Still a Phase-0 vertical slice |

## Closed since the first review

Each was verified against the running stack, not by inspection.

| Was | Now |
| --- | --- |
| **A3** Drill-through lost filters | Servers → Tools → Traces carries both chips and reflects them in the URL |
| **A4** Pagination could omit traces | Compound `(start_time, trace_id)` cursor. Forced a page boundary onto a real timestamp tie: the sibling is the first row of the next page. 61 pages, 0 duplicates, 0 gaps |
| **A5** Browser state/request races | Window change resets the cursor; abort controllers and generation tokens prevent stale responses overwriting current ones |
| **A6** Malformed input returned 500 | `min_duration_ms=abc` → **422**, bad cursor → **422**, stable error bodies |
| **A7** Acceptance suite not green | 82 passed, 0 failed. The quota-cache flake (J1d), a fixed-sleep race (H2) and the rollup reconciliation race are all closed |
| **A8** CI weaker than `make check` | Identical package lists, plus `node --check` on both consoles and `mkdocs build --strict` |
| **A9** Health ≠ readiness | `/ready` probes control plane, quota store and collector with bounded timeouts; a collector failure returns 503 rather than silently accepting |
| Filter drawer incomplete | Closed panel is `inert` + `aria-hidden` (0 reachable controls, was 10); focus returns to the trigger; Escape closes; advanced conditions work end to end; the duplicate filter implementation is gone |

### Also fixed, found during the second review

- **Pre-authentication information disclosure.** Both sign-in pages printed the
  exact key-issuing CLI invocation and its flags; `/openapi.json`, `/docs` and
  `/redoc` enumerated every route including the admin surface; and `/health`
  leaked the ClickHouse hostname and port in its failure body. Docs are now
  gated on `EXPOSE_API_DOCS` (**default off**), and the health body says
  "dependency unavailable" while the detail goes to the log.
- **Three definitions of "failure".** The overview excluded 401s and
  cancellations, `?status=error` counted 401s, and `/errors` counted both — the
  Errors page listed 62 traces the headline error rate said were not errors.
  One `NOT_A_FAILURE` constant now feeds all three (64 = 64 = 64).
- **Unversioned static assets were never revalidated**, so a deployed console
  fix could keep not reaching users. `Cache-Control: no-cache` with the existing
  ETag.
- **Capabilities was 2N+1 queries and unbounded.** Now three grouped queries
  regardless of row count, capped at 200 with the truncation disclosed.
- **The transport was unrecorded.** `network.transport` was empty on all stored
  spans, so stdio and streamable-HTTP were indistinguishable — hiding the
  transport most customers use. Now derived and asserted (B12, B12b), including
  on the cancellation path.
- **stdio had no transport-specific test.** On stdio, stdout *is* the JSON-RPC
  channel; a stray `print()` corrupted the stream while the demo still exited 0
  with every scenario passing. Now asserted on the bytes.

## Release blockers

### P0: deployment and customer-data safety

Both unchanged from the first review. They are the alpha gate.

#### A1. No secure alpha deployment configuration

`docker-compose.yml` remains a local environment: it publishes ClickHouse,
Kafka, Postgres, Redis, MinIO, ingest and query to the host and uses default
credentials. There is no deployable TLS ingress, private network, secret
injection or rotation. No IaC or manifests exist in the repository.

**Exit criteria**

- Staging/alpha manifests or IaC, separate from local Compose.
- Only intended edge endpoints exposed; databases, Kafka, Redis and object
  storage on private networks.
- TLS terminated before ingest and query traffic.
- Secrets from a managed store, with documented rotation.
- `/ready` and `/admin` reachable only from the internal network.

#### A2. No tested recovery posture

Single Kafka broker, `replication factor 1` (verified in `docker-compose.yml`),
single local volumes for ClickHouse, Postgres and MinIO. No backup/restore
procedure, no declared RPO/RTO, no disaster-recovery exercise. `DF-2` already
requires multi-broker Kafka before any customer data.

**Exit criteria**

- Kafka at replication factor 3 with an appropriate minimum ISR.
- Managed or replicated persistence for Postgres, ClickHouse and object storage.
- RPO and RTO defined.
- Backup and restore documented **and executed**, with measured timings.
- Recovery demonstrated from a failed broker and a lost database instance.

### P1: verification

#### A10. Automated browser tests — **CLOSED**

21 Playwright flows in `tests/test_browser_flows.py`, run in CI and by
`make browser`: sign-in, the trace list's column alignment, transport tags,
pagination (including that a filter change resets the cursor), the filter panel
(opens right, inert when closed, focus returns to the trigger, chips, the empty
state), the trace drawer, waterfall paging, drill-through, and that a same-view
refresh does not blank the pane.

Verified they catch regressions rather than assumed: reintroducing the
column-order bug fails `test_columns_and_cells_line_up` with
`assert 'ok' in ('stdio', 'http', ...)` — the exact symptom that shipped.

They skip when the stack is not running, and the skip names the reason.

#### A11. The stdio SIGTERM flush is unproven

On stdio the client tears the server down, and the export path buffers. The
`atexit` handler is redundant — `TracerProvider` defaults to
`shutdown_on_exit=True` and flushes on clean exit, which is what closing stdin
produces. The **SIGTERM** handler is the load-bearing one, because SIGTERM does
not run `atexit`.

That path is untestable on the review machine: on Windows, SIGTERM is
`TerminateProcess` and no Python handler runs. The code is present and reads
correctly; it has not been exercised.

**Exit criteria**

- Run the flush assertion on Linux, killing the server with SIGTERM, and
  confirm the final spans still arrive.

## Organisation and multi-user readiness

Unchanged. The control plane has organisations, users, invites, projects and
project-bound API keys, but:

- The console authenticates with API keys, not user sessions.
- There is no project-membership table; a user belongs directly to one org.
- Stored `admin` and `member` roles are not enforced.
- There is no SSO, password login, recovery flow or user-facing key management.

For an internal preview, operators can issue separate read keys to trusted
participants. If the alpha promises **team accounts, project permissions or
individual audit attribution**, user sessions plus membership and enforced RBAC
become release requirements.

## Operational gaps accepted only for an internal preview

- No external alerting for Kafka lag, freshness, DLQ growth, archive failures,
  quota fail-open or backup failure. No runbook with ownership and escalation.
- The Kafka message key remains null, so tenant partition isolation is not
  implemented (`DF-19` — confirmed unfixable in the current pipeline against two
  Collector versions).
- API keys are held in browser `localStorage`; no CSP or complete browser
  security-header policy.
- Docker application images run as root and use mutable base-image tags.
- The archive has no documented encryption, immutability, retention or erasure
  policy.
- Reads are not audited (writes are).

## Alpha gate checklist

### Product correctness

- [x] One filter implementation and one URL-backed state model
- [x] Drill-through filtering works from Servers and Capabilities
- [x] Compound cursor pagination cannot omit equal-timestamp traces
- [x] Time-window changes reset pagination and filter options
- [x] Stale list and drawer responses cannot overwrite current state
- [x] Malformed filters and cursors return 422, never 500
- [x] The filter drawer is inert and inaccessible when closed
- [x] One definition of failure across overview, filters and the error list
- [x] Both transports are recorded and distinguishable

### Verification

- [x] Unit tests, Ruff and mypy pass
- [x] GitHub Actions runs the same checks as `make check`
- [x] JavaScript syntax and strict docs build pass
- [x] The complete live acceptance suite passes with no failures
- [x] Concurrent query smoke testing passes
- [x] **Browser-flow tests exist and run in CI** (A10)
- [x] A trace read mid-flight is marked incomplete rather than presented as settled
- [ ] **The stdio SIGTERM flush is exercised on a POSIX host** (A11)

### Deployment and operations

- [ ] Alpha infrastructure separate from local Docker Compose
- [ ] TLS and private networking enabled
- [ ] Secrets managed and rotatable
- [ ] Kafka and persistent stores meet the accepted durability target
- [ ] Backup and restore exercised
- [x] Liveness and dependency-aware readiness configured
- [ ] Alerts and an on-call/runbook owner for critical signals

### Access and policy

- [ ] Decide whether alpha access is API-key-only or user-session based
- [ ] If team access is promised, implement and enforce org/project membership
- [ ] Define key expiry, revocation and rotation expectations
- [ ] Define telemetry retention, archive encryption and deletion policy

## Recommended order

1. Build the private, TLS-enabled alpha environment and prove restore (A1, A2).
2. Exercise the stdio SIGTERM flush on Linux (A11).
3. Add readiness alerts and a runbook owner.
4. Complete user-session and membership support **if** team accounts are in
   scope for the alpha.

## Note for the next reviewer

This document is **internal** and is excluded from the published documentation
site (`mkdocs.yml: exclude_docs`, enforced by `tests/test_docs.py`, which builds
the site and greps the output). It enumerates unpatched weaknesses; publishing
it would hand an attacker a prioritised checklist. If you rename this file,
update both.
