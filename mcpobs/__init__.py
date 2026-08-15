"""mcpobs — the optional onboarding helper for MCP Observability.

Everything here is additive and replaceable. A customer who prefers to
configure vanilla OpenTelemetry and send OTLP directly loses only the derived
failure kind; nothing breaks (V2 §18.2).

    from mcp.server import MCPServer
    from mcpobs import instrument

    mcp = MCPServer("my-server", version="1.0.0")
    instrument(mcp)                     # that is the whole integration
"""

from __future__ import annotations

from typing import Any

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

    Idempotent: calling it twice attaches one middleware.
    """
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
