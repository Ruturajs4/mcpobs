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
from mcpobs.middleware import FailureClassifierMiddleware

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
    "instrument",
]


def instrument(server: Any, capture_error_detail: bool = True) -> Any:
    """Attach failure classification to an MCPServer.

    `capture_error_detail` (default True) also records the error text from
    FAILING tool results, truncated to 512 characters. Without it an operator
    cannot see why a call failed -- the SDK leaves `status_message` empty. It
    never reads successful results and never populates the payload columns.
    Pass False to send only the failure category.

    Appends to the server's middleware chain, so it runs inside the SDK's
    built-in OpenTelemetry middleware and can annotate the span the SDK already
    opened. Does not create spans and does not wrap the protocol.

    Idempotent: calling it twice attaches one middleware.
    """
    chain = server.middleware
    if any(isinstance(m, FailureClassifierMiddleware) for m in chain):
        return server
    chain.append(FailureClassifierMiddleware(capture_error_detail=capture_error_detail))
    return server
