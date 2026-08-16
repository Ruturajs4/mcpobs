# Alpha readiness

**Reviewed:** 2026-08-16  
**Candidate:** branch `day5-remaining-deferrals`, including the uncommitted filter and pagination work  
**Verdict:** not ready for a customer-facing alpha

The project is a credible Phase-0 engineering prototype. It is suitable for an
internal demonstration with synthetic data on a trusted development machine.
It is not yet suitable for external users or customer telemetry.

## Readiness by audience

| Audience | Status | Conditions |
| --- | --- | --- |
| Local developer demo | Ready | Use synthetic data and the documented Docker Compose stack. |
| Internal technical preview | Conditional | Fix the current filter and pagination P1 defects first. |
| External design-partner alpha | Not ready | Requires a secure deployment, recovery plan, green acceptance suite, and stable user-facing flows. |
| Production | Not assessed as ready | The repository explicitly remains a Phase-0 vertical slice. |

## What is already strong

- The end-to-end telemetry path works: OTel -> authenticated ingest -> Collector
  -> Kafka -> normalizer -> ClickHouse -> query console.
- Tenant identity comes from the authenticated API key and caller-supplied
  tenant attributes are overwritten.
- Ingest, read, and admin scopes are separate.
- API key secrets are stored as hashes and are shown only when issued.
- Replay, dead-letter handling, trace assembly, payload redaction, quotas, and
  archive decoding have live acceptance coverage.
- The earlier ClickHouse concurrent-session failure appears fixed: 30
  simultaneous valid API requests completed with HTTP 200 during this review.
- The static quality gate is green: 279 unit tests pass, Ruff passes, and mypy
  passes.

## Release blockers

### P0: deployment and customer-data safety

#### A1. No secure alpha deployment configuration

The checked-in `docker-compose.yml` is intentionally a local environment. It
publishes ClickHouse, Kafka, Postgres, Redis, MinIO, ingest, and query ports to
the host and uses local/default credentials. There is no deployable TLS ingress,
private production network, secret injection, or credential-rotation setup.

This is acceptable for local development. It must not be used as the external
alpha deployment.

**Exit criteria**

- Provide staging/alpha manifests or IaC separate from local Compose.
- Expose only the intended edge endpoints.
- Keep databases, Kafka, Redis, and object storage on private networks.
- Terminate TLS before ingest and query traffic.
- Inject secrets from a managed secret store and document rotation.
- Configure authenticated and encrypted connections between services.

#### A2. No tested recovery posture

The local stack has one Kafka broker with replication factor 1 and single local
volumes for ClickHouse, Postgres, and MinIO. The repository does not contain a
tested backup/restore procedure, declared RPO/RTO, or disaster-recovery
exercise. `DF-2` already states that multi-broker Kafka is required before any
customer data.

**Exit criteria**

- Run Kafka with replication factor 3 and an appropriate minimum ISR in the
  alpha environment.
- Configure managed or replicated persistence for Postgres, ClickHouse, and
  object storage.
- Define RPO and RTO.
- Document and execute backup and restore procedures.
- Demonstrate recovery from a failed broker and a lost database instance.

### P1: correctness and release confidence

#### A3. Drill-through filters can be lost

The old filter state and the API-described filter state coexist in
`query/static/app.js`. Navigation writes drill-through values such as `tool`
and `server` into the old state, while the live generic request path reads the
URL. A click from Servers or Capabilities can therefore open an unfiltered trace
list.

**Exit criteria**

- Remove the superseded filter implementation and its CSS.
- Make the URL the only browser filter state.
- Add browser tests for Server -> traces and Capability -> traces.

#### A4. Pagination can omit traces

Trace pagination orders and seeks using only `start_time`. If more rows share a
timestamp than fit on one page, rows after the boundary are skipped because the
next page uses `start_time < cursor`.

**Exit criteria**

- Use a total order such as `(start_time, trace_id)`.
- Encode both fields in the cursor.
- Use the same pair in `ORDER BY` and the seek predicate.
- Test at least 81 traces with the same timestamp.

#### A5. Browser state and requests can race

Changing the time window does not reset pagination cursors or invalidate the
filter catalog. Multiple asynchronous renders are also unversioned, so an older
response can overwrite a newer search or navigation result.

**Exit criteria**

- Reset cursors and reload filter configuration when the window changes.
- Add a render generation token or abort stale requests.
- Guard trace-drawer requests against close and rapid trace changes.
- Test out-of-order responses and view/window transitions.

#### A6. Malformed inputs return HTTP 500

The review reproduced both of these responses:

```text
GET /api/v1/traces?min_duration_ms=not-a-number -> 500
GET /api/v1/traces?cursor=definitely-not-a-cursor -> 500
```

Filter parsing raises `ValueError`, and cursor decoding allows base64 and date
exceptions to escape the endpoint.

**Exit criteria**

- Translate invalid filter and cursor input to HTTP 400 or 422.
- Return a stable error body without a server traceback.
- Add authenticated endpoint tests for malformed values and bounds.

#### A7. The live acceptance suite is not green

The reviewed run completed with:

```text
75 passed, 1 warning, 1 failed
```

`H2` failed after the quota test. The quota override was restored in Postgres,
but the ingest process retained the five-span limit in its 30-second principal
cache. The following long progress scenario was throttled with HTTP 429, so its
progress spans did not arrive.

**Exit criteria**

- Make quota restoration converge before the progress scenario, or run the
  scenario before mutating the quota.
- Ensure a verification run cannot leave the local environment throttled.
- Require the complete acceptance suite to pass in the alpha environment.

#### A8. CI is weaker than the local quality gate

The Makefile checks all production packages. GitHub Actions currently lints
only `normalizer`, tests, scripts, and the demo server, and type-checks only
`normalizer`. A query, control, ingest, or archiver error can therefore pass CI
despite failing `make check` locally.

**Exit criteria**

- Make CI invoke the same package lists as `make check`.
- Add a JavaScript syntax/static check for the console.
- Add browser coverage for the filter, pagination, navigation, and drawer flows.

#### A9. Health does not mean readiness

The ingest `/health` endpoint returns OK without testing Postgres, Redis, or the
Collector. Several long-running services also lack Compose or deployment
health checks. An orchestrator can route traffic to a process that cannot
perform its job.

**Exit criteria**

- Separate liveness from readiness.
- Check required dependencies with bounded timeouts.
- Report Collector forwarding failures explicitly.
- Configure deployment health checks and route traffic only to ready replicas.

## Filter drawer assessment

The drawer is visually coherent, and it now moves off-screen when the user
changes views. Its implementation is not complete:

- The closed panel remains in the accessibility tree.
- It has no `inert` or `aria-hidden` state.
- Ten controls remained keyboard-tabbable after the panel visually closed in
  the reviewed browser session.
- The old and new filter implementations coexist in the same JavaScript file.
- The API advertises advanced conditions, but the generic panel cannot create
  or edit them.
- Lighthouse accessibility scored 96; contrast and table-header checks failed.

**Exit criteria**

- When closed, make the panel inert and hidden from assistive technology.
- Return focus to the Filters trigger.
- Support Escape and focus containment while open.
- Remove the old filter bar implementation.
- Either implement generic advanced-condition controls or remove the unused
  contract until they exist.

## Organization and multi-user readiness

The control plane has organizations, users, invites, projects, and project-bound
API keys. This is not yet complete multi-user product access:

- The console authenticates with API keys, not user sessions.
- There is no project-membership table.
- A user belongs directly to one organization.
- Stored `admin` and `member` roles are not enforced.
- There is no SSO, password login, recovery flow, or user-facing key management.

For an internal preview, operators can issue separate read keys to trusted
participants. If the alpha promises team accounts, project permissions, or
individual audit attribution, user sessions plus organization/project
membership and enforced RBAC are release requirements.

## Operational gaps accepted only for an internal preview

- No external alerting for Kafka lag, freshness, DLQ growth, archive failures,
  quota fail-open, or backup failure.
- No production runbook with ownership and escalation thresholds.
- The Kafka message key remains null, so tenant partition isolation is not
  implemented (`DF-19`).
- API keys are stored in browser `localStorage`; no CSP or complete browser
  security-header policy is configured.
- Docker application images run as root and use mutable base-image tags.
- The archive lacks a documented encryption, immutability, retention, and
  erasure policy.

## Alpha gate checklist

The external alpha is a GO only when every required item is checked.

### Product correctness

- [ ] One filter implementation and one URL-backed state model remain.
- [ ] Drill-through filtering works from Servers and Capabilities.
- [ ] Compound cursor pagination cannot omit equal-timestamp traces.
- [ ] Time-window changes reset pagination and filter options.
- [ ] Stale list and drawer responses cannot overwrite current state.
- [ ] Malformed filters and cursors return 400/422, never 500.
- [ ] The filter drawer is inert and inaccessible when closed.

### Verification

- [ ] Unit tests, Ruff, and mypy pass.
- [ ] GitHub Actions runs the same checks as `make check`.
- [ ] JavaScript and browser-flow checks pass.
- [ ] The complete live acceptance suite passes with no failures.
- [ ] Concurrent query/API smoke testing passes.

### Deployment and operations

- [ ] Alpha infrastructure is separate from local Docker Compose.
- [ ] TLS and private networking are enabled.
- [ ] Secrets are managed and rotatable.
- [ ] Kafka and persistent stores meet the accepted durability target.
- [ ] Backup and restore have been exercised.
- [ ] Liveness and dependency-aware readiness are configured.
- [ ] Alerts and an on-call/runbook owner exist for critical signals.

### Access and policy

- [ ] Decide whether alpha access is API-key-only or user-session based.
- [ ] If team access is promised, implement and enforce organization/project
  membership and roles.
- [ ] Define key expiry, revocation, and rotation expectations.
- [ ] Define telemetry retention, archive encryption, and deletion policy.

## Recommended implementation order

1. Remove duplicate filter code and stabilize browser state transitions.
2. Fix compound cursor pagination and input error contracts.
3. Make the acceptance suite deterministic and align CI with `make check`.
4. Add readiness checks and operational alerts.
5. Build the private TLS-enabled alpha environment and prove restore.
6. Complete user-session and membership support if team accounts are in scope.

## Reassessment evidence to collect

The next review should attach:

- The commit SHA being assessed.
- A clean working tree or an explicit candidate diff.
- Unit, lint, type-check, JavaScript, and browser-test output.
- Full live acceptance output.
- A same-timestamp pagination regression test.
- Malformed-input API tests proving 400/422 behavior.
- Alpha deployment diagrams/manifests with public endpoints identified.
- Backup and restore timestamps with measured RPO/RTO.
- Readiness and alert screenshots or exported configuration.

