"""mcpobs — the optional onboarding helper for MCP Observability.

Everything here is additive and replaceable. A customer who prefers to
configure vanilla OpenTelemetry and send OTLP directly loses only the precise
failure category -- their failures report the coarse `tool_error` instead --
and nothing else breaks.

(No internal spec citation here on purpose: this docstring is published as the
customer-facing SDK reference, where a reference the reader cannot resolve is
worse than no reference.)

    from mcp.server import MCPServer
    from mcpobs import instrument

    mcp = MCPServer("my-server", version="1.0.0")
    instrument(mcp)                     # that is the whole integration
"""

from __future__ import annotations

from importlib.metadata import version as _metadata_version
from typing import Any

from mcpobs import session as _session
from mcpobs import transport as _transport
from mcpobs.asgi import instrument_asgi
from mcpobs.classifier import (
    ATTRIBUTE,
    CLASSIFIER_VERSION,
    DETAIL_ATTRIBUTE,
    MRTR_STATE_ATTRIBUTE,
    RESOURCE_URI_ATTRIBUTE,
    RESULT_TYPE_ATTRIBUTE,
    FailureClassifier,
    FailureKind,
)
from mcpobs.downstream import available, instrument_downstream
from mcpobs.http import HttpBodyCapture, instrument_httpx
from mcpobs.middleware import FailureClassifierMiddleware
from mcpobs.payload import PayloadCapture
from mcpobs.streaming import ObservedSubscriptionBus, instrument_progress

# Read from the installed metadata rather than written here, so it cannot drift
# from pyproject.toml -- two version strings in one package is one of them being
# wrong. Falls back when running from a source checkout that was never
# installed, which is how the tests and the demo server import it.
try:  # pragma: no cover - trivial, and depends on install state
    __version__ = _metadata_version("mcpobs")
except Exception:  # noqa: BLE001
    __version__ = "0.0.0.dev0"


__all__ = [
    "ATTRIBUTE",
    "CLASSIFIER_VERSION",
    "DETAIL_ATTRIBUTE",
    "MRTR_STATE_ATTRIBUTE",
    "RESOURCE_URI_ATTRIBUTE",
    "RESULT_TYPE_ATTRIBUTE",
    "FailureClassifier",
    "FailureClassifierMiddleware",
    "FailureKind",
    "HttpBodyCapture",
    "ObservedSubscriptionBus",
    "PayloadCapture",
    "__version__",
    "available",
    "instrument",
    "instrument_asgi",
    "instrument_downstream",
    "instrument_httpx",
    "instrument_progress",
]


def instrument(
    server: Any,
    capture_error_detail: bool = True,
    capture_payloads: bool = False,
    transport: str | None = None,
    session_endpoint: str | None = None,
    session_headers: Any = None,
) -> Any:
    """Attach failure classification to an MCPServer.

    `capture_error_detail` (default True) also records the error text from
    FAILING tool results, truncated to 512 characters. Without it an operator
    cannot see why a call failed -- the SDK leaves `status_message` empty. It
    never reads successful results and never populates the payload columns.
    Pass False to send only the failure category.

    `capture_payloads` (default False) additionally records tool arguments and
    results, truncated and redacted. It is OFF by default because it is every
    argument and every result of every call, not just failures -- see
    mcpobs/payload.py for what the redaction does and does not catch.

    Appends to the server's middleware chain, so it runs inside the SDK's
    built-in OpenTelemetry middleware and can annotate the span the SDK already
    opened. Does not create spans and does not wrap the protocol.

    `transport` names the transport this server runs on ("stdio",
    "streamable-http", "sse"). Leave it unset and it is detected from
    `server.run(...)`, which is where the SDK itself names it. Pass it when your
    server never calls `run()` -- for example when you build the ASGI app and
    drive uvicorn yourself.

    `session_endpoint` is your own service that mints short-lived tokens for a
    server running on an END USER's machine. Configure it HERE, in
    your server, rather than asking your users to add environment variables to
    their MCP client config -- they should not have to paste observability
    settings into Claude Desktop to use your product.

    `session_headers` authenticates that call. Pass a CALLABLE when the
    credential refreshes, which it usually does:

        instrument(
            mcp,
            session_endpoint="https://acme.com/mcpobs-session",
            session_headers=lambda: {"authorization": f"Bearer {current_token()}"},
        )

    A dict is read once at startup; a callable is read on every fetch. With a
    refreshing user token the difference is telemetry that works for an hour and
    then stops with no error anywhere.

    Idempotent: calling it twice attaches one middleware.
    """
    if session_endpoint is not None or session_headers is not None:
        _session.configure(endpoint=session_endpoint, headers=session_headers)

    _transport.set_transport(transport, explicit=transport is not None)
    # Wrapped even when the middleware is already attached: `instrument()` is
    # idempotent, but a second call is exactly how someone adds the transport
    # they forgot the first time.
    _transport.observe_run(server)

    chain = server.middleware
    if any(isinstance(m, FailureClassifierMiddleware) for m in chain):
        return server
    chain.append(
        FailureClassifierMiddleware(
            capture_error_detail=capture_error_detail,
            capture_payloads=capture_payloads,
        )
    )
    return server
