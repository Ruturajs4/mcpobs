"""Which transport this server is running on.

THE PROBLEM. The MCP SDK does not emit `network.transport`, so every stored
span had an empty transport column -- measured across 5,782 spans, all blank.
Nothing in the data could distinguish a stdio server from a streamable-HTTP one,
which matters because **stdio is the common deployment**: the client launches
the server (Claude Desktop, an IDE, another agent), so most customer servers
never bind a port at all.

The consequence was not cosmetic. If stdio silently stopped producing spans
while HTTP kept working, no fleet-level assertion could notice, because both
populations were indistinguishable once stored.

HOW IT IS DETERMINED, in order of reliability:

1. **Explicitly**, via `instrument(server, transport=...)`. Always wins. A
   customer with an unusual setup should not have to defeat a heuristic.

2. **From `server.run(transport)`**, which is where the SDK itself names it --
   `run()` takes the transport as its first argument, so this is the SDK's own
   answer rather than an inference.

3. **From `instrument_asgi(app)`**, which is only ever applied to an HTTP
   transport. Servers that build the ASGI app and drive uvicorn themselves --
   which real deployments do, because they want their own uvicorn config --
   never call `run()`, so branch 2 alone would miss them.

If none of those happened the value stays empty, exactly as before. A guess
would be worse than a blank: an attribute people group by must not be
occasionally wrong.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("mcpobs.transport")

#: Values the MCP specification defines. `sse` is legacy but still in the SDK's
#: own signature, so it is accepted rather than silently normalised away.
KNOWN = ("stdio", "streamable-http", "sse")

ATTRIBUTE = "network.transport"

_current: str | None = None
_explicit = False


def set_transport(value: str | None, *, explicit: bool = False) -> None:
    """Record the transport. An explicit call can never be overwritten by a guess."""
    global _current, _explicit
    if value is None:
        return
    if _explicit and not explicit:
        return
    if value not in KNOWN:
        log.debug("ignoring unknown transport %r", value)
        return
    _current = value
    _explicit = _explicit or explicit


def current() -> str | None:
    return _current


def reset() -> None:
    """Test seam. Module state is process-wide, and a test that sets it would
    otherwise leak into every test that ran after it."""
    global _current, _explicit
    _current = None
    _explicit = False


def observe_run(server: Any) -> None:
    """Wrap `server.run` so the transport it is called with is recorded.

    Wrapping rather than reading: the transport is not knowable at
    `instrument()` time, because `run()` has not been called yet. It is knowable
    the instant it is called, and that is the SDK's own value rather than
    something inferred from the environment.

    Idempotent, and failure here is never allowed to stop a server booting --
    losing an attribute is acceptable, losing the process is not.
    """
    run = getattr(server, "run", None)
    if run is None or getattr(run, "__mcpobs_wrapped__", False):
        return

    def wrapper(transport: str = "stdio", *args: Any, **kwargs: Any) -> Any:
        # Default named explicitly, mirroring the SDK's own signature: a server
        # calling a bare `run()` IS running stdio, and recording nothing there
        # would leave the most common deployment blank.
        set_transport(transport)
        return run(transport, *args, **kwargs)

    wrapper.__mcpobs_wrapped__ = True  # type: ignore[attr-defined]
    try:
        server.run = wrapper  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        log.debug("could not observe run(): %s", exc)


def annotate(span: Any) -> None:
    """Stamp the transport on a span, if one is known."""
    if _current and span is not None and getattr(span, "is_recording", lambda: False)():
        span.set_attribute(ATTRIBUTE, _current)
