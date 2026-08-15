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
)

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
    def request(self, params: Any) -> tuple[str, int]:
        """The arguments a tool was called with, as (preview, original size)."""
        args = params.get("arguments") if isinstance(params, dict) else None
        return self._render(args)

    def response(self, result: Any) -> tuple[str, int]:
        """The content a tool returned, as (preview, original size).

        Reads the same `content` blocks the classifier already reads, so a
        result is never serialised twice.
        """
        content = (
            result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
        )
        if not content:
            return "", 0
        texts = []
        for block in content:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(text, str):
                texts.append(text)
            elif isinstance(block, dict):
                # Non-text blocks (image, audio, resource) are summarised by
                # TYPE rather than dumped: a base64 image is megabytes of noise
                # that tells an operator nothing.
                texts.append(f"[{block.get('type', 'block')}]")
        return self._render("\n".join(texts))

    # -- internals ---------------------------------------------------------
    def _render(self, value: Any) -> tuple[str, int]:
        if value is None or value == "":
            return "", 0
        if not isinstance(value, str):
            cleaned = self._scrub(value) if self.redact else value
            try:
                text = json.dumps(cleaned, ensure_ascii=False, default=str)
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
            return [self._scrub(v) for v in value]
        if isinstance(value, str):
            return self._scrub_text(value)
        return value

    def _scrub_text(self, text: str) -> str:
        for pattern in SENSITIVE_VALUES:
            text = pattern.sub(REDACTED, text)
        return text

    @staticmethod
    def _is_sensitive_key(key: Any) -> bool:
        lowered = str(key).lower()
        return any(marker in lowered for marker in SENSITIVE_KEYS)
