"""The default `Authenticator` for a self-hosted, single-tenant install.

WHAT THIS IS FOR
    `docker-compose.lite.yml` with no `CONTROL_PLANE_DSN` configured, and no
    ECC package installed. There is exactly one tenant, it is not multi-user,
    and there is nothing to authenticate against -- so this does not try to:
    every token is accepted and mapped onto the same static `Principal`.

WHY THIS IS SAFE
    This is not a security boundary standing in for a real one -- it is the
    absence of one, by design, for a deployment that IS the tenant boundary
    (one process, one operator, no network-exposed admin surface). The moment
    that stops being true -- more than one customer, a hosted offering, an
    admin API reachable by someone who isn't the operator -- is the moment to
    install the real `ControlPlane` (ECC), not to harden this file. This file
    is deliberately kept trivial so nobody is tempted to do the latter.

WHY PLAN "local", NOT "oss" OR SOME NEW NAME
    `control/quota.py`'s `PLANS` dict already has a "local" entry
    (`spans_per_minute=0, spans_per_day=0`, i.e. unlimited) for exactly this
    principal. `QuotaEnforcer.check()` short-circuits to an unconditional
    allow for it without ever touching Redis -- meaning `QuotaEnforcer` needs
    no local/OSS variant of its own; it already degrades correctly as long as
    the `Principal` it is handed carries `plan="local"`.
"""

from __future__ import annotations

from control.keys import INGEST, READ, SESSION_MINT
from control.models import Principal

LOCAL_PRINCIPAL: Principal = Principal(
    key_id=0,
    key_prefix="local",
    org_id=0,
    tenant="local",
    project="local",
    environment="local",
    scopes=(READ, INGEST, SESSION_MINT),
    plan="local",
)


class LocalAuthenticator:
    """Accepts any token (including none) as the single local principal."""

    def authenticate(self, token: str | None) -> Principal | None:
        return LOCAL_PRINCIPAL

    def quota_for_tenant(self, tenant: str) -> tuple[str, int | None, int | None]:
        return "local", None, None

    def touch(self, key_id: int) -> None:
        pass

    def ping(self) -> None:
        pass

    def wait_ready(self, timeout: float = 60.0) -> None:
        pass

    def migrate(self) -> list[str]:
        return []
