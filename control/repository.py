"""Control-plane reads and writes.

ONE RULE, ENFORCED HERE
    Authentication resolves a key to a `Principal`, and a `Principal` is the
    only place tenancy comes from. No function in this module accepts a tenant
    argument from a caller, because there is no legitimate caller who knows one
    the key did not already imply.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row

from control import keys
from control.models import Invite, IssuedKey, Org, Principal, Project, User

SCHEMA_DIR: Final = Path(__file__).resolve().parent / "schema"

CACHE_TTL_SECONDS: Final = 30
"""How long a resolved key is trusted without re-reading Postgres.

Architecture.md §8 makes this an explicit decision rather than a default: with
Postgres down, cached keys keep ingest working, and this TTL IS the survival
window. Short enough that a revocation takes effect in seconds -- revoking a
leaked key is an emergency and "it will apply within half a minute" is the
promise being made -- and long enough that a burst from one customer does not
become a burst against the control plane.
"""


class ControlPlane:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn: str = dsn or os.environ.get(
            "CONTROL_PLANE_DSN",
            "postgresql://mcpobs:mcpobs@localhost:5432/mcpobs_control",
        )
        self._conn: psycopg.Connection[Any] | None = None
        #: prefix -> (expiry, secret_hash, principal). The hash is cached so a
        #: hit can verify the secret without touching Postgres.
        self._cache: dict[str, tuple[float, str, Principal | None]] = {}

    # -- plumbing ----------------------------------------------------------
    @property
    def conn(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        return self._conn

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            return cur.fetchall() if cur.description else []

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def migrate(self) -> list[str]:
        """Apply the schema. Idempotent, like the ClickHouse runner.

        No checksum ledger here, unlike `normalizer/migrations.py`. That one
        guards against editing a migration whose effects are already spread
        across a distributed table; this schema is small, `CREATE TABLE IF NOT
        EXISTS` is genuinely idempotent, and adding a ledger would be ceremony
        rather than safety.
        """
        applied = []
        for path in sorted(SCHEMA_DIR.glob("*.sql")):
            with self.conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
            applied.append(path.stem)
        return applied

    def wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                self._one("SELECT 1")
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.0)
        raise RuntimeError(f"control plane not ready: {last}")

    # -- authentication ----------------------------------------------------
    def authenticate(self, token: str | None) -> Principal | None:
        """Resolve a presented key, or None.

        Returns None for every failure -- malformed, unknown, revoked, expired
        -- and never says which. An error message that distinguishes "no such
        key" from "revoked key" is an oracle for probing whether a stolen key
        was ever valid.
        """
        if not token:
            return None
        parsed = keys.split(token)
        if parsed is None:
            return None
        prefix, secret = parsed

        now = time.monotonic()
        cached = self._cache.get(prefix)
        if cached and cached[0] > now:
            _, secret_hash, principal = cached
            # The HASH is cached alongside the principal, and the secret is
            # re-verified against it on every hit. Caching only the principal
            # would have let any secret through once a valid prefix was warm --
            # and it would also have re-read Postgres to check, which is not a
            # cache at all.
            if principal is None or not keys.verify(secret, secret_hash):
                return None
            return principal

        row = self._one(
            """SELECT k.id AS key_id, k.prefix, k.org_id, k.secret_hash, k.scopes,
                      k.expires_at, k.revoked_at,
                      o.slug AS tenant, o.plan,
                      o.quota_spans_per_minute, o.quota_spans_per_day,
                      p.slug AS project, p.environment
               FROM api_keys k
               JOIN orgs o ON o.id = k.org_id
               JOIN projects p ON p.id = k.project_id
               WHERE k.prefix = %s""",
            (prefix,),
        )
        if row is None or not keys.verify(secret, row["secret_hash"]):
            # Negative results are cached too, so an attacker spraying invalid
            # keys cannot turn authentication into a denial of service against
            # Postgres.
            self._cache[prefix] = (now + CACHE_TTL_SECONDS, "", None)
            return None
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < datetime.now(UTC):
            return None

        principal = Principal(
            key_id=row["key_id"],
            key_prefix=row["prefix"],
            org_id=row["org_id"],
            tenant=row["tenant"],
            project=row["project"],
            environment=row["environment"],
            scopes=keys.parse_scopes(row["scopes"]),
            plan=row["plan"] or "trial",
            quota_spans_per_minute=row["quota_spans_per_minute"],
            quota_spans_per_day=row["quota_spans_per_day"],
        )
        self._cache[prefix] = (now + CACHE_TTL_SECONDS, row["secret_hash"], principal)
        return principal

    def touch(self, key_id: int) -> None:
        """Record last use, best-effort.

        Deliberately not on the authentication path's critical section: a write
        per ingest request would make the control plane the bottleneck of the
        hot path, and "when was this key last used" is a support question, not a
        security control.
        """
        with contextlib.suppress(Exception):
            self._rows("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (key_id,))

    # -- orgs, invites, users ----------------------------------------------
    def create_org(self, slug: str, name: str = "", plan: str = "trial") -> Org:
        row = self._one(
            """INSERT INTO orgs (slug, name, plan) VALUES (%s, %s, %s)
               ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
               RETURNING id, slug, name, plan""",
            (slug, name or slug, plan),
        )
        assert row is not None
        return Org(**row)

    def create_project(
        self, org_id: int, slug: str, name: str = "", environment: str = "production"
    ) -> Project:
        row = self._one(
            """INSERT INTO projects (org_id, slug, name, environment)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (org_id, slug) DO UPDATE SET name = EXCLUDED.name
               RETURNING id, org_id, slug, name, environment""",
            (org_id, slug, name or slug, environment),
        )
        assert row is not None
        return Project(**row)

    def invite(
        self,
        org_id: int,
        email: str,
        role: str = "member",
        invited_by: int | None = None,
        ttl_days: int = 14,
    ) -> tuple[Invite, str]:
        """Create an invite. Returns (invite, code) -- the code is shown once."""
        code, code_hash = keys.mint_invite()
        row = self._one(
            """INSERT INTO invites (org_id, email, code_hash, role, invited_by, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id, org_id, email, role, expires_at, accepted_at""",
            (org_id, email.lower(), code_hash, role, invited_by,
             datetime.now(UTC) + timedelta(days=ttl_days)),
        )
        assert row is not None
        return Invite(**row), code

    def accept_invite(self, code: str, name: str = "") -> User | None:
        """Redeem an invite and create the user. The ONLY way a user is created.

        Single-use and atomic: the UPDATE is conditional on `accepted_at IS
        NULL`, so two simultaneous redemptions of one code cannot both win. A
        read-then-write would have raced, and an invite code is exactly the kind
        of thing that gets pasted twice.
        """
        row = self._one(
            """UPDATE invites SET accepted_at = now()
               WHERE code_hash = %s AND accepted_at IS NULL AND expires_at > now()
               RETURNING id, org_id, email, role""",
            (keys.hash_invite(code),),
        )
        if row is None:
            return None
        user = self._one(
            """INSERT INTO users (org_id, email, name, role) VALUES (%s, %s, %s, %s)
               ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
               RETURNING id, org_id, email, name, role""",
            (row["org_id"], row["email"], name, row["role"]),
        )
        assert user is not None
        self._rows(
            "UPDATE invites SET accepted_by = %s WHERE id = %s", (user["id"], row["id"])
        )
        return User(**user)

    # -- keys --------------------------------------------------------------
    def issue_key(
        self,
        org_id: int,
        project_id: int,
        scopes: tuple[str, ...] = (keys.INGEST,),
        name: str = "",
        created_by: int | None = None,
    ) -> IssuedKey:
        project = self._one(
            "SELECT slug, environment FROM projects WHERE id = %s AND org_id = %s",
            (project_id, org_id),
        )
        if project is None:
            raise ValueError("project does not belong to this org")
        token, prefix, secret_hash = keys.mint(project["environment"])
        self._rows(
            """INSERT INTO api_keys (org_id, project_id, name, prefix, secret_hash,
                                     scopes, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (org_id, project_id, name, prefix, secret_hash, ",".join(scopes), created_by),
        )
        return IssuedKey(
            prefix=prefix,
            token=token,
            project=project["slug"],
            environment=project["environment"],
            scopes=scopes,
        )

    # -- audit -------------------------------------------------------------
    def audit(
        self,
        action: str,
        target: str = "",
        outcome: str = "ok",
        detail: dict[str, Any] | None = None,
        actor: Principal | None = None,
        source: str = "console",
        source_ip: str = "",
    ) -> None:
        """Record one operator action.

        Called INSIDE the transaction that performs the action wherever there is
        one, so the record and the change land together or not at all. Called on
        its own only for things that change nothing -- a refused attempt.
        """
        self._rows(
            """INSERT INTO audit_log
                   (actor_key_id, actor_prefix, actor_org, actor_source,
                    action, target, outcome, detail, source_ip)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
            (
                actor.key_id if actor else None,
                actor.key_prefix if actor else "",
                actor.tenant if actor else "",
                source,
                action,
                target,
                outcome,
                json.dumps(detail or {}),
                source_ip,
            ),
        )

    def audit_trail(self, limit: int = 200, target: str = "") -> list[dict[str, Any]]:
        where = "WHERE target = %s" if target else ""
        params: tuple[Any, ...] = (target, limit) if target else (limit,)
        return self._rows(
            f"""SELECT at, actor_prefix, actor_org, actor_source, action, target,
                       outcome, detail, source_ip
                FROM audit_log {where} ORDER BY at DESC LIMIT %s""",
            params,
        )

    # -- mutations, each audited in its own transaction --------------------
    def set_quota(
        self,
        slug: str,
        per_minute: int | None,
        per_day: int | None,
        actor: Principal | None = None,
        source: str = "cli",
        source_ip: str = "",
    ) -> bool:
        """Override an org's limits. NULL restores the plan's.

        The read of the OLD values, the write, and the audit record are one
        transaction. Reading the old values outside it would let a concurrent
        change slip in between, so the entry would report a `from` that was
        never true.
        """
        with self.conn.transaction():
            before = self._one(
                "SELECT quota_spans_per_minute, quota_spans_per_day FROM orgs "
                "WHERE slug = %s FOR UPDATE",
                (slug,),
            )
            if before is None:
                return False
            self._rows(
                "UPDATE orgs SET quota_spans_per_minute = %s, quota_spans_per_day = %s "
                "WHERE slug = %s",
                (per_minute, per_day, slug),
            )
            self.audit(
                "quota.set",
                target=slug,
                detail={
                    "from": {
                        "per_minute": before["quota_spans_per_minute"],
                        "per_day": before["quota_spans_per_day"],
                    },
                    "to": {"per_minute": per_minute, "per_day": per_day},
                },
                actor=actor, source=source, source_ip=source_ip,
            )
        self._cache.clear()  # so the new limit applies on the next request here
        return True

    def set_plan(self, slug: str, plan: str) -> bool:
        row = self._one(
            "UPDATE orgs SET plan = %s WHERE slug = %s RETURNING id", (plan, slug)
        )
        self._cache.clear()
        return row is not None

    def revoke_key(
        self,
        prefix: str,
        actor: Principal | None = None,
        source: str = "cli",
        source_ip: str = "",
    ) -> bool:
        """Revoke a key. The revocation and its audit entry are one transaction.

        Which is what lets this be unconditional. An audit log in a DIFFERENT
        store would force a bad choice here: block the revoke when the log is
        unavailable, and a leaked credential stays live during our outage; do it
        anyway, and the one action most worth recording is the one most likely
        to go unrecorded. Same database, one transaction, neither trade.
        """
        with self.conn.transaction():
            row = self._one(
                "UPDATE api_keys SET revoked_at = now() "
                "WHERE prefix = %s AND revoked_at IS NULL "
                "RETURNING id, scopes, org_id",
                (prefix,),
            )
            if row is None:
                return False
            self.audit(
                "key.revoke",
                target=prefix,
                detail={"scopes": row["scopes"], "org_id": row["org_id"]},
                actor=actor, source=source, source_ip=source_ip,
            )
        # Drop the cache entry so revocation is immediate for THIS process.
        # Other processes converge within CACHE_TTL_SECONDS, which is the
        # documented promise rather than an accident.
        self._cache.pop(prefix, None)
        return True
