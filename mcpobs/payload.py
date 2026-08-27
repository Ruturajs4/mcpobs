"""Tool request and response capture.

WHY IT IS OFF BY DEFAULT, UNLIKE ERROR DETAIL
    Error detail (D49) is narrow: failing calls only, and the text is usually an
    exception message. This is every argument and every result of every call --
    a different magnitude in both volume and sensitivity. V2 §15 specifies
    payloads as "opt-in, redactable, size-limited, and separately retained", and
    a tool named `get_customer` returning a customer record is the normal case,
    not the edge case.

    So: one flag, easy to find, off until asked for.

        instrument(mcp, capture_payloads=True)

WHAT THE SAFEGUARDS ACTUALLY DO
    Redaction is pattern-based and therefore incomplete. It catches the obvious
    shapes -- a field called `password`, a bearer token, an API key -- and will
    miss a secret in a field called `note`. It reduces harm; it does not make
    capture safe. Anyone turning this on should know that, so the docstring says
    it rather than implying a guarantee.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

MAX_CHARS: Final = 2048
"""Truncation limit. Full results can be megabytes; a preview is for reading."""

REDACTED: Final = "[redacted]"

#: Field names whose VALUE is replaced wholesale. Matched case-insensitively as
#: a substring, so `api_key`, `apiKey` and `X-API-KEY` all hit.
SENSITIVE_KEYS: Final[tuple[str, ...]] = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "private_key", "session",
    "cookie", "signature", "access_key",
    # `requestState` carries RECORDED ELICITATION OUTCOMES -- the user's actual
    # answers (D28). It is hashed for MRTR correlation precisely because it must
    # not be stored; now that the whole params object is captured it would
    # otherwise ride along in plain text.
    "requeststate",
)

#: Keys that must NEVER be redacted, checked BEFORE the sensitive list.
#:
#: Pattern redaction is documented as incomplete (D56), but that was only ever
#: stated in one direction -- that it might MISS a secret. It also OVER-matches,
#: and that failure is quieter: `progressToken` contains "token", so the field
#: that correlates a progress notification to its request was being destroyed in
#: every captured payload. A missed secret is a risk you can reason about; a
#: silently deleted protocol field looks like the server never sent it.
#:
#: Deliberately an explicit list of PROTOCOL field names rather than a cleverer
#: matcher. Segment-aware matching would fix `author` (contains "auth") and not
#: `progressToken`, because "token" genuinely is a whole segment there -- the
#: distinction is what the field MEANS, which no string rule recovers.
#:
#: Known remaining false positives, stated rather than hidden: any key containing
#: `auth` (`author`), `session` (`sessionCount`) or `token` that is not listed
#: here is still redacted.
NEVER_REDACT: Final[frozenset[str]] = frozenset({
    "progresstoken",   # correlates progress notifications to their request
})

#: Value shapes that are secrets regardless of the field they sit in.
SENSITIVE_VALUES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),                  # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),           # GitHub
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                   # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),  # JWT
)


class PayloadCapture:
    """Renders tool arguments and results into bounded, redacted previews."""

    def __init__(self, max_chars: int = MAX_CHARS, redact: bool = True) -> None:
        self.max_chars = max_chars
        self.redact = redact

    # -- public ------------------------------------------------------------
    def request(self, method: str, request_id: Any, params: Any) -> tuple[str, int]:
        """The JSON-RPC request, as (preview, original size).

        THE ACTUAL WIRE MESSAGE, not just `arguments`. This is an MCP
        observability product: the useful artefact is the protocol message you
        can compare against the spec or paste into a bug report, and the first
        version threw away everything except the arguments.

        What that discarded, all of it debugging-relevant:
          * `_meta.io.modelcontextprotocol/clientInfo` -- WHICH CLIENT called.
            The SDK sets no client attribute on the span, so this is the only
            place client identity appears at all, and V2 6.1 asks for exactly
            that ("which clients are calling which tools").
          * `_meta` clientCapabilities -- what the client claimed to support,
            the first thing to check on a capability error.
          * `_meta` protocolVersion and traceparent.
          * `name`, so a prompt or resource call is self-describing.
        """
        envelope = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params if isinstance(params, dict) else {},
        }
        return self._render(envelope)

    def response(self, request_id: Any, result: Any) -> tuple[str, int]:
        """The JSON-RPC response, as (preview, original size).

        Also the whole message, so `resultType`, `isError` and especially
        `structuredContent` -- the typed result, previously invisible -- survive.

        Works for EVERY method. The first version looked for `content` blocks
        and therefore returned nothing for prompts/get (which returns
        `messages`) or resources/read (which returns `contents`).
        """
        body = result if isinstance(result, dict) else self._as_dict(result)
        if not body:
            return "", 0
        return self._render({"jsonrpc": "2.0", "id": request_id, "result": body})

    @staticmethod
    def _as_dict(result: Any) -> dict[str, Any]:
        """Best-effort for a model rather than the sealed wire form."""
        for attr in ("model_dump", "dict"):
            dumper = getattr(result, attr, None)
            if callable(dumper):
                try:
                    return dumper(by_alias=True, exclude_none=True)
                except Exception:  # noqa: BLE001
                    try:
                        return dumper()
                    except Exception:  # noqa: BLE001
                        return {}
        return {}

    def render(self, value: Any) -> tuple[str, int]:
        """Redact + truncate an arbitrary value, as (preview, original size).

        Public because `mcpobs.http` needs exactly the same treatment for HTTP
        bodies, and a second copy of the redaction rules is how the two drift
        apart until one of them leaks.
        """
        return self._render(value)

    # -- internals ---------------------------------------------------------
    def _render(self, value: Any) -> tuple[str, int]:
        if value is None or value == "":
            return "", 0
        if not isinstance(value, str):
            cleaned = self._scrub(value) if self.redact else value
            try:
                # indent=2 deliberately: this is read by a human in a panel, not
                # parsed by a machine, and a one-line JSON-RPC envelope with
                # _meta inside it is unreadable.
                text = json.dumps(cleaned, ensure_ascii=False, default=str, indent=2)
            except (TypeError, ValueError):
                text = str(cleaned)
        else:
            text = self._scrub_text(value) if self.redact else value

        size = len(text)
        if size > self.max_chars:
            return text[: self.max_chars] + f"… (+{size - self.max_chars} chars)", size
        return text, size

    def _scrub(self, value: Any) -> Any:
        """Redact by key name, recursively, before serialising."""
        if isinstance(value, dict):
            return {
                k: REDACTED if self._is_sensitive_key(k) else self._scrub(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            # Non-text content blocks are summarised by TYPE, never dumped: a
            # base64 image is megabytes of noise telling an operator nothing.
            return [self._scrub(self._summarise_block(v)) for v in value]
        if isinstance(value, str):
            return self._scrub_text(value)
        return value

    def _scrub_text(self, text: str) -> str:
        for pattern in SENSITIVE_VALUES:
            text = pattern.sub(REDACTED, text)
        return text

    @staticmethod
    def _summarise_block(value: Any) -> Any:
        if isinstance(value, dict) and value.get("type") in ("image", "audio", "blob"):
            kept = {k: v for k, v in value.items() if k not in ("data", "blob")}
            kept["data"] = f"[{value.get('type')} omitted]"
            return kept
        return value

    @staticmethod
    def _is_sensitive_key(key: Any) -> bool:
        lowered = str(key).lower()
        if lowered in NEVER_REDACT:
            return False
        return any(marker in lowered for marker in SENSITIVE_KEYS)
