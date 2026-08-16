"""Fetch and refresh a session token, for servers the client launches.

ADR-011. A stdio server runs on the end user's machine, so it must not hold a
long-lived credential. Instead it calls an endpoint the CUSTOMER hosts, which
authenticates their user however their product already does, and returns a
short-lived token.

    MCPOBS_SESSION_ENDPOINT=https://acme.com/internal/mcpobs-session

THE RULES THIS FILE IS BUILT TO, each from a failure that would otherwise be
discovered in production:

1.  TELEMETRY NEVER BLOCKS THE TOOL. An unreachable endpoint means the server
    starts without telemetry and keeps retrying. Our outage must not become the
    customer's product outage, and a tool that will not start because its
    metrics backend is down is a worse product than one with no metrics.

2.  REFRESH EARLY, AND WITH JITTER. At 75% of lifetime, not at expiry: a laptop
    clock that is a minute fast would otherwise present a token the server has
    already retired. The jitter matters more -- ten thousand machines opened at
    09:00 would all refresh at 12:00 together and flood the customer's own
    authentication service, an outage we would have caused.

3.  A FAILED REFRESH KEEPS THE CURRENT TOKEN. It is valid for another 25% of
    its life; discarding it on the first failed attempt would throw away
    working credentials because of a transient.

4.  HTTPS ONLY, EXCEPT LOCALHOST. A plaintext session endpoint hands the token
    to anyone on the network, which is worse than the static key it replaces.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("mcpobs.session")

ENDPOINT_ENV = "MCPOBS_SESSION_ENDPOINT"
#: Whatever the customer's endpoint needs to authenticate the call -- typically
#: the user's existing session token, which their own app already has. It is a
#: USER-scoped credential, not an org-wide one, which is the difference that
#: makes this design worth the trouble.
HEADERS_ENV = "MCPOBS_SESSION_HEADERS"

#: Refresh at this fraction of the token's life.
REFRESH_AT = 0.75
#: Spread refreshes over this fraction of the remaining time.
JITTER = 0.10
#: Never hammer the customer's endpoint faster than this after a failure.
MIN_RETRY_SECONDS = 5.0
MAX_RETRY_SECONDS = 300.0
#: A session endpoint that takes longer than this is down as far as we care.
FETCH_TIMEOUT_SECONDS = 10.0


@dataclass
class Session:
    token: str
    endpoint: str
    #: Monotonic deadline, not a wall-clock time. The wall clock can step --
    #: NTP corrections, sleep/wake on a laptop -- and a token's remaining life
    #: must not jump when it does.
    refresh_at: float
    expires_at: float


def _diag(message: str) -> None:
    """Diagnostics to STDERR, never stdout.

    On the stdio transport stdout IS the JSON-RPC channel, and a single stray
    line corrupts the protocol. This has bitten this codebase once already,
    silently, for weeks.
    """
    print(f"[mcpobs] {message}", file=sys.stderr)


def _parse_headers(raw: str) -> dict[str, str]:
    """`k=v,k2=v2` -> dict. The same shape OTEL_EXPORTER_OTLP_HEADERS uses.

    Deliberately the OTel format rather than JSON: a customer configuring this
    is already configuring OTel headers beside it, and two syntaxes for the same
    idea in adjacent variables is how one of them gets written wrong.
    """
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            key, _, value = pair.partition("=")
            out[key.strip()] = value.strip()
    return out


def _check_endpoint(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return  # development
    raise ValueError(
        f"session endpoint must be https (got {parsed.scheme or 'no scheme'}). "
        "A plaintext endpoint hands the token to the network."
    )


class SessionProvider:
    """Holds the current token and keeps it fresh.

    Thread-safe: OTel exports from a background thread while the server handles
    calls on another, and both read the token.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        headers: dict[str, str] | None = None,
        fetch: Any = None,
    ) -> None:
        self.endpoint: str = endpoint or os.getenv(ENDPOINT_ENV, "") or ""
        self.headers = headers if headers is not None else _parse_headers(
            os.getenv(HEADERS_ENV, "")
        )
        self._fetch = fetch or self._http_fetch
        self._session: Session | None = None
        self._lock = threading.Lock()
        self._failures = 0
        self._next_attempt = 0.0

    # -- public ------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def current(self) -> Session | None:
        """The token to export with, refreshing if it is time. Never raises.

        Returning None means "no telemetry right now", which callers treat as a
        reason to buffer rather than a reason to fail.
        """
        with self._lock:
            now = time.monotonic()
            session = self._session

            if session and now < session.refresh_at:
                return session

            if now < self._next_attempt:
                # Backing off. Keep serving the old token if it is still valid --
                # a failed refresh is not a reason to discard working
                # credentials that have a quarter of their life left.
                return session if session and now < session.expires_at else None

            fetched = self._try_fetch()
            if fetched is not None:
                self._session = fetched
                return fetched
            return session if session and now < session.expires_at else None

    def invalidate(self) -> None:
        """Drop the current token, e.g. after the server rejected it as expired.

        Does NOT fetch: the caller is on a failure path and a synchronous fetch
        there would turn one rejected export into a stalled exporter thread.
        """
        with self._lock:
            self._session = None

    # -- internals ---------------------------------------------------------
    def _try_fetch(self) -> Session | None:
        try:
            _check_endpoint(self.endpoint)
            payload = self._fetch(self.endpoint, self.headers)
            session = self._build(payload)
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            delay = min(
                MAX_RETRY_SECONDS, MIN_RETRY_SECONDS * (2 ** min(self._failures, 6))
            )
            self._next_attempt = time.monotonic() + delay
            # First failure is loud, the rest are not. A server that cannot
            # reach the endpoint for an hour should not write 700 identical
            # lines to the customer's stderr.
            if self._failures == 1:
                _diag(f"session endpoint unreachable ({exc}); telemetry paused, retrying")
            else:
                log.debug("session fetch failed (%s attempts): %s", self._failures, exc)
            return None

        if self._failures:
            _diag("session endpoint recovered; telemetry resumed")
        self._failures = 0
        self._next_attempt = 0.0
        return session

    @staticmethod
    def _build(payload: dict[str, Any]) -> Session:
        token = str(payload["token"])
        # `expires_in`, never an absolute time. The response was produced by a
        # server whose clock we do not share, and a laptop's clock is routinely
        # wrong by minutes.
        ttl = float(payload.get("expires_in") or 0)
        if not token or ttl <= 0:
            raise ValueError("session response missing token or expires_in")

        now = time.monotonic()
        # Jitter SUBTRACTS, so a refresh is always earlier than the deadline and
        # never later. Adding it would let some machines refresh after expiry.
        spread = 1.0 - random.uniform(0.0, JITTER)
        return Session(
            token=token,
            endpoint=str(payload.get("endpoint") or ""),
            refresh_at=now + ttl * REFRESH_AT * spread,
            expires_at=now + ttl,
        )

    @staticmethod
    def _http_fetch(endpoint: str, headers: dict[str, str]) -> dict[str, Any]:
        import httpx

        response = httpx.get(endpoint, headers=headers, timeout=FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("session endpoint did not return a JSON object")
        return data
