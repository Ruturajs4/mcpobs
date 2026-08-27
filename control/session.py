"""Short-lived session tokens for client-launched (stdio) servers.

ADR-011. Roughly half of MCP servers run on stdio, which means the CLIENT
launches them on the end user's own machine -- so the only place to put an
ingest credential is a config file on a laptop. Today that credential is
org-wide and permanent, and the exposure is not that it can leak but that
nothing expires when it does.

A session token is minted by the customer's own service (which already
authenticates their user), lives ~3 hours, and can only write telemetry.

WHY A JWT RATHER THAN AN OPAQUE TOKEN IN REDIS. Not storage -- a session record
is a few hundred bytes. Two other reasons decide it:

  * Ingest is the highest-volume path in the system. An opaque token means a
    lookup on every span batch, so Redis being unavailable would become total
    ingest failure for every customer. Verification here is local.
  * The existing 30-second principal cache works because an ORG key is shared by
    thousands of requests. A PER-USER token is used by one laptop, so the same
    cache would miss nearly every time and the lookup would be paid anyway.

WHAT THIS MODULE REFUSES TO DO, DELIBERATELY:

  * It never trusts the token's `alg` header. Algorithm confusion -- swapping
    RS256 for HS256, or for `none` -- is the classic JWT break and it is a
    one-line mistake.
  * It never accepts a token without `aud`. An ingest session token must be
    rejected by the query API; without an audience claim, a credential that can
    write telemetry could read the org's data by being pointed at another host.
  * It never reads tenancy or attributes from anywhere but the token. The
    gateway already overwrites caller-claimed tenancy for the same reason: a
    value the caller can choose is not an identity.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt

from control.keys import INGEST
from control.models import Principal

log = logging.getLogger("mcpobs.session")

#: The only algorithm accepted, on both sides. Pinned rather than read from the
#: token, because a verifier that honours the token's own `alg` can be told to
#: verify with `none`.
ALGORITHM = "HS256"

#: Who these tokens are for. An ingest session token presented to the query API
#: fails on this claim rather than on scope alone.
AUDIENCE_INGEST = "mcpobs:ingest"

ISSUER = "mcpobs"

#: Default and ceiling. The customer may ask for less; a request for more is
#: clamped rather than refused, because the useful behaviour when someone asks
#: for a 30-day token is a 3-hour one, not an error they will work around.
DEFAULT_TTL_SECONDS = 3 * 60 * 60
MAX_TTL_SECONDS = 12 * 60 * 60

#: Laptop clocks are wrong. This project already treats host clocks as
#: unreliable enough to caveat latency percentiles; wall-clock skew of a minute
#: is unremarkable. Tokens refresh at ~75% of TTL, so this leeway never has to
#: cover a legitimate case -- it only stops a slightly-fast clock rejecting a
#: token that is genuinely valid.
CLOCK_LEEWAY_SECONDS = 60

#: Attributes ride on every request inside the token, and base64 adds a third.
#: A customer attaching a 2 KB user profile would add ~2.7 KB to every span
#: batch, forever.
MAX_ATTRIBUTE_BYTES = 512

#: Attribute keys a customer may bind. An allow-list rather than free-form: this
#: bounds both PII sprawl and ClickHouse cardinality, and an unknown key is far
#: more likely to be a mistake than a requirement.
ALLOWED_ATTRIBUTES = ("user_id", "user_name", "workspace", "tenant_label", "session_label")


class SessionError(Exception):
    """Minting refused. The message is safe to return to the caller."""


@dataclass(frozen=True)
class SessionClaims:
    """A verified session token."""

    org_id: int
    tenant: str
    project: str
    environment: str
    subject: str
    attributes: dict[str, str] = field(default_factory=dict)
    jti: str = ""
    epoch: int = 0
    expires_at: int = 0


#: RFC 7518 §3.2: an HMAC key should be at least as long as the hash output.
#: Enforced rather than warned about -- PyJWT logs a warning for a short key and
#: signs with it anyway, and a warning in a log nobody reads is not a control. A
#: short key is brute-forceable, and forging this token forges tenancy.
MIN_SIGNING_KEY_BYTES = 32


def _signing_key() -> str:
    key = os.getenv("MCPOBS_SESSION_SIGNING_KEY", "")
    if not key:
        # Refused rather than defaulted. A development fallback key would ship
        # to production the first time someone forgot to set this, and every
        # token in the world would then be forgeable.
        raise SessionError("session signing key is not configured")
    if len(key.encode()) < MIN_SIGNING_KEY_BYTES:
        raise SessionError(
            f"session signing key is shorter than {MIN_SIGNING_KEY_BYTES} bytes"
        )
    return key


def _key_id() -> str:
    """Which signing key produced a token.

    Present from the first release even with one key: without `kid`, rotation
    becomes a flag day where every outstanding token dies at once, because old
    and new cannot be verified side by side.
    """
    return os.getenv("MCPOBS_SESSION_KEY_ID", "k1")


def clean_attributes(attributes: dict[str, Any] | None) -> dict[str, str]:
    """Validate what the customer wants bound to the token.

    Raises rather than silently dropping: a customer who asks for `email` and
    receives a token without it would discover the omission in their telemetry,
    days later, rather than at the call that made the mistake.
    """
    if not attributes:
        return {}
    unknown = sorted(set(attributes) - set(ALLOWED_ATTRIBUTES))
    if unknown:
        raise SessionError(
            f"unsupported attribute(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(ALLOWED_ATTRIBUTES)}"
        )
    cleaned = {k: str(v) for k, v in attributes.items() if v is not None}
    size = sum(len(k) + len(v) for k, v in cleaned.items())
    if size > MAX_ATTRIBUTE_BYTES:
        raise SessionError(
            f"attributes are {size} bytes, over the {MAX_ATTRIBUTE_BYTES} limit. "
            "They travel on every request."
        )
    return cleaned


def mint(
    principal: Principal,
    subject: str,
    ttl_seconds: int | None = None,
    attributes: dict[str, Any] | None = None,
    epoch: int = 0,
) -> tuple[str, int]:
    """Issue a session token for one of the customer's users.

    `principal` is the customer's own server-side key -- the credential this
    whole design exists to keep off the end user's machine. Tenancy comes from
    it and is never a parameter.

    Returns (token, ttl_seconds) rather than an absolute expiry: the SDK's clock
    may be wrong, and a relative lifetime cannot be misread by a skewed one.
    """
    # `ttl_seconds or DEFAULT` was wrong: 0 is falsy, so a caller asking for a
    # zero-second token silently received a three-hour one. "Unset" and "zero"
    # are different requests and only one of them is valid.
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    if ttl <= 0:
        raise SessionError("ttl_seconds must be positive")
    # Clamped, never honoured as given. A client-chosen lifetime is a client
    # choosing its own exposure window.
    ttl = min(ttl, MAX_TTL_SECONDS)

    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE_INGEST,
        "iat": now,
        "exp": now + ttl,
        # Named so a single token can be rejected once a blacklist exists. It is
        # emitted before anything reads it, because a revocation list can only
        # reject a token it can name -- and tokens minted without this claim
        # would be permanently unrevocable.
        "jti": uuid.uuid4().hex,
        # Bumping a per-subject or per-org epoch invalidates every outstanding
        # token for it in one entry, which is what "deprovision this user"
        # actually needs. Also emitted before it is checked.
        "epoch": int(epoch),
        "org": principal.org_id,
        "tenant": principal.tenant,
        "project": principal.project,
        "env": principal.environment,
        "sub": str(subject),
        "attr": clean_attributes(attributes),
    }
    token = jwt.encode(
        payload, _signing_key(), algorithm=ALGORITHM, headers={"kid": _key_id()}
    )
    return token, ttl


def verify(token: str) -> SessionClaims:
    """Verify a session token, or raise.

    Local: no database, no cache, no network. That is the point -- this runs on
    the highest-volume path in the system, and a lookup here would make an
    unavailable Redis into an ingest-wide outage.
    """
    try:
        payload = jwt.decode(
            token,
            _signing_key(),
            # A LIST WE CHOOSE, never the token's own header.
            algorithms=[ALGORITHM],
            audience=AUDIENCE_INGEST,
            issuer=ISSUER,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SessionError("session token expired") from exc
    except jwt.InvalidTokenError as exc:
        # One message for every other failure. Distinguishing "bad signature"
        # from "wrong audience" tells a prober which half of a forgery worked.
        raise SessionError("invalid session token") from exc

    attributes = payload.get("attr") or {}
    if not isinstance(attributes, dict):
        attributes = {}

    return SessionClaims(
        org_id=int(payload.get("org", 0)),
        tenant=str(payload["tenant"]),
        project=str(payload["project"]),
        environment=str(payload.get("env", "")),
        subject=str(payload["sub"]),
        # Re-cleaned on the way OUT as well as in. The signature proves the
        # payload was ours, not that a past version of this code enforced the
        # rules the current one relies on.
        attributes={
            k: str(v) for k, v in attributes.items() if k in ALLOWED_ATTRIBUTES
        },
        jti=str(payload.get("jti", "")),
        epoch=int(payload.get("epoch", 0)),
        expires_at=int(payload["exp"]),
    )


def is_session_token(token: str | None) -> bool:
    """Whether this looks like a JWT rather than an API key.

    Shape only, so the gateway can route to the right verifier without trying
    both and reporting whichever failed second. API keys are `mcpo_`-prefixed
    and contain no dots; a JWT is three base64 segments.
    """
    if not token:
        return False
    return token.count(".") == 2 and not token.startswith("mcpo_")


def principal_for(
    claims: SessionClaims,
    plan: str = "trial",
    spans_per_minute: int | None = None,
    spans_per_day: int | None = None,
) -> Principal:
    """The Principal a verified session token stands for.

    INGEST ONLY, whatever the minting key could do. A leaked session token must
    be able to write telemetry for its lifetime and nothing else -- never to
    read the org's data.

    Quota is passed IN, resolved from the org rather than read from the token: a
    limit the client presents is a limit the client chooses. It defaults to the
    trial plan rather than to unlimited, because failing open on a quota is how
    one compromised laptop bills an entire organisation.
    """
    return Principal(
        key_id=0,
        key_prefix=f"session:{claims.subject}"[:32],
        org_id=claims.org_id,
        tenant=claims.tenant,
        project=claims.project,
        environment=claims.environment,
        scopes=(INGEST,),
        plan=plan,
        quota_spans_per_minute=spans_per_minute,
        quota_spans_per_day=spans_per_day,
        # Carried through to the stamping. Validated at mint AND re-filtered on
        # verify, so what arrives here is already only allow-listed keys.
        session_attributes=tuple(sorted(claims.attributes.items())),
    )
