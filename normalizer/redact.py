"""Redaction for span attributes that routinely carry secrets.

WHY THIS EXISTS SERVER-SIDE AS WELL AS CLIENT-SIDE
    `mcpobs.payload` redacts what WE capture. This redacts what the OTel
    instrumentation libraries capture on their own, which we do not control:

      * `http.url` -- query strings carry `?api_key=…` constantly
      * `db.statement` -- SQL with inlined literals, i.e. customer data

    Both were flowing into `span_attributes` completely unredacted while payload
    capture was carefully truncating and scrubbing a few columns over. That is
    privacy theatre: the sensitive value was already stored, just somewhere less
    obvious.

WHAT IT COSTS
    Raw attributes exist so nothing is lost and a replay can reprocess anything
    (Architecture.md §5.4). Redacting them trades a little fidelity for not
    storing secrets. Only the listed keys are touched; everything else is stored
    exactly as emitted.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED: Final = "[redacted]"

#: Attribute keys whose values are scrubbed. Deliberately short: each entry is
#: a key that is KNOWN to carry secrets in practice, not a guess.
RISKY_KEYS: Final[frozenset[str]] = frozenset({
    "http.url", "url.full", "http.target", "url.query",
    "db.statement", "db.query.text",
})

#: Attribute keys that ARE a credential, replaced whole rather than pattern
#: matched. Substring match, because instrumentation names vary:
#: `http.request.header.authorization`, `http.response.header.set_cookie`,
#: `rpc.request.metadata.authorization`.
CREDENTIAL_KEYS: Final[tuple[str, ...]] = (
    "authorization", "proxy-authorization", "cookie", "x-api-key",
    "api-key", "x-auth-token", "set-cookie",
)

#: Query/DB parameter names whose values are replaced.
SENSITIVE_PARAMS: Final[tuple[str, ...]] = (
    "token", "api_key", "apikey", "key", "secret", "password", "passwd",
    "auth", "signature", "sig", "access_token", "session",
)

#: Value shapes that are secrets wherever they appear.
SENSITIVE_VALUES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
)

#: SQL string literals. A parameterised statement (`WHERE id = ?`) is untouched;
#: an interpolated one (`WHERE email = 'a@b.com'`) has its literals replaced.
SQL_LITERAL: Final = re.compile(r"'((?:[^']|'')*)'")


class AttributeRedactor:
    """Scrubs known-risky attribute values. Never raises."""

    def apply(self, attributes: dict[str, str]) -> dict[str, str]:
        """Redact every attribute, not only the six we predicted.

        THE BUG THIS FIXES
            This used to scrub ONLY keys in `RISKY_KEYS`, so a credential in any
            other attribute was stored verbatim. Measured:

                http.request.header.authorization
                    -> "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.sig"

            stored raw. Nothing had leaked only because nothing in this stack
            captures inbound headers -- the protection was the absence of a
            feature, not a decision. Any customer setting the documented
            one-liner

                OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST=authorization

            would have written OAuth access tokens into ClickHouse in plain text.
            And per D62 a secret that reaches the table cannot be recalled: it is
            in backups, in the Kafka replay window, and in the archive.

        SO THE VALUE PATTERNS NOW RUN EVERYWHERE, and the key list narrows to
        what it always meant -- keys needing STRUCTURAL treatment (URL query
        strings, SQL literals) rather than keys that might contain a secret.
        Predicting which key a credential will land in is exactly the bet a
        deny-list loses (D70).
        """
        if not attributes:
            return attributes
        return {
            key: self._redact(key, value) for key, value in attributes.items()
        }

    def _redact(self, key: str, value: str) -> str:
        if not isinstance(value, str) or not value:
            return value
        try:
            lowered = key.lower()
            # An attribute that IS a credential header is replaced whole. Its
            # value may be an opaque token no pattern matches -- a session
            # cookie, a bespoke scheme -- so shape-matching is not enough here.
            if any(marker in lowered for marker in CREDENTIAL_KEYS):
                return REDACTED
            if key in RISKY_KEYS:
                return self.value(key, value)
            return self._scrub_shapes(value)
        except Exception:  # noqa: BLE001 - redaction must never break ingest
            return REDACTED

    def _scrub_shapes(self, value: str) -> str:
        """Credential SHAPES, in any attribute.

        Length-guarded before touching a regex: the shortest match is
        `Bearer ` plus eight characters, so anything under 15 chars cannot
        contain one. Most attributes are short (`GET`, `200`, a tool name), so
        this skips the regex pass for the large majority of them and keeps a
        per-span cost off the normalizer's hot path.
        """
        if len(value) < 15:
            return value
        for pattern in SENSITIVE_VALUES:
            value = pattern.sub(REDACTED, value)
        return value

    def value(self, key: str, value: str) -> str:
        if not isinstance(value, str) or not value:
            return value
        try:
            if key in ("db.statement", "db.query.text"):
                return self._sql(value)
            return self._url(value)
        except Exception:  # noqa: BLE001 - redaction must never break ingest
            return REDACTED

    # -- internals ---------------------------------------------------------
    def _sql(self, statement: str) -> str:
        """Replace inlined literals; leave parameter placeholders alone.

        `SELECT … WHERE customer = ?` keeps its shape, which is what you group
        by. `WHERE email = 'a@b.com'` loses the address, which is what you must
        not store.
        """
        redacted = SQL_LITERAL.sub(f"'{REDACTED}'", statement)
        return self._patterns(redacted)

    def _url(self, url: str) -> str:
        """Scrub sensitive query parameters, keeping the URL readable.

        The path is what identifies the call, so it survives; only the named
        parameters are replaced. A URL with no query string is unchanged.
        """
        parts = urlsplit(url)
        if not parts.query:
            return self._patterns(url)
        pairs = [
            (k, REDACTED if any(m in k.lower() for m in SENSITIVE_PARAMS) else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        rebuilt = urlunsplit(parts._replace(query=urlencode(pairs)))
        return self._patterns(rebuilt)

    @staticmethod
    def _patterns(text: str) -> str:
        for pattern in SENSITIVE_VALUES:
            text = pattern.sub(REDACTED, text)
        return text


#: Leading keyword -> operation name. Deliberately small: these are the
#: statements that actually appear in a tool's hot path.
_SQL_OPERATIONS: Final[tuple[str, ...]] = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
    "REPLACE", "MERGE", "TRUNCATE", "WITH",
)

_SQL_TABLE: Final = re.compile(
    r"\b(?:FROM|INTO|UPDATE|TABLE|JOIN)\s+[\"'`\[]?([A-Za-z_][\w.]*)", re.I
)


def parse_statement(statement: str) -> tuple[str, str]:
    """(operation, collection) from a SQL statement -- closes DF-12.

    `opentelemetry-instrumentation-dbapi` emits `db.system` and `db.statement`
    but neither `db.operation` nor `db.collection`, so "which table is slow" was
    unanswerable without reading every statement by eye. Both are derivable from
    the statement we already hold.

    Parsing SQL with regex is only defensible because of what it is used FOR:
    two low-cardinality labels to group by. A statement it cannot parse yields
    ("", ""), which is exactly the status quo -- never a wrong answer.
    """
    if not statement:
        return "", ""
    stripped = statement.lstrip()
    head = stripped.split(None, 1)[0].upper().strip("(") if stripped else ""
    operation = head if head in _SQL_OPERATIONS else ""
    match = _SQL_TABLE.search(stripped)
    return operation, (match.group(1) if match else "")
