"""The SDK side of ADR-011.

Almost everything here is a failure mode. The happy path is one test; the rest
is what happens when the customer's endpoint is down, slow, or lying — because
this runs on end users' machines where nobody is watching a dashboard.
"""

from __future__ import annotations

import time

import pytest

from mcpobs.session import JITTER, REFRESH_AT, SessionProvider


def responder(*payloads: object):
    """A fetch that returns each payload in turn, raising where one is an
    Exception. Lets a test script a sequence of outages."""
    queue = list(payloads)

    def fetch(endpoint: str, headers: dict[str, str]) -> dict:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return dict(item)  # type: ignore[arg-type]

    return fetch


TOKEN = {"token": "t1", "expires_in": 3600, "endpoint": "https://ingest.example"}


class TestHappyPath:
    def test_a_token_is_fetched_and_reused(self) -> None:
        calls = []

        def fetch(endpoint: str, headers: dict[str, str]) -> dict:
            calls.append(endpoint)
            return dict(TOKEN)

        p = SessionProvider(endpoint="https://acme.example/session", fetch=fetch)
        assert p.current().token == "t1"  # type: ignore[union-attr]
        assert p.current().token == "t1"  # type: ignore[union-attr]
        # Fetched once, not once per export. The exporter asks for the token on
        # every batch.
        assert len(calls) == 1

    def test_headers_are_passed_to_the_customers_endpoint(self) -> None:
        seen: dict[str, str] = {}

        def fetch(endpoint: str, headers: dict[str, str]) -> dict:
            seen.update(headers)
            return dict(TOKEN)

        SessionProvider(
            endpoint="https://acme.example/session",
            headers={"authorization": "Bearer user-session"},
            fetch=fetch,
        ).current()
        assert seen == {"authorization": "Bearer user-session"}


class TestFailureNeverBlocks:
    def test_an_unreachable_endpoint_returns_none_rather_than_raising(self) -> None:
        """Rule 1. A tool that will not start because its telemetry backend is
        down is a worse product than one with no telemetry."""
        p = SessionProvider(
            endpoint="https://acme.example/session",
            fetch=responder(ConnectionError("refused")),
        )
        assert p.current() is None

    def test_a_failed_refresh_keeps_the_current_token(self) -> None:
        """Rule 3. It has a quarter of its life left; a transient is not a
        reason to throw away working credentials."""
        p = SessionProvider(
            endpoint="https://acme.example/session",
            fetch=responder(TOKEN, ConnectionError("down")),
        )
        assert p.current().token == "t1"  # type: ignore[union-attr]

        # Force a refresh attempt, which will fail.
        p._session.refresh_at = time.monotonic() - 1  # type: ignore[union-attr]
        still = p.current()
        assert still is not None and still.token == "t1"

    def test_an_expired_token_is_not_served_after_a_failed_refresh(self) -> None:
        """The other half of rule 3: keeping it is right, keeping it forever is
        not. An expired token would be rejected by the gateway anyway."""
        p = SessionProvider(
            endpoint="https://acme.example/session",
            fetch=responder(TOKEN, ConnectionError("down")),
        )
        p.current()
        p._session.refresh_at = time.monotonic() - 1  # type: ignore[union-attr]
        p._session.expires_at = time.monotonic() - 1  # type: ignore[union-attr]
        assert p.current() is None

    def test_failures_back_off_instead_of_hammering(self) -> None:
        """A server that cannot reach the endpoint must not turn into a load
        generator against the customer's own auth service."""
        attempts = []

        def fetch(endpoint: str, headers: dict[str, str]) -> dict:
            attempts.append(time.monotonic())
            raise ConnectionError("down")

        p = SessionProvider(endpoint="https://acme.example/session", fetch=fetch)
        for _ in range(5):
            p.current()
        # One attempt; the rest are inside the backoff window.
        assert len(attempts) == 1

    def test_recovery_is_reported(self) -> None:
        p = SessionProvider(
            endpoint="https://acme.example/session",
            fetch=responder(ConnectionError("down"), TOKEN),
        )
        assert p.current() is None
        p._next_attempt = 0.0  # backoff elapsed
        assert p.current().token == "t1"  # type: ignore[union-attr]
        assert p._failures == 0


class TestRefreshScheduling:
    def test_refresh_is_scheduled_before_expiry(self) -> None:
        """Rule 2. Refreshing AT expiry means a slightly fast clock presents a
        token the server has already retired."""
        p = SessionProvider(endpoint="https://acme.example/session", fetch=responder(TOKEN))
        s = p.current()
        assert s is not None
        remaining = s.expires_at - time.monotonic()
        refresh_in = s.refresh_at - time.monotonic()
        assert refresh_in < remaining
        assert refresh_in <= 3600 * REFRESH_AT + 1

    def test_jitter_only_moves_refresh_earlier(self) -> None:
        """Adding jitter would let some machines refresh AFTER expiry.

        Ten thousand laptops opened at 09:00 must not all refresh at 12:00, but
        spreading them later would be worse than not spreading them at all.
        """
        earliest = 3600 * REFRESH_AT * (1 - JITTER)
        for _ in range(200):
            p = SessionProvider(
                endpoint="https://acme.example/session", fetch=responder(TOKEN)
            )
            s = p.current()
            assert s is not None
            refresh_in = s.refresh_at - time.monotonic()
            assert earliest - 1 <= refresh_in <= 3600 * REFRESH_AT + 1

    def test_deadlines_are_monotonic_not_wall_clock(self) -> None:
        """A laptop's wall clock steps on NTP correction and on sleep/wake. A
        token's remaining life must not jump when it does."""
        p = SessionProvider(endpoint="https://acme.example/session", fetch=responder(TOKEN))
        s = p.current()
        assert s is not None
        # Monotonic values are small uptimes, never unix timestamps.
        assert s.expires_at < 10**9


class TestRefusals:
    def test_a_plaintext_endpoint_is_refused(self) -> None:
        """Worse than the static key it replaces: it hands the token to the
        network on every fetch."""
        p = SessionProvider(endpoint="http://acme.example/session", fetch=responder(TOKEN))
        assert p.current() is None

    def test_localhost_over_http_is_allowed_for_development(self) -> None:
        p = SessionProvider(endpoint="http://127.0.0.1:9000/session", fetch=responder(TOKEN))
        assert p.current() is not None

    def test_a_response_without_a_token_is_refused(self) -> None:
        p = SessionProvider(
            endpoint="https://acme.example/session",
            fetch=responder({"expires_in": 3600}),
        )
        assert p.current() is None

    def test_a_response_without_expiry_is_refused(self) -> None:
        """An absent TTL is not "never expires" -- it is a broken endpoint."""
        p = SessionProvider(
            endpoint="https://acme.example/session", fetch=responder({"token": "t"})
        )
        assert p.current() is None

    def test_an_unconfigured_provider_is_inert(self) -> None:
        p = SessionProvider(endpoint="", fetch=responder(TOKEN))
        assert not p.configured
        assert p.current() is None


class TestInvalidate:
    def test_invalidate_drops_the_token_without_fetching(self) -> None:
        """Called from the export failure path. A synchronous fetch there turns
        one rejected batch into a stalled exporter thread."""
        calls = []

        def fetch(endpoint: str, headers: dict[str, str]) -> dict:
            calls.append(1)
            return dict(TOKEN)

        p = SessionProvider(endpoint="https://acme.example/session", fetch=fetch)
        p.current()
        assert len(calls) == 1
        p.invalidate()
        assert p._session is None
        assert len(calls) == 1


class TestHeaderParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("a=1,b=2", {"a": "1", "b": "2"}),
            (" a = 1 ", {"a": "1"}),
            ("", {}),
            ("garbage", {}),
        ],
    )
    def test_otel_header_format(self, raw: str, expected: dict[str, str]) -> None:
        """The same syntax as OTEL_EXPORTER_OTLP_HEADERS, which sits beside it in
        the customer's config. Two syntaxes for one idea is how one gets written
        wrong."""
        from mcpobs.session import _parse_headers

        assert _parse_headers(raw) == expected
