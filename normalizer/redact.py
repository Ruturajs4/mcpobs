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
        if not attributes:
            return attributes
        return {
            key: self.value(key, value) if key in RISKY_KEYS else value
            for key, value in attributes.items()
        }

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
