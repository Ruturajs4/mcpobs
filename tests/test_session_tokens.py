"""Session tokens (ADR-011).

These are credentials that will sit on end users' laptops, so most of what
matters is what the verifier REFUSES. The happy path is two assertions; the
rest of this file is attacks that must fail.
"""

from __future__ import annotations

import os
import time

import jwt
import pytest

os.environ.setdefault("MCPOBS_SESSION_SIGNING_KEY", "k" * 40)

from control.keys import INGEST, READ
from control.models import Principal
from control.session import (
    ALGORITHM,
    AUDIENCE_INGEST,
    ISSUER,
    MAX_TTL_SECONDS,
    SessionError,
    clean_attributes,
    is_session_token,
    mint,
    principal_for,
    verify,
)

SIGNING_KEY = "k" * 40


def customer_key(**kw: object) -> Principal:
    """The customer's own server-side key -- what mints sessions."""
    defaults = dict(
        key_id=1,
        org_id=7,
        tenant="acme",
        project="prod",
        environment="live",
        scopes=(INGEST, READ),
        plan="growth",
    )
    defaults.update(kw)
    return Principal(**defaults)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_a_minted_token_verifies(self) -> None:
        token, ttl = mint(customer_key(), subject="u_931")
        claims = verify(token)
        assert claims.tenant == "acme"
        assert claims.project == "prod"
        assert claims.subject == "u_931"
        assert ttl == 3 * 60 * 60

    def test_attributes_survive_the_round_trip(self) -> None:
        token, _ = mint(
            customer_key(), subject="u_931", attributes={"user_id": "u_931", "workspace": "eu"}
        )
        assert verify(token).attributes == {"user_id": "u_931", "workspace": "eu"}

    def test_jti_and_epoch_are_present_before_anything_reads_them(self) -> None:
        """The claims that make revocation possible later.

        A blacklist can only reject a token it can name, and revocation gets
        built exactly when it is urgently needed. Tokens minted without these
        would be permanently unrevocable, so they ship unused.
        """
        claims = verify(mint(customer_key(), subject="u_931")[0])
        assert claims.jti
        assert claims.epoch == 0


class TestForgeryIsRefused:
    def test_the_none_algorithm_is_rejected(self) -> None:
        """The classic JWT break: a token that asks to be verified with nothing."""
        forged = jwt.encode(
            {
                "iss": ISSUER, "aud": AUDIENCE_INGEST, "sub": "attacker",
                "tenant": "victim", "project": "prod",
                "iat": int(time.time()), "exp": int(time.time()) + 3600,
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(SessionError):
            verify(forged)

    def test_a_token_signed_with_another_key_is_rejected(self) -> None:
        forged = jwt.encode(
            {
                "iss": ISSUER, "aud": AUDIENCE_INGEST, "sub": "attacker",
                "tenant": "victim", "project": "prod",
                "iat": int(time.time()), "exp": int(time.time()) + 3600,
            },
            key="a-different-key-that-is-long-enough-to-sign",
            algorithm=ALGORITHM,
        )
        with pytest.raises(SessionError):
            verify(forged)

    def test_a_token_for_another_audience_is_rejected(self) -> None:
        """An ingest token must not be usable against the query API.

        Without this, a credential that can only WRITE telemetry could READ the
        org's data by being pointed at a different host -- which would collapse
        the ingest/read separation the key model deliberately maintains.
        """
        other = jwt.encode(
            {
                "iss": ISSUER, "aud": "mcpobs:query", "sub": "u_931",
                "tenant": "acme", "project": "prod",
                "iat": int(time.time()), "exp": int(time.time()) + 3600,
            },
            key=SIGNING_KEY,
            algorithm=ALGORITHM,
        )
        with pytest.raises(SessionError):
            verify(other)

    def test_an_expired_token_is_rejected(self) -> None:
        past = int(time.time()) - 7200
        stale = jwt.encode(
            {
                "iss": ISSUER, "aud": AUDIENCE_INGEST, "sub": "u_931",
                "tenant": "acme", "project": "prod",
                "iat": past, "exp": past + 60,
            },
            key=SIGNING_KEY,
            algorithm=ALGORITHM,
        )
        with pytest.raises(SessionError, match="expired"):
            verify(stale)

    def test_a_token_from_another_issuer_is_rejected(self) -> None:
        other = jwt.encode(
            {
                "iss": "somebody-else", "aud": AUDIENCE_INGEST, "sub": "u_931",
                "tenant": "acme", "project": "prod",
                "iat": int(time.time()), "exp": int(time.time()) + 3600,
            },
            key=SIGNING_KEY,
            algorithm=ALGORITHM,
        )
        with pytest.raises(SessionError):
            verify(other)

    def test_failures_are_indistinguishable(self) -> None:
        """One message for every failure except expiry.

        Telling a prober that their token was well-formed but for the wrong
        audience, rather than simply forged, narrows their search for them.
        """
        messages = set()
        for bad in ("not-a-token", "a.b.c"):
            with pytest.raises(SessionError) as exc:
                verify(bad)
            messages.add(str(exc.value))
        assert messages == {"invalid session token"}


class TestScopeAndTenancy:
    def test_a_session_can_only_ingest(self) -> None:
        """Whatever the minting key could do.

        A leaked session token must be able to write telemetry for its lifetime
        and nothing else. The minting key here holds READ as well.
        """
        claims = verify(mint(customer_key(scopes=(INGEST, READ)), subject="u_931")[0])
        principal = principal_for(claims)
        assert principal.scopes == (INGEST,)
        assert principal.can(INGEST)
        assert not principal.can(READ)

    def test_tenancy_comes_from_the_minting_key_not_a_parameter(self) -> None:
        token, _ = mint(customer_key(tenant="acme", project="prod"), subject="u_931")
        claims = verify(token)
        assert (claims.tenant, claims.project) == ("acme", "prod")

    def test_quota_defaults_closed_not_open(self) -> None:
        """Failing open on a quota is how one laptop bills an organisation."""
        claims = verify(mint(customer_key(), subject="u_931")[0])
        assert principal_for(claims).plan == "trial"


class TestLifetime:
    def test_a_long_ttl_is_clamped_not_honoured(self) -> None:
        """A client-chosen lifetime is a client choosing its own exposure window."""
        _, ttl = mint(customer_key(), subject="u_931", ttl_seconds=30 * 24 * 3600)
        assert ttl == MAX_TTL_SECONDS

    def test_a_shorter_ttl_is_allowed(self) -> None:
        _, ttl = mint(customer_key(), subject="u_931", ttl_seconds=600)
        assert ttl == 600

    def test_a_non_positive_ttl_is_refused(self) -> None:
        with pytest.raises(SessionError):
            mint(customer_key(), subject="u_931", ttl_seconds=0)


class TestAttributes:
    def test_unknown_attributes_are_refused_not_dropped(self) -> None:
        """Silently dropping `email` would be discovered days later, in the data."""
        with pytest.raises(SessionError, match="email"):
            clean_attributes({"email": "someone@example.com"})

    def test_oversized_attributes_are_refused(self) -> None:
        """They ride on every request, forever."""
        with pytest.raises(SessionError, match="bytes"):
            clean_attributes({"user_name": "x" * 900})

    def test_unknown_attributes_are_stripped_on_the_way_out_too(self) -> None:
        """The signature proves the payload was ours, not that a past version of
        this code enforced the rules the current one relies on."""
        token = jwt.encode(
            {
                "iss": ISSUER, "aud": AUDIENCE_INGEST, "sub": "u_931",
                "tenant": "acme", "project": "prod",
                "iat": int(time.time()), "exp": int(time.time()) + 3600,
                "attr": {"user_id": "u_931", "email": "leaked@example.com"},
            },
            key=SIGNING_KEY,
            algorithm=ALGORITHM,
        )
        assert verify(token).attributes == {"user_id": "u_931"}


class TestRouting:
    def test_an_api_key_is_not_mistaken_for_a_session_token(self) -> None:
        assert not is_session_token("mcpo_local_abc_def")
        assert not is_session_token(None)
        assert not is_session_token("")

    def test_a_jwt_is_recognised(self) -> None:
        assert is_session_token(mint(customer_key(), subject="u_931")[0])


class TestSigningKey:
    def test_a_short_signing_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PyJWT warns and signs anyway; a warning in a log is not a control.

        Forging this token forges tenancy, so a brute-forceable key is not a
        configuration preference.
        """
        monkeypatch.setenv("MCPOBS_SESSION_SIGNING_KEY", "short")
        with pytest.raises(SessionError, match="shorter than"):
            mint(customer_key(), subject="u_931")

    def test_a_missing_signing_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never a development default -- it would ship the day someone forgot."""
        monkeypatch.setenv("MCPOBS_SESSION_SIGNING_KEY", "")
        with pytest.raises(SessionError, match="not configured"):
            mint(customer_key(), subject="u_931")


class TestAttributesReachTheSpan:
    """The promise the customer documentation makes.

    It said attributes are "stamped onto every span from the token", and they
    were not: `Principal` had no field for them, so `principal_for()` validated
    them at mint, re-filtered them on verify, and then dropped them. Everything
    about the feature worked except the part a customer would see.

    Documentation ahead of implementation is the worst kind, because it is a
    promise someone builds on. This asserts the whole path, not the pieces.
    """

    def test_a_session_principal_carries_its_attributes(self) -> None:
        claims = verify(
            mint(
                customer_key(),
                subject="u_931",
                attributes={"user_id": "u_931", "workspace": "eu"},
            )[0]
        )
        principal = principal_for(claims)
        assert dict(principal.session_attributes) == {
            "user_id": "u_931",
            "workspace": "eu",
        }

    def test_an_api_key_principal_carries_none(self) -> None:
        """Only session tokens bind attributes; an ordinary key must not gain a
        field full of someone else's user ids."""
        assert customer_key().session_attributes == ()

    def test_the_gateway_stamps_them_as_trusted(self) -> None:
        """They join the TRUSTED set, which is the entire point of binding them
        to the credential: a value the caller sent under the same key is dropped
        with the rest, so a user cannot attribute traffic to somebody else."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

        from ingest.app import SESSION_ATTRIBUTE_PREFIX, stamp_parsed

        claims = verify(
            mint(customer_key(), subject="u_931", attributes={"user_id": "u_931"})[0]
        )
        principal = principal_for(claims)

        request = ExportTraceServiceRequest()
        resource_spans = request.resource_spans.add()
        # The client claims someone else's id under the same key.
        resource_spans.resource.attributes.append(
            KeyValue(
                key=f"{SESSION_ATTRIBUTE_PREFIX}user_id",
                value=AnyValue(string_value="somebody-else"),
            )
        )

        stamped = ExportTraceServiceRequest()
        stamped.ParseFromString(stamp_parsed(request, principal))
        values = {
            kv.key: kv.value.string_value
            for kv in stamped.resource_spans[0].resource.attributes
        }
        assert values[f"{SESSION_ATTRIBUTE_PREFIX}user_id"] == "u_931"
        assert "somebody-else" not in values.values()
