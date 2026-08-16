"""Cross-tenant reads, for operators only.

WHY THIS IS A SEPARATE FILE AND A SEPARATE CLASS
    `query/repository.py` opens with a rule: tenant scoping happens in that
    layer and nowhere else, so "an endpoint cannot forget to scope a query,
    because an endpoint never writes one". Every query in it takes a tenant.

    Adding cross-tenant methods there would have broken that sentence -- and a
    file where MOST queries are scoped is far more dangerous than one where none
    are, because the reader's eye stops checking. So the queries that
    deliberately span tenants live here, in a module whose name says so, and
    `SpanRepository` keeps its invariant exactly as written.

    Every method here is reachable only through the `admin` scope
    (`query/admin.py`). Assertion K2 fires a normal read key at these endpoints
    and checks it is refused.

WHAT AN OPERATOR ACTUALLY NEEDS
    Not "everything, cross-tenant". The three questions that get someone paged:

      * Which tenant is about to be throttled, or is being throttled now?
        (Architecture §8's whale, whose burst becomes everyone else's lag.)
      * Is the pipeline healthy -- freshness, dead letters, version drift?
      * Who is using this, and are their credentials in order?

    A dashboard that answered more than that would mostly be answering the
    customer's questions with the operator's key, which is what the customer
    console is for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from clickhouse_connect.driver.client import Client

from query.dtos import AdminOverview, PipelineHealth, TenantRow
from query.repository import FRESHNESS_WINDOW_MINUTES, _number


class AdminRepository:
    """Reads that span every tenant. Requires the `admin` scope."""

    def __init__(self, client: Client, control: Any) -> None:
        self.client = client
        self.control = control

    def _rows(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return list(self.client.query(sql, parameters=params or {}).result_rows)

    # -- tenants -----------------------------------------------------------
    def tenants(self, window_minutes: int = 1440) -> list[TenantRow]:
        """One row per tenant, joining telemetry to the control plane.

        The two halves come from different databases on purpose -- identity is
        Postgres, volume is ClickHouse -- so this joins them in Python rather
        than pretending one can see the other. A tenant present in only one side
        is exactly the sort of thing an operator needs to see: an org with no
        telemetry has not onboarded, and telemetry with no org should be
        impossible now that the gateway authenticates.
        """
        volume = {
            row[0]: row
            for row in self._rows(
                """SELECT tenant_id,
                          count(),
                          countIf(mcp_is_error = 1),
                          uniqExact(service_name),
                          max(timestamp),
                          countIf(resource_attributes['mcpobs.quota.soft_exceeded'] = 'true')
                   FROM spans_raw
                   WHERE timestamp >= now() - INTERVAL {window:UInt32} MINUTE
                   GROUP BY tenant_id""",
                {"window": window_minutes},
            )
        }

        orgs = self.control._rows(
            """SELECT o.slug, o.name, o.plan, o.created_at,
                      o.quota_spans_per_minute, o.quota_spans_per_day,
                      (SELECT count(*) FROM projects p WHERE p.org_id = o.id) AS projects,
                      (SELECT count(*) FROM api_keys k
                         WHERE k.org_id = o.id AND k.revoked_at IS NULL) AS active_keys,
                      (SELECT count(*) FROM users u WHERE u.org_id = o.id) AS users,
                      (SELECT count(*) FROM invites i
                         WHERE i.org_id = o.id AND i.accepted_at IS NULL
                           AND i.expires_at > now()) AS open_invites
               FROM orgs o ORDER BY o.slug"""
        )

        from control.quota import QuotaEnforcer

        rows: list[TenantRow] = []
        seen: set[str] = set()
        for org in orgs:
            slug = org["slug"]
            seen.add(slug)
            plan = QuotaEnforcer.plan_for(org["plan"])
            counts = volume.get(slug)
            rows.append(
                TenantRow(
                    tenant=slug,
                    name=org["name"] or slug,
                    plan=plan.name,
                    projects=int(org["projects"]),
                    users=int(org["users"]),
                    active_keys=int(org["active_keys"]),
                    open_invites=int(org["open_invites"]),
                    spans=int(counts[1]) if counts else 0,
                    errors=int(counts[2]) if counts else 0,
                    servers=int(counts[3]) if counts else 0,
                    last_seen=counts[4] if counts else None,
                    soft_quota_spans=int(counts[5]) if counts else 0,
                    limit_minute=(
                        org["quota_spans_per_minute"]
                        if org["quota_spans_per_minute"] is not None
                        else plan.spans_per_minute
                    ),
                    limit_day=(
                        org["quota_spans_per_day"]
                        if org["quota_spans_per_day"] is not None
                        else plan.spans_per_day
                    ),
                    limit_overridden=org["quota_spans_per_minute"] is not None
                    or org["quota_spans_per_day"] is not None,
                    onboarded=bool(counts),
                )
            )

        # Telemetry under a tenant with no org row. Should be impossible since
        # the gateway resolves tenancy from an authenticated key -- which is
        # precisely why it is surfaced rather than filtered out. If it ever
        # appears, either a key outlived its org or something bypassed the
        # gateway, and both are worth a page.
        for slug, counts in volume.items():
            if slug in seen:
                continue
            rows.append(
                TenantRow(
                    tenant=slug, name=slug, plan="?", orphaned=True,
                    spans=int(counts[1]), errors=int(counts[2]),
                    servers=int(counts[3]), last_seen=counts[4],
                    onboarded=True,
                )
            )

        rows.sort(key=lambda r: r.spans, reverse=True)
        return rows

    # -- pipeline ----------------------------------------------------------
    def pipeline(self) -> PipelineHealth:
        """Is the pipeline healthy right now?

        Freshness uses the same FIXED recent window the customer console does
        (D46): a wide range sweeps in replayed spans, whose event time precedes
        their ingest time by hours by design, and reports that as latency.
        """
        fresh = self._rows(
            f"""SELECT quantile(0.50)(dateDiff('millisecond', timestamp, ingested_at)),
                       quantile(0.95)(dateDiff('millisecond', timestamp, ingested_at)),
                       count()
                FROM spans_raw
                WHERE timestamp >= now() - INTERVAL {FRESHNESS_WINDOW_MINUTES} MINUTE"""
        )
        p50, p95, recent = fresh[0] if fresh else (0, 0, 0)

        dead = self._rows(
            """SELECT reason, count() FROM ingest_dead_letter
               WHERE received_at >= now() - INTERVAL 24 HOUR
               GROUP BY reason ORDER BY 2 DESC"""
        )

        # Several live normalization versions means a deploy is mid-rollout or a
        # replay is in flight. Neither is wrong; both are worth knowing before
        # trusting an aggregate, because argMax resolution is what hides the
        # difference (D24).
        versions = self._rows(
            """SELECT normalization_version, count() FROM spans_raw
               WHERE timestamp >= now() - INTERVAL 60 MINUTE
               GROUP BY 1 ORDER BY 1"""
        )

        return PipelineHealth(
            freshness_p50_seconds=round(_number(p50) / 1000, 2),
            freshness_p95_seconds=round(_number(p95) / 1000, 2),
            spans_recent=int(_number(recent)),
            dead_letters_24h=sum(int(row[1]) for row in dead),
            dead_letter_reasons={str(row[0]): int(row[1]) for row in dead},
            normalization_versions={str(row[0]): int(row[1]) for row in versions},
        )

    def overview(self, window_minutes: int = 1440) -> AdminOverview:
        tenants = self.tenants(window_minutes)
        return AdminOverview(
            window_minutes=window_minutes,
            tenants=tenants,
            pipeline=self.pipeline(),
            total_spans=sum(t.spans for t in tenants),
            total_errors=sum(t.errors for t in tenants),
            orphaned=sum(1 for t in tenants if t.orphaned),
            never_onboarded=sum(1 for t in tenants if not t.onboarded),
        )

    # -- keys --------------------------------------------------------------
    def keys(self) -> list[dict[str, Any]]:
        """Every key, with its org and last use. Never the secret -- only a hash
        is stored, so there is nothing here to leak even to an operator."""
        return [
            dict(row)
            for row in self.control._rows(
                """SELECT k.prefix, k.name, k.scopes, k.created_at, k.last_used_at,
                          k.revoked_at, k.expires_at,
                          o.slug AS tenant, p.slug AS project
                   FROM api_keys k
                   JOIN orgs o ON o.id = k.org_id
                   JOIN projects p ON p.id = k.project_id
                   ORDER BY k.revoked_at NULLS FIRST, k.created_at DESC
                   LIMIT 200"""
            )
        ]

    def invites(self) -> list[dict[str, Any]]:
        """Outstanding invites. Invite-only means these ARE the signup queue."""
        return [
            dict(row)
            for row in self.control._rows(
                """SELECT i.email, i.role, i.created_at, i.expires_at,
                          i.accepted_at, o.slug AS tenant
                   FROM invites i JOIN orgs o ON o.id = i.org_id
                   ORDER BY i.accepted_at NULLS FIRST, i.created_at DESC
                   LIMIT 200"""
            )
        ]


def as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None
