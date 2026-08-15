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

DETAIL_ATTRIBUTE: Final = "mcpobs.failure.detail"
"""Captured error text from a FAILING result. On by default.

WHAT THIS IS AND IS NOT
    This is the text the MCP SDK put in an errored `CallToolResult` -- normally
    an exception string or the tool's own failure message. It exists because
    140 of 146 failing spans carried no message anywhere in the telemetry: the
    SDK's reachable branch calls `set_status(StatusCode.ERROR)` with no
    description (D13), so `status_message` is empty and an operator cannot
    answer "what actually went wrong".

    It is NOT payload capture. Successful results are never read. Payload
    capture (V2 §15) remains a separate, opt-in feature writing separate
    columns, and assertion B2 still requires those columns to be NULL.

HONEST CAVEAT
    An exception string CAN contain user data -- "user alice@example.com not
    found" is a realistic message. This is on by default, so that trade is made
    for the customer rather than by them, and the README discloses it in the
    integration snippet itself. `instrument(mcp, capture_error_detail=False)`
    turns it off.
"""

RESOURCE_URI_ATTRIBUTE: Final = "mcp.resource.uri"
"""Which resource was read. The SDK does not record this.

`OpenTelemetryMiddleware` derives its target from `params["name"]`, which
`tools/call` and `prompts/get` have and `resources/read` does not -- resources
are addressed by `uri`. So a resource span says only "a read happened", never
what was read, and neither the span name nor any attribute carries it.

Recovered here from `ctx.params["uri"]`, the same way the failure kind is
recovered: the information is present in the customer's process and simply
never reaches the span. This is structural addressing, the direct analogue of a
tool name -- not payload. It IS the standard semconv key, so if the SDK starts
emitting it, this becomes a harmless no-op rather than a conflict.
"""

REQUEST_ATTRIBUTE: Final = "gen_ai.tool.call.arguments"
RESPONSE_ATTRIBUTE: Final = "gen_ai.tool.call.result"
"""Tool request/response previews. OFF by default -- see mcpobs/payload.py.

These are the OTel GenAI semantic-convention keys, both marked opt-in there for
the same reason they are opt-in here. Using the standard names means a customer
already collecting them sees one set of attributes, not two.
"""

REQUEST_SIZE_ATTRIBUTE: Final = "mcpobs.request.size"
RESPONSE_SIZE_ATTRIBUTE: Final = "mcpobs.response.size"
"""Original byte counts, recorded even when the preview is truncated -- "the
result was 4 MB" is useful on its own."""

DETAIL_MAX_CHARS: Final = 512
"""Bounded. An unbounded error string is a payload by another name."""

CLASSIFIER_VERSION: Final = 2
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

    def error_detail(self, result: Any) -> str:
        """Truncated error text for a FAILING result. Never called otherwise.

        The caller checks `is_error` first; this method does not, deliberately,
        so that the "errors only" rule lives at one obvious call site rather
        than being implied by a helper's internals.
        """
        text = self.first_text(result)
        if not text:
            return ""
        if len(text) <= DETAIL_MAX_CHARS:
            return text
        return text[:DETAIL_MAX_CHARS] + f"… (+{len(text) - DETAIL_MAX_CHARS} chars)"

    @staticmethod
    def resource_uri(params: Any) -> str:
        """The `uri` a resources/* call addressed, or ''."""
        if not isinstance(params, dict):
            return ""
        uri = params.get("uri")
        return uri if isinstance(uri, str) else ""

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
