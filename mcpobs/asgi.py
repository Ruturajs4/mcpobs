"""Observe the HTTP layer BENEATH MCP (closes DF-22).

WHY MCP-LEVEL INSTRUMENTATION CANNOT SEE AUTHORIZATION
    The 2026-07-28 authorization spec puts auth at the TRANSPORT level: the MCP
    server is an OAuth 2.1 resource server, and a request without a valid token
    is answered with `401 Unauthorized` and a `WWW-Authenticate` header before
    any method is dispatched.

    So it happens before the SDK's `OpenTelemetryMiddleware` and before ours.
    A client that cannot authenticate produces NO MCP SPAN AT ALL -- the whole
    class of "my clients cannot connect" was absent from the console rather
    than shown as a failure. Expired tokens, wrong-audience tokens, failed
    discovery, and `403 insufficient_scope` were all invisible.

    That last one matters more than it sounds. The spec's step-up authorization
    flow makes `insufficient_scope` a ROUTINE runtime occurrence that clients
    are expected to handle -- so we were blind to a normal protocol flow, not
    just to a misconfiguration.

WHY THIS WRAPS AN APP INSTANCE INSTEAD OF PATCHING A CLASS
    `StarletteInstrumentor().instrument()` patches
    `starlette.applications.Starlette`. The MCP SDK binds that name at IMPORT
    time, so patching afterwards has no effect on the app it builds -- measured:
    instrument-then-import gives `_InstrumentedStarlette` with the middleware
    attached, import-then-instrument gives a plain `Starlette` with nothing.

    `instrument_downstream()` reports `starlette: instrumented` in BOTH cases,
    because the class patch genuinely did happen. A report that says
    "instrumented" while producing no spans is worse than one that says nothing,
    so the ordering hazard is called out here and in the README rather than left
    for someone to discover from an empty dashboard.

    Wrapping the app instance has no ordering hazard: by the time you hold an
    app, there is nothing left to race.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def instrument_asgi(app: Any, **kwargs: Any) -> Any:
    """Wrap an ASGI app so every HTTP request becomes a server span.

    Returns the wrapped app; the original is unchanged. Use it as:

        app = mcp.streamable_http_app()
        app = instrument_asgi(app)
        uvicorn.run(app, host="0.0.0.0", port=8000)

    This is what makes 401 and 403 visible: those responses never reach an MCP
    method, so the span the ASGI layer produces is the ONLY record that the
    request happened at all.

    Returns the app unwrapped if the ASGI instrumentation is not installed. An
    observability helper must not stop a server from serving because an optional
    dependency is missing.
    """
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    except ImportError:
        log.info(
            "opentelemetry-instrumentation-asgi not installed; "
            "HTTP-layer spans (and therefore 401/403 visibility) unavailable"
        )
        return app

    # `receive`/`send` sub-spans are suppressed by default. The ASGI
    # instrumentation emits one of each PER REQUEST alongside the server span,
    # so leaving them on triples the span volume of an HTTP MCP server -- and
    # measured, they carry nothing an MCP operator uses: the status code and
    # duration are already on the server span. A customer who wants them can
    # pass `exclude_spans=[]`.
    kwargs.setdefault("exclude_spans", ["receive", "send"])
    return OpenTelemetryMiddleware(app, **kwargs)
