"""Recover the failure kind that the MCP SDK erases before it reaches the span.

WHY THIS EXISTS
    `MCPServer._handle_call_tool` catches every exception and converts it to
    `CallToolResult(isError=True)` *before* `OpenTelemetryMiddleware` observes
    the result (mcpserver/server.py:422). So a raised exception, a tool
    returning isError, an unknown tool and a schema violation all produce an
    identical span: status=ERROR, error.type="tool_error". See docs/decisions.md
    D13.

    The signal is not missing -- it is in the result *content*. And the part
    that distinguishes the cases is boilerplate the SDK itself writes, not text
    the tool author or end user produced:

        "Error executing tool {name}: {e}"   mcpserver/tools/base.py:181
        "Unknown tool: {name}"               mcpserver/tools/tool_manager.py:72

PRIVACY PROPERTY
    This classifier runs in the CUSTOMER's process and emits a single
    low-cardinality enum. No tool input or output is captured, transmitted or
    stored to make the taxonomy work. That is what keeps error intelligence a
    core feature instead of one gated behind payload capture and its redaction
    and retention machinery (V2 §15).

KNOWN WEAKNESS
    We match SDK-internal message formats, which can change in any release.
    tests/test_sdk_contract.py pins them against the installed SDK so an
    upgrade fails the build rather than silently degrading data. The durable
    fix is upstream: the SDK should expose the failure kind itself (D18).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final

ATTRIBUTE: Final = "mcpobs.failure.kind"
"""Span attribute this classifier sets. Vendor-prefixed, low cardinality."""

RESULT_TYPE_ATTRIBUTE: Final = "mcpobs.result.type"
"""Set only when resultType is not "complete" -- i.e. MRTR interim results."""

MRTR_STATE_ATTRIBUTE: Final = "mcpobs.mrtr.state"
"""Correlation key linking the round-trips of ONE logical tool call.

Under MRTR a server returns `input_required` plus a `requestState` blob, and the
client echoes that blob back on the retry. So the value a round EMITS matches the
value the next round RECEIVES -- which is the only link between them, because
(measured, see scripts/mrtr_experiment.py) the round-trips do NOT share a
trace_id.

ALWAYS A HASH, NEVER THE RAW BLOB: requestState carries recorded elicitation
outcomes -- the user's actual answers. Storing it would be payload capture
through the back door, breaking the privacy property that makes this taxonomy a
core feature (D17).
"""

CLASSIFIER_VERSION: Final = 1
"""Bumped whenever the rules change, so reclassification is a replay."""


class FailureKind:
    TOOL_ERROR: Final = "tool_error"
    """The tool ran and reported failure. The server behaved correctly."""

    SERVER_EXCEPTION: Final = "server_exception"
    """The handler raised. Our bug, or the tool's."""

    UNKNOWN_TOOL: Final = "unknown_tool"
    """The client called a tool that does not exist. Often a client-side bug."""

    INVALID_ARGUMENTS: Final = "invalid_arguments"
    """Arguments failed schema validation. Usually a model or client problem."""

    UNCLASSIFIED: Final = "unclassified"
    """Matched nothing. A rising count means the SDK moved -- alert on it."""


class FailureClassifier:
    """Maps an errored CallToolResult to a failure kind.

    Pure and side-effect free so it can be unit tested without a server.
    """

    EXECUTION_PREFIX: Final = "Error executing tool "
    UNKNOWN_TOOL_PREFIX: Final = "Unknown tool:"

    # Pydantic renders "1 validation error for echo_fastArguments" / "2 validation
    # errors for ...". Matched on the count phrase, which is stable across
    # pydantic versions and independent of the model name.
    VALIDATION_PATTERN: Final = re.compile(r"\d+ validation errors? for ")

    def classify(self, text: str) -> str:
        """Return a FailureKind for the first text block of an errored result.

        ORDER MATTERS. A validation failure is wrapped by the SDK and therefore
        ALSO carries the "Error executing tool " prefix:

            Error executing tool echo_fast: 1 validation error for ...

        so the validation check must come first, or every schema violation is
        misreported as a server exception.
        """
        if not text:
            return FailureKind.UNCLASSIFIED

        if text.startswith(self.UNKNOWN_TOOL_PREFIX):
            return FailureKind.UNKNOWN_TOOL

        if text.startswith(self.EXECUTION_PREFIX):
            if self.VALIDATION_PATTERN.search(text):
                return FailureKind.INVALID_ARGUMENTS
            return FailureKind.SERVER_EXCEPTION

        # No SDK boilerplate: the text is the tool's own message, which means
        # the tool ran and deliberately returned isError.
        return FailureKind.TOOL_ERROR

    def classify_result(self, result: Any) -> str:
        """Classify a tool result. Safe against unexpected shapes."""
        return self.classify(self.first_text(result))

    @staticmethod
    def first_text(result: Any) -> str:
        """First text block of a result, or '' if there is none.

        Handles BOTH shapes. By the time middleware sees a result it is the
        *sealed wire form* -- a plain dict with camelCase keys:

            {"content": [{"type": "text", "text": "..."}],
             "isError": True, "resultType": "complete", "_meta": {...}}

        The `CallToolResult` model only appears if this is called earlier in the
        chain or from a test. Supporting both costs four lines and avoids a
        classifier that silently returns "" in production.

        Deliberately tolerant: this runs inside a customer's request path and
        must never raise, whatever shape the result turns out to be.
        """
        content = (
            result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
        )
        for block in content or []:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(text, str) and text:
                return text
        return ""

    @staticmethod
    def is_error(result: Any) -> bool:
        """True if the result reports failure, in either shape."""
        if isinstance(result, dict):
            return bool(result.get("isError"))
        return bool(getattr(result, "is_error", False))

    @staticmethod
    def mrtr_state(value: Any) -> str:
        """Short, stable hash of a requestState blob. Never the blob itself."""
        if not isinstance(value, str) or not value:
            return ""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def outgoing_state(cls, result: Any) -> str:
        """Hash of the requestState this round EMITS (round N)."""
        value = result.get("requestState") if isinstance(result, dict) else None
        return cls.mrtr_state(value)

    @classmethod
    def incoming_state(cls, params: Any) -> str:
        """Hash of the requestState this round RECEIVES (round N+1)."""
        value = params.get("requestState") if isinstance(params, dict) else None
        return cls.mrtr_state(value)

    @staticmethod
    def result_type(result: Any) -> str:
        """The 2026-07-28 `resultType`: "complete" or "input_required".

        Present in the wire form, which means MRTR interim results ARE
        observable from middleware -- closing the gap D11 recorded, where the
        SDK's own span carries no resultType attribute.
        """
        if isinstance(result, dict):
            value = result.get("resultType")
        else:
            value = getattr(result, "resultType", None) or getattr(result, "result_type", None)
        return value if isinstance(value, str) else ""
