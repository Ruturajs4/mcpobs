"""Pluggable control-plane interface, so ingest/query never import
`ControlPlane` directly -- only this shape.

WHY A PROTOCOL, NOT AN ABSTRACT BASE CLASS
    `ControlPlane` (control/repository.py) already has exactly this shape and
    is moving to the private ECC repo unmodified. A Protocol lets it keep
    satisfying this interface it never imports, with zero coupling back to
    OSS beyond "these method names and types line up". An ABC would force
    ECC's class to import and subclass something from OSS for no behavioral
    gain -- structural typing is the whole point here.

WHY THIS SET OF METHODS AND NO MORE
    Every method below is one `ingest/app.py` or `query/app.py` actually
    calls on `control` today. `ControlPlane`'s real surface is much bigger --
    org/project/invite/key/audit management -- but none of that runs on the
    ingest or query hot path. It stays admin-only surface in ECC
    (`query/admin.py`, `query/admin_repository.py`, `scripts/admin.py`) and is
    deliberately not part of this interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from control.models import Principal


@runtime_checkable
class Authenticator(Protocol):
    """What ingest and query need to turn an API key into a `Principal`."""

    def authenticate(self, token: str | None) -> Principal | None:
        """Resolve a bearer token, or return None if it is invalid/revoked."""
        ...

    def quota_for_tenant(self, tenant: str) -> tuple[str, int | None, int | None]:
        """(plan, spans_per_minute override, spans_per_day override) for a tenant.

        Looked up by tenant rather than trusted from a session token: the
        token proves who the caller is, not what the org is currently
        allowed (see ingest/app.py's session-token path).
        """
        ...

    def touch(self, key_id: int) -> None:
        """Record that a key was just used (last-seen bookkeeping)."""
        ...

    def ping(self) -> None:
        """Raise if the control plane cannot be reached. Used by /ready."""
        ...

    def wait_ready(self, timeout: float = 60.0) -> None:
        """Block until the control plane is reachable, or raise on timeout."""
        ...

    def migrate(self) -> list[str]:
        """Apply any pending schema migrations. Returns the filenames applied."""
        ...
