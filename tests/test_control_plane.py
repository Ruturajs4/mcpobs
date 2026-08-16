"""Auth, tenancy and the archive (DF-9).

These run WITHOUT Postgres, MinIO or Kafka. Everything here is either pure
(key minting, stamping, archive framing) or driven through a fake, because a
security boundary that is only checked when the whole stack is up is a boundary
that stops being checked the first time someone is in a hurry. The end-to-end
versions live in `scripts/verify.py` as the F-series.
"""

from __future__ import annotations

import contextlib
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
        never be able to grant something the code does not recognise.

        This used `admin` as its example of an unknown scope, and broke the day
        `admin` became a real one -- correctly. A test that names a specific
        invalid value has to be revisited when the valid set grows, which is the
        point of naming it rather than asserting a count.
        """
        assert keys.parse_scopes("ingest,superuser,root,read") == ("ingest", "read")
        assert keys.parse_scopes("ingest,admin,read") == ("ingest", "admin", "read")

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
            "key_id": 1, "prefix": prefix, "org_id": 1,
            "secret_hash": secret_hash, "scopes": "ingest",
            "expires_at": None, "revoked_at": None,
            "tenant": "acme", "project": "default", "environment": "production",
            # The auth query selects these too. Kept in the fixture rather
            # than defaulted in the repository: a missing column in the REAL
            # query is a bug worth crashing on, not one to paper over.
            "plan": "trial", "quota_spans_per_minute": None,
            "quota_spans_per_day": None,
        }
        plane = self._plane(row)
        assert plane.authenticate(token) is not None          # populates the cache
        forged = f"mcpo_production_{prefix}_not-the-secret"
        assert plane.authenticate(forged) is None             # served from cache

    def test_a_revoked_key_is_refused(self) -> None:
        from datetime import UTC, datetime

        token, prefix, secret_hash = keys.mint("production")
        plane = self._plane({
            "key_id": 1, "prefix": prefix, "org_id": 1,
            "secret_hash": secret_hash, "scopes": "ingest",
            "expires_at": None, "revoked_at": datetime.now(UTC),
            "tenant": "acme", "project": "default", "environment": "production",
            # The auth query selects these too. Kept in the fixture rather
            # than defaulted in the repository: a missing column in the REAL
            # query is a bug worth crashing on, not one to paper over.
            "plan": "trial", "quota_spans_per_minute": None,
            "quota_spans_per_day": None,
        })
        assert plane.authenticate(token) is None

    def test_an_expired_key_is_refused(self) -> None:
        from datetime import UTC, datetime, timedelta

        token, prefix, secret_hash = keys.mint("production")
        plane = self._plane({
            "key_id": 1, "prefix": prefix, "org_id": 1,
            "secret_hash": secret_hash, "scopes": "ingest",
            "expires_at": datetime.now(UTC) - timedelta(days=1), "revoked_at": None,
            "tenant": "acme", "project": "default", "environment": "production",
            # The auth query selects these too. Kept in the fixture rather
            # than defaulted in the repository: a missing column in the REAL
            # query is a bug worth crashing on, not one to paper over.
            "plan": "trial", "quota_spans_per_minute": None,
            "quota_spans_per_day": None,
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


class TestDownstreamInstrumentation:
    """`instrument_downstream()` -- entry-point discovery, opt-in.

    The alternative for customers who cannot use `opentelemetry-instrument`,
    which is a large share of MCP servers: they are spawned by the client over
    stdio, so they do not own the command line that starts them.
    """

    def test_it_discovers_rather_than_lists(self) -> None:
        """A hardcoded list is a list we would forget to update. Reading the
        entry-point group means `pip install
        opentelemetry-instrumentation-redis` is the whole integration."""
        from mcpobs import available

        names = available()
        assert "httpx" in names
        assert "sqlite3" in names

    def test_it_returns_a_report_rather_than_none(self) -> None:
        """A call that patches an unknown set of libraries and says nothing is
        not something anyone should put in a production server."""
        from mcpobs import instrument_downstream

        report = instrument_downstream()
        assert set(report) == set(__import__("mcpobs").available())
        assert all(isinstance(v, str) for v in report.values())

    def test_exclusions_are_honoured(self) -> None:
        from mcpobs import instrument_downstream

        report = instrument_downstream(exclude=("sqlite3",))
        assert report["sqlite3"] == "skipped: excluded"

    def test_one_broken_instrumentor_cannot_stop_the_others(self) -> None:
        """An observability library that prevents a server from booting has done
        more damage than the telemetry was worth."""
        from mcpobs import downstream

        class Exploding:
            name = "exploding"

            def load(self) -> Any:
                raise RuntimeError("no such module")

        real = downstream.entry_points
        downstream.entry_points = lambda group: [Exploding(), *real(group=group)]  # type: ignore[assignment]
        try:
            report = downstream.instrument_downstream()
        finally:
            downstream.entry_points = real  # type: ignore[assignment]
        assert "will not load" in report["exploding"]
        assert report["sqlite3"] in ("instrumented", "already instrumented")


class TestInstrumentationOrderIndependence:
    """Body capture must survive `instrument_downstream()`, in either order.

    Asserted with a REAL request over a real socket, because the claim is about
    what the instrumentation actually patched. Checking an internal
    `is_instrumented_by_opentelemetry` flag would only confirm that our own
    guard ran -- which is not the question. A customer will pick the wrong order
    half the time and nothing should depend on it.
    """

    def _serve(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("content-length", 0)))
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _spans_for(self, order: str) -> list[Any]:
        import asyncio

        import httpx
        from opentelemetry import trace
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        from opentelemetry.util._once import Once

        from mcpobs import instrument_downstream, instrument_httpx

        HTTPXClientInstrumentor().uninstrument()
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Resetting `_TRACER_PROVIDER` alone is not enough: OTel guards
        # `set_tracer_provider` with a `Once`, so the second call in a process
        # is silently ignored and the span lands in the FIRST test's exporter.
        # The symptom is an empty span list here and a passing sibling test,
        # which reads exactly like a product bug and is not one.
        # The RAW module global, not `get_tracer_provider()`. When nothing is
        # set, that call returns the ProxyTracerProvider, and restoring the
        # proxy INTO `_TRACER_PROVIDER` makes it delegate to itself -- a
        # RecursionError 900 frames deep inside the MCP SDK, which looks like
        # anything except a test-teardown bug.
        previous = trace._TRACER_PROVIDER
        previous_once = trace._TRACER_PROVIDER_SET_ONCE
        trace._TRACER_PROVIDER = None
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace.set_tracer_provider(provider)

        server = self._serve()
        try:
            if order == "downstream-first":
                instrument_downstream()
                instrument_httpx()
            else:
                instrument_httpx()
                instrument_downstream()

            url = f"http://127.0.0.1:{server.server_address[1]}/x"

            async def call() -> None:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(url, json={"q": 1})

            asyncio.run(call())
            return list(exporter.get_finished_spans())
        finally:
            server.shutdown()
            trace._TRACER_PROVIDER = previous
            trace._TRACER_PROVIDER_SET_ONCE = previous_once

    def test_body_capture_survives_downstream_first(self) -> None:
        from mcpobs.http import REQUEST_BODY_ATTRIBUTE

        spans = self._spans_for("downstream-first")
        assert spans, "no HTTP span emitted at all"
        assert REQUEST_BODY_ATTRIBUTE in (spans[0].attributes or {})

    def test_body_capture_survives_httpx_first(self) -> None:
        """The order that would break if `instrument_downstream()` blindly
        re-instrumented: the second call would replace the hooked
        instrumentation with an unhooked one and the bodies would vanish."""
        from mcpobs.http import REQUEST_BODY_ATTRIBUTE

        spans = self._spans_for("httpx-first")
        assert spans, "no HTTP span emitted at all"
        assert REQUEST_BODY_ATTRIBUTE in (spans[0].attributes or {})


class TestStreamingVisibility:
    """DF-20 and DF-21: long-running work must report while it is running.

    Both had one root cause -- a span is exported when it ENDS, so anything
    modelled as one long-lived span says nothing until it is over. Child spans
    are exported as soon as THEY end, while the parent is still open.
    """

    def teardown_method(self) -> None:
        """RESTORE the global provider.

        Without this these tests leaked their exporter into every test that ran
        afterwards, and `test_span_carries_the_failure_kind` failed while
        passing in isolation -- which reads as a product bug and is not one.
        """
        from opentelemetry import trace

        previous = getattr(self, "_previous_provider", None)
        if previous is not None:
            trace._TRACER_PROVIDER, trace._TRACER_PROVIDER_SET_ONCE = previous

    def _recorded(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
        from opentelemetry.util._once import Once

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        if not hasattr(self, "_previous_provider"):
            self._previous_provider = (
                trace._TRACER_PROVIDER, trace._TRACER_PROVIDER_SET_ONCE
            )
        trace._TRACER_PROVIDER = None
        trace._TRACER_PROVIDER_SET_ONCE = Once()
        trace.set_tracer_provider(provider)
        import mcpobs.streaming as streaming

        streaming._tracer = trace.get_tracer("mcpobs.streaming")
        return exporter, streaming

    def test_a_progress_report_becomes_its_own_span(self) -> None:
        exporter, streaming = self._recorded()
        with streaming._tracer.start_as_current_span("tools/call slow"):
            streaming._emit_progress(3, 12, "exported 3/12")
            # Asserted BEFORE the parent closes: that is the entire point.
            # `add_event()` would have put this on the parent, where it would
            # not be exported until the parent ended -- the moment that is
            # already too late.
            spans = [s for s in exporter.get_finished_spans() if s.name == "mcp.progress"]
            assert len(spans) == 1
            assert spans[0].attributes["mcp.progress.percent"] == 25.0
            assert spans[0].attributes["mcp.progress.message"] == "exported 3/12"

    def test_the_progress_span_is_a_child_of_the_running_call(self) -> None:
        exporter, streaming = self._recorded()
        with streaming._tracer.start_as_current_span("tools/call slow") as parent:
            streaming._emit_progress(1, 2, None)
            child = next(s for s in exporter.get_finished_spans() if s.name == "mcp.progress")
            assert child.parent.span_id == parent.get_span_context().span_id

    def test_progress_outside_a_span_is_dropped_rather_than_orphaned(self) -> None:
        """A parentless progress span would be a trace of one event with no
        context -- noise that costs storage and answers nothing."""
        exporter, streaming = self._recorded()
        streaming._emit_progress(1, 2, None)
        assert not [s for s in exporter.get_finished_spans() if s.name == "mcp.progress"]

    def test_emission_is_capped_and_says_so(self) -> None:
        """One span per report is fine at three reports and a denial of service
        at three million. A stream that just stops looks like the tool
        stopping, which is a worse lie than admitting the cap."""
        exporter, streaming = self._recorded()
        streaming._counter = streaming._ProgressCounter()
        with streaming._tracer.start_as_current_span("tools/call busy"):
            for i in range(streaming.MAX_PROGRESS_SPANS + 50):
                streaming._emit_progress(i, None, None)
            spans = [s for s in exporter.get_finished_spans() if s.name == "mcp.progress"]
        assert len(spans) == streaming.MAX_PROGRESS_SPANS + 1
        assert spans[-1].attributes["mcp.progress.truncated"] is True

    async def test_the_bus_wrapper_publishes_first_and_observes_second(self) -> None:
        """Delivery is the customer's function; telemetry is ours. Ours must
        never delay or fail theirs."""
        exporter, streaming = self._recorded()
        order: list[str] = []

        class Bus:
            async def publish(self, event: object) -> str:
                order.append("delivered")
                return "ok"

        bus = streaming.ObservedSubscriptionBus(Bus())
        assert await bus.publish(object()) == "ok"
        assert order == ["delivered"]
        assert [s.name for s in exporter.get_finished_spans()] == ["mcp.subscription.event"]

    async def test_a_failing_span_never_breaks_delivery(self) -> None:
        _, streaming = self._recorded()

        class Bus:
            async def publish(self, event: object) -> str:
                return "delivered"

        bus = streaming.ObservedSubscriptionBus(Bus())
        streaming._tracer = None  # type: ignore[assignment]
        try:
            assert await bus.publish(object()) == "delivered"
        finally:
            self._recorded()

    def test_unknown_bus_methods_pass_through(self) -> None:
        """A wrapper that has to enumerate its subject's API breaks on the next
        SDK release."""
        _, streaming = self._recorded()

        class Bus:
            def subscribe(self, fn: object) -> str:
                return "subscribed"

        assert streaming.ObservedSubscriptionBus(Bus()).subscribe(None) == "subscribed"


class TestQuotas:
    """Soft flags and hard rejections (Architecture.md 5.1, ADR-008)."""

    def enforcer(self):
        from control.quota import QuotaEnforcer, QuotaStore

        class Store(QuotaStore):
            def __init__(self) -> None:
                self._local = {}
                self.degraded = False
                self.url = ""

            def incr(self, key: str, amount: int, ttl: int) -> int:
                return self._incr_local(key, amount, ttl)

        return QuotaEnforcer(store=Store())

    def test_under_the_limit_is_allowed_and_unflagged(self) -> None:
        verdict = self.enforcer().check("acme", "trial", spans=10)
        assert verdict.allowed and not verdict.soft_exceeded

    def test_the_soft_threshold_fires_before_anything_is_refused(self) -> None:
        """A flag raised at 100% would arrive with the rejection and tell the
        customer nothing they were not about to find out anyway."""
        verdict = self.enforcer().check("acme", "trial", spans=1_700)  # 85% of 2000
        assert verdict.allowed and verdict.soft_exceeded

    def test_over_the_minute_limit_is_refused(self) -> None:
        enforcer = self.enforcer()
        enforcer.check("acme", "trial", spans=2_000)
        verdict = enforcer.check("acme", "trial", spans=1)
        assert not verdict.allowed
        assert "rate limit" in verdict.reason
        assert 0 < verdict.retry_after <= 60

    def test_the_daily_limit_catches_what_no_single_minute_would(self) -> None:
        """Per-minute and per-day limits catch different things; neither
        subsumes the other."""
        verdict = self.enforcer().check(
            "acme", "trial", spans=200_001, override_minute=0
        )
        assert not verdict.allowed
        assert "daily volume" in verdict.reason

    def test_spans_are_metered_not_requests(self) -> None:
        """Metering requests would let a customer send the same volume in a
        hundredth of the calls and stay under any limit."""
        enforcer = self.enforcer()
        assert enforcer.check("acme", "trial", spans=1_999).allowed
        assert not enforcer.check("acme", "trial", spans=2).allowed

    def test_a_rejected_burst_is_still_counted(self) -> None:
        """Counting only what was accepted would make a rejected flood
        invisible, so the tenant being rejected would look quiet to whoever
        went looking for the cause."""
        enforcer = self.enforcer()
        enforcer.check("acme", "trial", spans=5_000)
        assert enforcer.check("acme", "trial", spans=1).used_minute > 5_000

    def test_tenants_are_metered_separately(self) -> None:
        enforcer = self.enforcer()
        enforcer.check("acme", "trial", spans=2_100)
        assert enforcer.check("globex", "trial", spans=10).allowed

    def test_an_unlimited_plan_short_circuits(self) -> None:
        verdict = self.enforcer().check("acme", "enterprise", spans=10_000_000)
        assert verdict.allowed and not verdict.soft_exceeded

    def test_an_override_beats_the_plan(self) -> None:
        enforcer = self.enforcer()
        assert enforcer.check("acme", "trial", spans=5_000, override_minute=10_000).allowed

    def test_an_override_of_zero_means_unlimited_not_unset(self) -> None:
        """0 is meaningful here, which is why the column is NULLable rather than
        defaulting to 0 -- otherwise an override to unlimited would be
        indistinguishable from no override at all."""
        verdict = self.enforcer().check(
            "acme", "trial", spans=10_000_000, override_minute=0, override_day=0
        )
        assert verdict.allowed

    def test_an_unknown_plan_falls_back_to_the_most_restrictive(self) -> None:
        """A typo in a plan name must not hand somebody unlimited ingest."""
        from control.quota import DEFAULT_PLAN, QuotaEnforcer

        assert QuotaEnforcer.plan_for("gold-tier-typo").name == DEFAULT_PLAN

    def test_a_broken_counter_fails_open(self) -> None:
        """Refusing telemetry because OUR bookkeeping broke inverts who is being
        protected. Architecture 8 settles this shape already: with the control
        plane down, ingest keeps working."""
        from control.quota import QuotaEnforcer, QuotaStore

        class Broken(QuotaStore):
            def __init__(self) -> None:
                self.url = ""
                self._local = {}
                self.degraded = False

            @property
            def client(self):
                raise RuntimeError("redis is down")

        assert QuotaEnforcer(store=Broken()).check("acme", "trial", spans=1).allowed

    def test_empty_payloads_do_not_consume_quota(self) -> None:
        assert self.enforcer().check("acme", "trial", spans=0).allowed


class TestQuotaAtTheGateway:
    def principal(self, **kw):
        from control.models import Principal

        return Principal(
            key_id=1, org_id=1, tenant="acme", project="default",
            environment="production", scopes=("ingest",), **kw
        )

    def payload(self, spans: int) -> bytes:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

        request = ExportTraceServiceRequest()
        resource_spans = ResourceSpans()
        scope_spans = ScopeSpans()
        for _ in range(spans):
            scope_spans.spans.append(Span(name="x"))
        resource_spans.scope_spans.append(scope_spans)
        request.resource_spans.append(resource_spans)
        return request.SerializeToString()

    def test_spans_are_counted_across_every_resource_and_scope(self) -> None:
        from ingest.app import _count_spans, _parse

        assert _count_spans(_parse(self.payload(7))) == 7

    def test_the_soft_flag_reaches_the_span(self) -> None:
        """A soft quota that only appears in our logs is a warning the customer
        never receives. On the span it is in their own console, on exactly the
        data that was at risk."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        from ingest.app import SOFT_QUOTA_ATTRIBUTE, stamp

        out = ExportTraceServiceRequest()
        out.ParseFromString(stamp(self.payload(1), self.principal(), soft=True))
        assert SOFT_QUOTA_ATTRIBUTE in [
            kv.key for kv in out.resource_spans[0].resource.attributes
        ]

    def test_no_flag_when_under_the_threshold(self) -> None:
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        from ingest.app import SOFT_QUOTA_ATTRIBUTE, stamp

        out = ExportTraceServiceRequest()
        out.ParseFromString(stamp(self.payload(1), self.principal(), soft=False))
        assert SOFT_QUOTA_ATTRIBUTE not in [
            kv.key for kv in out.resource_spans[0].resource.attributes
        ]

    def test_rejection_is_429_and_retryable_not_403(self) -> None:
        """The difference matters to an OTLP exporter: 429 is retryable and 403
        is not, so a rate limit sent as 403 makes a client abandon data it could
        have delivered a minute later."""
        import pytest
        from fastapi import HTTPException

        from control.quota import Verdict
        from ingest.app import _reject

        with pytest.raises(HTTPException) as caught:
            _reject(
                self.principal(),
                Verdict(allowed=False, reason="rate limit", retry_after=42),
            )
        assert caught.value.status_code == 429
        assert caught.value.headers["Retry-After"] == "42"


class TestAdminScopeIsolation:
    """The admin console spans every tenant, so its credential is the boundary.

    These are the tests that matter most in this file. Everything else the
    dashboard does is a table; this is the part where a mistake shows one
    customer another customer's name.
    """

    def principal(self, *scopes: str):
        from control.models import Principal

        return Principal(
            key_id=1, org_id=1, tenant="acme", project="default",
            environment="production", scopes=tuple(scopes),
        )

    def test_admin_is_its_own_scope_not_a_bigger_read(self) -> None:
        """`read` is bounded by one org and `admin` is not, which makes them
        different KINDS of credential. Treating admin as "read, but more" would
        put every read key one bug away from a cross-tenant view."""
        from control.keys import ADMIN, READ

        assert ADMIN != READ
        assert not self.principal(READ).can(ADMIN)
        assert not self.principal(ADMIN).can(READ)

    def test_a_read_key_cannot_reach_the_admin_api(self) -> None:
        import pytest
        from fastapi import HTTPException

        from query.admin import operator

        request = _FakeRequest({"x-api-key": "read-key"})
        with _patched_control(self.principal("read")), pytest.raises(HTTPException) as caught:
            operator(request)
        assert caught.value.status_code == 401

    def test_an_ingest_key_cannot_reach_the_admin_api(self) -> None:
        import pytest
        from fastapi import HTTPException

        from query.admin import operator

        with _patched_control(self.principal("ingest")), pytest.raises(HTTPException) as caught:
            operator(_FakeRequest({"x-api-key": "ingest-key"}))
        assert caught.value.status_code == 401

    def test_an_admin_key_is_accepted(self) -> None:
        from query.admin import operator

        with _patched_control(self.principal("admin")):
            assert operator(_FakeRequest({"x-api-key": "admin-key"})).can("admin")

    def test_no_credential_is_refused(self) -> None:
        import pytest
        from fastapi import HTTPException

        from query.admin import operator

        with _patched_control(None), pytest.raises(HTTPException) as caught:
            operator(_FakeRequest({}))
        assert caught.value.status_code == 401

    def test_every_admin_route_carries_the_scope_dependency(self) -> None:
        """A check repeated per endpoint is a check that will eventually be
        forgotten on one of them. This asserts the dependency is on ALL of them,
        so adding a route without it fails here rather than in production."""
        from query.admin import operator, router

        for route in router.routes:
            deps = [
                getattr(d.call, "__name__", "")
                for d in getattr(route, "dependant", None).dependencies
            ] if getattr(route, "dependant", None) else []
            nested = [
                getattr(sub.call, "__name__", "")
                for d in (getattr(route, "dependant", None).dependencies if
                          getattr(route, "dependant", None) else [])
                for sub in d.dependencies
            ]
            assert operator.__name__ in deps + nested, route.path

    def test_the_admin_scope_cannot_be_granted_over_http(self) -> None:
        """An API able to mint a cross-tenant credential is one authorization
        bug away from a customer minting one. There is no product reason for
        that endpoint, so its absence is asserted rather than assumed."""
        from query.admin import router

        paths = " ".join(r.path for r in router.routes)
        assert "issue" not in paths
        assert "scopes" not in paths


class TestAdminQuotaAndRevoke:
    """The two emergency levers Architecture 8 names."""

    def test_quota_null_and_zero_are_different_values(self) -> None:
        """`null` restores the plan limit, `0` means unlimited. Collapsing them
        would make "give this tenant unlimited ingest" indistinguishable from
        "put them back on their plan"."""
        from control.quota import QuotaEnforcer, QuotaStore

        class Store(QuotaStore):
            def __init__(self) -> None:
                self._local, self.degraded, self.url = {}, False, ""

            def incr(self, key: str, amount: int, ttl: int) -> int:
                return self._incr_local(key, amount, ttl)

        enforcer = QuotaEnforcer(store=Store())
        # None -> plan limit applies (trial caps at 2000/min)
        assert not enforcer.check("t1", "trial", spans=2_001, override_minute=None).allowed
        # 0 -> unlimited
        assert enforcer.check("t2", "trial", spans=10_000_000, override_minute=0,
                              override_day=0).allowed

    def test_revoking_an_unknown_prefix_is_a_404_not_a_silent_success(self) -> None:
        """A revoke that reports success without revoking anything is the worst
        possible outcome for the one operation you run in an emergency."""
        import pytest
        from fastapi import HTTPException

        from query.admin import revoke

        class Plane:
            def revoke_key(self, prefix: str, **kw: object) -> bool:
                # **kw absorbs actor/source/source_ip, which the endpoint now
                # passes so the revocation is attributable.
                return False

        import query.app as app_module

        original = app_module.control_plane
        app_module.control_plane = lambda: Plane()
        try:
            with pytest.raises(HTTPException) as caught:
                revoke("nope", _FakeRequest({}), None)
        finally:
            app_module.control_plane = original
        assert caught.value.status_code == 404


class TestAdminRepositoryShape:
    """Cross-tenant reads live in their own module, deliberately."""

    def test_the_span_repository_never_gained_a_crosstenant_read(self) -> None:
        """`query/repository.py` opens with a rule: tenant scoping happens in
        that layer and nowhere else. A file where MOST queries are scoped is
        more dangerous than one where none are, because the reader's eye stops
        checking."""
        import inspect

        import query.repository as repo

        source = inspect.getsource(repo)
        # Every span read still goes through the tenant-scoped CTE.
        assert "tenant_id = {tenant:String}" in source

    def test_orphaned_tenants_are_surfaced_not_filtered(self) -> None:
        """Telemetry under a tenant with no org row should be impossible now
        that the gateway authenticates. If it appears, either a key outlived its
        org or something bypassed the gateway -- both worth a page, neither
        worth hiding."""
        import inspect

        from query.admin_repository import AdminRepository

        assert "orphaned=True" in inspect.getsource(AdminRepository.tenants)


class _FakeRequest:
    """Enough of a Request for the code under test.

    `url` and `client` are here because `operator()` reads them when recording a
    refusal. Without them the audit call raised, and the deliberate
    `contextlib.suppress` around it swallowed the error -- so the test failed
    with an empty log and looked like a missing feature. That suppress is
    correct (a broken audit must not turn a 401 into a 500) and this is its
    cost: a coding error inside the audit call is invisible except in a test
    that exercises it properly.
    """

    def __init__(self, headers: dict) -> None:
        self.headers = headers
        self.url = type("Url", (), {"path": "/api/v1/admin/overview"})()
        self.client = type("Client", (), {"host": "127.0.0.1"})()


@contextlib.contextmanager
def _patched_control(principal):
    import query.app as app_module

    class Plane:
        def authenticate(self, token):
            return principal

    original = app_module.control_plane
    app_module.control_plane = lambda: Plane()
    try:
        yield
    finally:
        app_module.control_plane = original


class TestAuditLog:
    """Operator actions leave a record, or they do not happen.

    The design point worth testing is the TRANSACTION. The action and its audit
    entry are written together in Postgres, which is what lets a revoke be
    unconditional: an audit log in a different store would force a bad choice --
    block the revoke when the log is down and a leaked credential stays live, or
    do it anyway and the action most worth recording is the one most likely to
    go unrecorded.
    """

    def plane(self, *, org_exists=True, key_active=True):
        """A ControlPlane with the database faked at the statement level.

        Fake at `_rows`, not at `set_quota`, because the thing under test IS the
        sequence of statements and their transaction -- stubbing the method
        would test nothing but the stub.
        """
        from control.repository import ControlPlane

        plane = ControlPlane.__new__(ControlPlane)
        plane.dsn = ""
        plane._conn = None
        plane._cache = {}
        plane.statements = []
        plane.committed = False
        plane.rolled_back = False

        outer = plane

        class Transaction:
            def __enter__(self):
                outer.statements.append(("BEGIN", ()))
                return self

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    outer.committed = True
                else:
                    outer.rolled_back = True
                return False

        class Conn:
            def transaction(self):
                return Transaction()

        def rows(sql, params=()):
            outer.statements.append((" ".join(sql.split()), params))
            if "FROM orgs" in sql and "FOR UPDATE" in sql:
                return ([{"quota_spans_per_minute": 111, "quota_spans_per_day": None}]
                        if org_exists else [])
            if "UPDATE api_keys SET revoked_at" in sql:
                return ([{"id": 9, "scopes": "read", "org_id": 1}]
                        if key_active else [])
            return []

        plane._rows = rows
        plane._one = lambda sql, params=(): (rows(sql, params) or [None])[0]
        type(plane).conn = property(lambda self: Conn())
        return plane

    def actor(self):
        from control.models import Principal

        return Principal(
            key_id=7, key_prefix="a1b2c3d4", org_id=1, tenant="acme",
            project="default", environment="production", scopes=("admin",),
        )

    def _actions(self, plane):
        return [sql for sql, _ in plane.statements]

    def test_a_quota_change_and_its_record_are_one_transaction(self) -> None:
        plane = self.plane()
        assert plane.set_quota("acme", 500, None, actor=self.actor(), source="console")
        actions = self._actions(plane)
        assert actions[0] == "BEGIN"
        assert any("INSERT INTO audit_log" in a for a in actions)
        assert plane.committed

    def test_the_entry_records_what_changed_not_just_that_it_did(self) -> None:
        import json

        plane = self.plane()
        plane.set_quota("acme", 500, None, actor=self.actor())
        detail = next(
            json.loads(params[7])
            for sql, params in plane.statements
            if "INSERT INTO audit_log" in sql
        )
        assert detail["from"]["per_minute"] == 111
        assert detail["to"]["per_minute"] == 500

    def test_the_old_value_is_read_inside_the_transaction(self) -> None:
        """Reading it outside would let a concurrent change slip in between, so
        the entry would report a `from` that was never true."""
        plane = self.plane()
        plane.set_quota("acme", 500, None, actor=self.actor())
        actions = self._actions(plane)
        assert actions.index("BEGIN") < next(
            i for i, a in enumerate(actions) if "FOR UPDATE" in a
        )

    def test_the_actor_is_named_by_key_prefix(self) -> None:
        plane = self.plane()
        plane.set_quota("acme", 500, None, actor=self.actor(), source="console")
        params = next(
            p for sql, p in plane.statements if "INSERT INTO audit_log" in sql
        )
        assert params[0] == 7            # key id
        assert params[1] == "a1b2c3d4"   # denormalised prefix
        assert params[3] == "console"

    def test_a_cli_action_is_audited_too(self) -> None:
        """`scripts/admin.py` needs database access, which makes it the MORE
        privileged path, not one to exempt."""
        plane = self.plane()
        plane.set_quota("acme", 500, None)  # no actor -> CLI
        params = next(
            p for sql, p in plane.statements if "INSERT INTO audit_log" in sql
        )
        assert params[3] == "cli"
        assert params[0] is None

    def test_a_missing_org_writes_no_entry(self) -> None:
        """A phantom record of a change that never happened is worse than no
        record: it is a false statement in the one place people go for truth."""
        plane = self.plane(org_exists=False)
        assert not plane.set_quota("nope", 5, None, actor=self.actor())
        assert not any("INSERT INTO audit_log" in a for a in self._actions(plane))

    def test_a_revoke_and_its_record_are_one_transaction(self) -> None:
        plane = self.plane()
        assert plane.revoke_key("a1b2c3d4", actor=self.actor(), source="console")
        actions = self._actions(plane)
        assert actions[0] == "BEGIN"
        assert any("INSERT INTO audit_log" in a for a in actions)

    def test_revoking_an_inactive_key_writes_no_entry(self) -> None:
        plane = self.plane(key_active=False)
        assert not plane.revoke_key("gone", actor=self.actor())
        assert not any("INSERT INTO audit_log" in a for a in self._actions(plane))

    def test_the_revoked_key_leaves_the_cache_immediately(self) -> None:
        plane = self.plane()
        plane._cache["a1b2c3d4"] = (9e9, "hash", self.actor())
        plane.revoke_key("a1b2c3d4", actor=self.actor())
        assert "a1b2c3d4" not in plane._cache


class TestAuditOfRefusals:
    def test_a_refused_admin_attempt_is_recorded(self) -> None:
        """The most interesting rows in the table: somebody presenting a
        credential that does not work at the cross-tenant surface."""
        import pytest
        from fastapi import HTTPException

        from control.models import Principal
        from query.admin import operator

        recorded = []

        class Plane:
            def authenticate(self, token):
                return Principal(
                    key_id=1, key_prefix="reader01", org_id=1, tenant="acme",
                    project="default", environment="production", scopes=("read",),
                )

            def audit(self, action, **kw):
                recorded.append((action, kw))

        import query.app as app_module

        original = app_module.control_plane
        app_module.control_plane = lambda: Plane()
        try:
            with pytest.raises(HTTPException):
                operator(_FakeRequest({"x-api-key": "read-key"}))
        finally:
            app_module.control_plane = original

        assert recorded and recorded[0][0] == "admin.denied"
        assert recorded[0][1]["outcome"] == "denied"
        assert recorded[0][1]["detail"]["reason"] == "wrong scope"

    def test_a_failing_audit_write_still_returns_401_not_500(self) -> None:
        """Failing to log a refusal must not turn a 401 into a 500 and tell the
        caller something they should not learn from an error code."""
        import pytest
        from fastapi import HTTPException

        from query.admin import operator

        class Plane:
            def authenticate(self, token):
                return None

            def audit(self, *a, **kw):
                raise RuntimeError("audit table is gone")

        import query.app as app_module

        original = app_module.control_plane
        app_module.control_plane = lambda: Plane()
        try:
            with pytest.raises(HTTPException) as caught:
                operator(_FakeRequest({}))
        finally:
            app_module.control_plane = original
        assert caught.value.status_code == 401

    def test_a_forwarded_ip_is_taken_but_never_trusted(self) -> None:
        """A caller can set `x-forwarded-for` freely, so it is an operational
        hint and never an authorization input. Bounded, so a long header cannot
        bloat the table."""
        from query.admin import _client_ip

        assert _client_ip(_FakeRequest({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})) == "1.2.3.4"
        assert len(_client_ip(_FakeRequest({"x-forwarded-for": "x" * 500}))) <= 64
