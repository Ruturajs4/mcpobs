"""Minting and verifying API keys and invite codes.

Separated from the repository so the credential rules live in one readable file
rather than being spread across SQL statements. Everything here is pure: no
database, no clock, no config -- which is what makes it testable without either.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

PREFIX_CHARS: Final = 8
SECRET_BYTES: Final = 32

#: `mcpo_<environment>_<prefix>_<secret>`. The environment rides in the key on
#: purpose: the single most common ingest mistake is pointing a staging server
#: at production, and a key that says `mcpo_prod_...` in a staging config file
#: is visible to a human reading a diff.
BRAND: Final = "mcpo"

INGEST: Final = "ingest"
READ: Final = "read"

ADMIN: Final = "admin"
"""Operator scope: reads ACROSS tenants.

Deliberately a third scope rather than a flag on a read key. Every other scope
is bounded by one org -- `read` answers "my data", `ingest` writes "my data" --
and this one is not, which makes it a different KIND of credential rather than a
bigger version of the same one.

It is issued only by `scripts/admin.py`, never through any HTTP endpoint. An API
that can mint a cross-tenant credential is one authorization bug away from a
customer minting one, and there is no product reason for that endpoint to exist.
"""

VALID_SCOPES: Final[frozenset[str]] = frozenset({INGEST, READ, ADMIN})


def _hash(secret: str) -> str:
    """SHA-256 of a HIGH-ENTROPY secret.

    Not bcrypt/argon2, and the distinction matters. Those are slow by design to
    make brute force expensive against human-chosen passwords. This input is 256
    bits from `secrets.token_urlsafe`; brute force is not the threat model, and a
    deliberately slow hash would add its cost to the authentication of every
    ingest request in the hot path.

    Hashing at all is the part that matters: a leaked database dump must not
    contain usable credentials. If a PASSWORD column is ever added it needs a
    real password hash -- do not copy this.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def mint(environment: str) -> tuple[str, str, str]:
    """Return (token, prefix, secret_hash) for a new key.

    The caller stores prefix + hash and shows the token once. There is
    deliberately no way to recover the token afterwards.
    """
    prefix = secrets.token_hex(PREFIX_CHARS // 2)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    token = f"{BRAND}_{environment}_{prefix}_{secret}"
    return token, prefix, _hash(secret)


def split(token: str) -> tuple[str, str] | None:
    """(prefix, secret) from a presented token, or None if it is malformed.

    Returns None rather than raising: a malformed key is an ordinary
    unauthenticated request, not an exceptional condition, and it arrives from
    the internet constantly.
    """
    parts = token.strip().split("_")
    if len(parts) < 4 or parts[0] != BRAND:
        return None
    prefix = parts[2]
    secret = "_".join(parts[3:])  # token_urlsafe can itself contain '_'
    if not prefix or not secret:
        return None
    return prefix, secret


def verify(secret: str, secret_hash: str) -> bool:
    """Constant-time comparison.

    `==` on a hash leaks its prefix through timing. The leak is small and the
    fix is one function call, so there is no trade to weigh.
    """
    return hmac.compare_digest(_hash(secret), secret_hash)


def mint_invite() -> tuple[str, str]:
    """Return (code, code_hash) for a new invite.

    An invite code is a bearer credential -- whoever holds it becomes a member
    of someone's organisation -- so it is generated and stored exactly like a
    key, and shown once.
    """
    code = secrets.token_urlsafe(24)
    return code, _hash(code)


def hash_invite(code: str) -> str:
    return _hash(code)


def parse_scopes(raw: str) -> tuple[str, ...]:
    """Scopes as stored (comma-separated) to a tuple, dropping unknown ones.

    Unknown scopes are DROPPED rather than passed through. A scope string is
    data, and data that reaches an authorisation check should never be able to
    grant something the code does not recognise.
    """
    return tuple(s for s in (p.strip() for p in raw.split(",")) if s in VALID_SCOPES)
