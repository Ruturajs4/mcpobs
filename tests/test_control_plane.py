"""Auth, tenancy and the archive (DF-9).

These run WITHOUT Postgres, MinIO or Kafka. Everything here is either pure
(key minting, stamping, archive framing) or driven through a fake, because a
security boundary that is only checked when the whole stack is up is a boundary
that stops being checked the first time someone is in a hurry. The end-to-end
versions live in `scripts/verify.py` as the F-series.
"""

from __future__ import annotations

from typing import Any

from control import keys
from control.models import Principal


class TestKeyMinting:
    def test_a_key_carries_its_environment(self) -> None:
        """The single most common ingest mistake is pointing a staging server at
        production. A key that reads `mcpo_production_...` in a staging config
        is visible to a human reading a diff."""
        token, _, _ = keys.mint("staging")
        assert token.startswith("mcpo_staging_")

    def test_the_secret_is_never_stored(self) -> None:
        token, prefix, secret_hash = keys.mint("production")
        _, secret = keys.split(token)  # type: ignore[misc]
        assert secret not in secret_hash
        assert prefix in token
        assert keys.verify(secret, secret_hash)

    def test_a_wrong_secret_fails_against_a_valid_prefix(self) -> None:
        _, _, secret_hash = keys.mint("production")
        assert not keys.verify("not-the-secret", secret_hash)

    def test_split_handles_secrets_containing_underscores(self) -> None:
        """`token_urlsafe` emits `_`, and the format is underscore-separated.
        Splitting on the last delimiter instead of rejoining the tail would
        truncate roughly one key in two -- intermittently, which is worse than
        never."""
        token = "mcpo_production_abcd1234_aa_bb_cc"
        assert keys.split(token) == ("abcd1234", "aa_bb_cc")

    def test_malformed_tokens_return_none_rather_than_raising(self) -> None:
        """These arrive from the internet constantly. An unauthenticated request
        is ordinary, not exceptional.

        Written without an `or` escape clause on purpose: the first version had
        one, which made the assertion pass for a case it did not actually
        cover. A test that can pass two ways checks neither.
        """
        for token in (
            "",                       # nothing
            "garbage",                # no separators
            "mcpo_short",             # too few segments
            "bearer mcpo_a_b_c",      # header not stripped by the caller
            "other_production_ab_cd",  # not our brand
            "mcpo_production__secret",  # empty prefix
            "mcpo_production_abcd_",   # empty secret
        ):
            assert keys.split(token) is None, token

    def test_unknown_scopes_are_dropped_not_passed_through(self) -> None:
        """A scope string is data. Data reaching an authorisation check must
        never be able to grant something the code does not recognise."""
        assert keys.parse_scopes("ingest,admin,superuser,read") == ("ingest", "read")

    def test_invite_codes_are_hashed_like_keys(self) -> None:
        """An invite code is a bearer credential: whoever holds it becomes a
        member of someone's organisation."""
        code, code_hash = keys.mint_invite()
        assert code not in code_hash
        assert keys.hash_invite(code) == code_hash


class TestTrustedStamping:
    """The write-side security boundary (Architecture.md §5.1)."""

    def _payload(self, claimed_tenant: str) -> bytes:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
        from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans

        request = ExportTraceServiceRequest()
        resource_spans = ResourceSpans()
        resource_spans.resource.attributes.append(
            KeyValue(key="tenant.id", value=AnyValue(string_value=claimed_tenant))
        )
        resource_spans.resource.attributes.append(
            KeyValue(key="service.name", value=AnyValue(string_value="theirs"))
        )
        request.resource_spans.append(resource_spans)
        return request.SerializeToString()

    def _attributes(self, payload: bytes) -> dict[str, str]:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        request = ExportTraceServiceRequest()
        request.ParseFromString(payload)
        return {
            kv.key: kv.value.string_value
            for kv in request.resource_spans[0].resource.attributes
        }

    @property
    def principal(self) -> Principal:
        return Principal(
            key_id=1, org_id=1, tenant="acme", project="default",
            environment="production", scopes=("ingest",),
        )

    def test_a_claimed_tenant_is_overwritten(self) -> None:
        """THE test. A customer must not be able to write telemetry into another
        tenant by setting a resource attribute."""
        from ingest.app import stamp

        stamped = stamp(self._payload("globex"), self.principal)
        assert self._attributes(stamped)["tenant.id"] == "acme"

    def test_exactly_one_tenant_attribute_survives(self) -> None:
        """Appending without removing would leave two `tenant.id` entries and
        make the winner depend on whichever consumer read last -- which is a
        coin toss, not a boundary."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        from ingest.app import stamp

        request = ExportTraceServiceRequest()
        request.ParseFromString(stamp(self._payload("globex"), self.principal))
        keys_seen = [kv.key for kv in request.resource_spans[0].resource.attributes]
        assert keys_seen.count("tenant.id") == 1

    def test_the_customer_s_own_attributes_are_preserved(self) -> None:
        """Only the three trusted keys are ours. Dropping the rest would throw
        away `service.name` and every other thing the console renders."""
        from ingest.app import stamp

        attributes = self._attributes(stamp(self._payload("globex"), self.principal))
        assert attributes["service.name"] == "theirs"

    def test_an_empty_payload_still_stamps_nothing_wrong(self) -> None:
        from ingest.app import stamp

        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # isort: skip
            ExportTraceServiceRequest,
        )

        stamped = stamp(ExportTraceServiceRequest().SerializeToString(), self.principal)
        request = ExportTraceServiceRequest()
        request.ParseFromString(stamped)
        assert list(request.resource_spans) == []


class TestAuthenticationCache:
    """The cache must not become a way in."""

    def _plane(self, row: dict[str, Any] | None) -> Any:
        from control.repository import ControlPlane

        plane = ControlPlane.__new__(ControlPlane)
        plane._cache = {}
        plane._conn = None
        plane.dsn = ""
        plane._one = lambda *_a, **_k: row  # type: ignore[method-assign]
        return plane

    def test_a_valid_prefix_with_a_wrong_secret_is_refused_when_cached(self) -> None:
        """Caching only the principal would have let ANY secret through once a
        valid prefix was warm. The hash is cached alongside it and the secret is
        re-verified on every hit."""
        token, prefix, secret_hash = keys.mint("production")
        row = {
            "key_id": 1, "org_id": 1, "secret_hash": secret_hash, "scopes": "ingest",
            "expires_at": None, "revoked_at": None,
            "tenant": "acme", "project": "default", "environment": "production",
        }
        plane = self._plane(row)
        assert plane.authenticate(token) is not None          # populates the cache
        forged = f"mcpo_production_{prefix}_not-the-secret"
        assert plane.authenticate(forged) is None             # served from cache

    def test_a_revoked_key_is_refused(self) -> None:
        from datetime import UTC, datetime

        token, _, secret_hash = keys.mint("production")
        plane = self._plane({
            "key_id": 1, "org_id": 1, "secret_hash": secret_hash, "scopes": "ingest",
            "expires_at": None, "revoked_at": datetime.now(UTC),
            "tenant": "acme", "project": "default", "environment": "production",
        })
        assert plane.authenticate(token) is None

    def test_an_expired_key_is_refused(self) -> None:
        from datetime import UTC, datetime, timedelta

        token, _, secret_hash = keys.mint("production")
        plane = self._plane({
            "key_id": 1, "org_id": 1, "secret_hash": secret_hash, "scopes": "ingest",
            "expires_at": datetime.now(UTC) - timedelta(days=1), "revoked_at": None,
            "tenant": "acme", "project": "default", "environment": "production",
        })
        assert plane.authenticate(token) is None

    def test_an_unknown_key_is_refused_and_negatively_cached(self) -> None:
        """Negative caching keeps a key-spraying attacker from turning
        authentication into a denial of service against Postgres."""
        plane = self._plane(None)
        token, prefix, _ = keys.mint("production")
        assert plane.authenticate(token) is None
        assert plane._cache[prefix][2] is None

    def test_scopes_gate_what_a_key_can_do(self) -> None:
        principal = Principal(
            key_id=1, org_id=1, tenant="acme", project="default",
            environment="production", scopes=("ingest",),
        )
        assert principal.can("ingest")
        assert not principal.can("read")


class TestArchiveFormat:
    def test_framing_survives_a_round_trip(self) -> None:
        from archiver.archiver import frame, unframe

        payloads = [b"", b"one", b"\x00\x01\x02", b"x" * 5000]
        assert unframe(frame(payloads)) == payloads

    def test_concatenation_alone_would_have_lost_the_boundaries(self) -> None:
        """Protobuf is not self-delimiting: two concatenated
        ExportTraceServiceRequests parse as ONE merged message, silently. The
        archive would still look valid and would no longer contain the Kafka
        messages it claims to."""
        from archiver.archiver import frame, unframe

        assert len(unframe(frame([b"aa", b"bb"]))) == 2

    def test_keys_are_deterministic_so_a_retry_overwrites(self) -> None:
        """The put happens before the offset commit. A crash in between
        re-archives the same messages, and the key must collide rather than
        create a second copy."""
        from datetime import UTC, datetime

        from archiver.archiver import archive_key

        when = datetime(2026, 8, 15, 17, 30, tzinfo=UTC)
        assert archive_key("acme", when, 3, 10, 99) == archive_key("acme", when, 3, 10, 99)

    def test_the_key_puts_the_tenant_first(self) -> None:
        """So erasing one customer is a prefix delete rather than a full scan."""
        from datetime import UTC, datetime

        from archiver.archiver import archive_key

        assert archive_key("acme", datetime(2026, 8, 15, tzinfo=UTC), 0, 0, 1).startswith("acme/")

    def test_two_tenants_never_share_an_object(self) -> None:
        """A shared object would make per-tenant deletion a rewrite, and would
        hand anyone with read access to one prefix the contents of another."""
        from archiver.archiver import ArchiveBatch

        batch = ArchiveBatch(max_messages=10, max_seconds=60, max_bytes=1 << 20)
        batch.add("acme", b"a", partition=0, offset=1)
        batch.add("globex", b"b", partition=0, offset=2)
        assert set(batch.groups) == {("acme", 0), ("globex", 0)}

    def test_one_object_per_tenant_and_partition(self) -> None:
        """The first version looped partitions x tenants at flush time and wrote
        every tenant's messages once per partition, silently duplicating the
        archive."""
        from archiver.archiver import ArchiveBatch

        batch = ArchiveBatch(max_messages=10, max_seconds=60, max_bytes=1 << 20)
        for partition in (0, 1):
            for offset in (5, 6):
                batch.add("acme", b"x", partition=partition, offset=offset)
        assert len(batch.groups) == 2
        assert batch.groups[("acme", 0)][1:] == (5, 6)
