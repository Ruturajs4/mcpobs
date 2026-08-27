"""Discover the real `Authenticator`, or fall back to the local default.

SAME MECHANISM `mcpobs/downstream.py` ALREADY USES, on purpose. That module
discovers installed OTel instrumentors via the `opentelemetry_instrumentor`
entry-point group rather than importing them by name; this does the same
thing for the control plane. If the private ECC package is `pip install`-ed
alongside this one and registers itself under `ENTRY_POINT_GROUP`, its
`ControlPlane` wins automatically -- ingest and query never branch on
"am I OSS or ECC", they only ever see the `Authenticator` protocol
(control/interfaces.py).

Resolved once and cached at module scope, mirroring the lazy singletons this
replaces (`ingest/app.py`'s old `control = ControlPlane()` global,
`query/app.py`'s old `control_plane()` function) -- constructing the real
`ControlPlane` opens a database connection, which must not happen at import
time in every process that merely imports this module (including tests).
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Final

from control.interfaces import Authenticator
from control.local import LocalAuthenticator

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP: Final = "mcpobs_control_plane"

_authenticator: Authenticator | None = None


def _discover() -> Authenticator:
    """The first registered entry point wins; none registered -> local default.

    Not "first N, tried in order until one works": a control plane that fails
    to construct (bad DSN, unreachable Postgres) should fail loudly at
    startup, not silently fall back to the single-tenant default and accept
    every token as `local`.
    """
    points = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name)
    if not points:
        log.info("no %s entry point registered; using LocalAuthenticator", ENTRY_POINT_GROUP)
        return LocalAuthenticator()
    entry_point = points[0]
    authenticator_class = entry_point.load()
    log.info("control plane: %s (from %s)", entry_point.name, entry_point.value)
    return authenticator_class()


def authenticator() -> Authenticator:
    """The process-wide `Authenticator`. Resolved once, on first use."""
    global _authenticator
    if _authenticator is None:
        _authenticator = _discover()
    return _authenticator


def reset() -> None:
    """Test-only: force the next `authenticator()` call to re-resolve."""
    global _authenticator
    _authenticator = None
