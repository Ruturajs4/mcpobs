"""Control-plane primitives that ship in OSS: scopes, key formats, the
`Principal`/`Authenticator` shapes, the single-tenant default, and the
entry-point resolver (control/resolve.py) that finds a real implementation
if one is installed.

The real, Postgres-backed `ControlPlane` (orgs, users, invites, projects,
audit log) is NOT here -- it lives in the private ECC repo, which depends on
this package and registers itself against `Authenticator`
(control/interfaces.py) via the `mcpobs_control_plane` entry point. See
docs/decisions.md D180.
"""

from control.keys import ADMIN, INGEST, READ
from control.models import IssuedKey, Principal

__all__ = ["ADMIN", "INGEST", "READ", "IssuedKey", "Principal"]
