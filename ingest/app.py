"""The authenticating ingest gateway.

WHY THIS EXISTS AT ALL, GIVEN ADR-003
    ADR-003 says no hand-written service sits in the ingest path: the Collector
    produces to Kafka directly, because we want its batching, retry, queueing and
    backpressure rather than our own reimplementation of them on a deadline. That
    decision stands, and everything durability-critical still happens inside the
    Collector.

    ADR-003 also named the cost it was accepting: "custom auth logic must fit the
    Collector's extension model, and complex tenant resolution may require a
    small custom extension." DF-15 was the spike to find out. The answer,
    measured against the running image rather than assumed:

        otel/opentelemetry-collector-contrib:0.115.1 ships basicauth,
        bearertokenauth, oidcauth, asapauth, sigv4auth, oauth2client and
        headers_setter.

    Every one of them answers "is this request allowed?". NONE of them resolves
    an arbitrary API key to a tenant and writes it onto the Resource. The
    authenticator extension interface puts data in the request context; nothing
    stock moves it from there onto resource attributes.

    So the choice was a custom Go extension or a shim. This is the shim, kept
    deliberately thin: it authenticates, it stamps, it forwards. It does not
    batch, retry, queue, or talk to Kafka -- all of which remain the Collector's,
    which is the part of ADR-003 that actually mattered.

THE STAMPING IS THE SECURITY BOUNDARY
    Resource attributes arrive from the customer's process, so `tenant.id` in an
    inbound payload is an assertion by an untrusted party. This OVERWRITES it
    from the authenticated key. Architecture.md §5.1: "A customer must not be
    able to write telemetry into another tenant by setting a resource
    attribute." Assertion F3 fires a payload that tries exactly that.
"""

# NO `from __future__ import annotations`: FastAPI resolves Annotated types at
# import, and the string ForwardRefs it produces fail at request time instead.

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from control import ControlPlane
from control.keys import INGEST
from control.models import Principal
from control.quota import QuotaEnforcer, Verdict

log = logging.getLogger("ingest")

COLLECTOR_ENDPOINT = os.getenv("COLLECTOR_ENDPOINT", "http://otel-collector:4318/v1/traces")
COLLECTOR_HEALTH_ENDPOINT = os.getenv(
    "COLLECTOR_HEALTH_ENDPOINT", "http://otel-collector:13133/"
)

#: Stamped on every span of a request that arrived while the tenant was over its
#: SOFT threshold. A soft quota that only appears in our logs is a warning the
#: customer never receives; on the span, it is visible in their own console, on
#: exactly the data that was at risk.
SOFT_QUOTA_ATTRIBUTE = "mcpobs.quota.soft_exceeded"

#: Written by us, from the authenticated key, over anything the customer sent.
TENANT_ATTRIBUTE = "tenant.id"
PROJECT_ATTRIBUTE = "project.id"
REGION_ATTRIBUTE = "data.region"
REGION = os.getenv("DATA_REGION", "local")

control = ControlPlane()
quotas = QuotaEnforcer()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Wait for the control plane, then apply its schema.

    A gateway that starts before it can authenticate would answer 401 to every
    legitimate customer for as long as Postgres took to come up, which reads
    exactly like a credential problem on their side.
    """
    control.wait_ready()
    applied = control.migrate()
    log.info("control plane ready: %s", ", ".join(applied))
    yield


def _docs_enabled() -> bool:
    """See query/app.py. Same reasoning, and this one faces customer servers.

    Default-off: forgetting to set a variable must leave the exposed thing
    closed rather than open.
    """
    return os.getenv("EXPOSE_API_DOCS", "").lower() in ("1", "true", "yes")


app = FastAPI(
    title="mcpobs ingest",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled() else None,
    redoc_url="/redoc" if _docs_enabled() else None,
    openapi_url="/openapi.json" if _docs_enabled() else None,
)
_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=30.0)
    return _client


def principal_from(authorization: str | None, api_key: str | None) -> Principal:
    """Authenticate, or refuse. Never falls back to a default tenant.

    There is no anonymous ingest path. A gateway that quietly accepts
    unauthenticated spans into a default tenant is how one customer's telemetry
    ends up in another's console, and "it only happens when the header is
    missing" is not a mitigation.
    """
    token = api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    principal = control.authenticate(token)
    if principal is None or not principal.can(INGEST):
        # One message for every failure. Distinguishing "unknown key" from
        # "read-only key" would tell a prober which of their stolen keys is
        # real.
        raise HTTPException(status_code=401, detail="invalid or insufficient API key")
    return principal


def _parse(payload: bytes) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    request.ParseFromString(payload)
    return request


def _count_spans(request: ExportTraceServiceRequest) -> int:
    """Spans in the payload -- the metered unit, and the one that costs money."""
    return sum(
        len(scope_spans.spans)
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
    )


def _reject(principal: Principal, verdict: Verdict) -> None:
    """429 with `Retry-After`, and the numbers behind the decision.

    429 rather than 403: this is a limit, not a permission, and the difference
    matters to an OTLP exporter -- 429 is retryable and 403 is not, so a rate
    limit sent as 403 would make a client give up on data it could have
    delivered a minute later. The daily verdict sets `Retry-After` to the
    window reset for the same reason: an honest "not before this time" beats a
    retry that cannot succeed.
    """
    log.warning("quota rejection for %s: %s", principal.tenant, verdict.reason)
    raise HTTPException(
        status_code=429,
        detail=verdict.reason,
        headers={
            "Retry-After": str(verdict.retry_after),
            # The counter behind the decision, so the customer can see how far
            # over they are without asking us.
            "X-Quota-Used-Minute": str(verdict.used_minute),
            "X-Quota-Limit-Minute": str(verdict.limit_minute),
            "X-Quota-Used-Day": str(verdict.used_day),
            "X-Quota-Limit-Day": str(verdict.limit_day),
        },
    )


def stamp(payload: bytes, principal: Principal, soft: bool = False) -> bytes:
    """Parse-and-stamp, for callers holding raw bytes (and for the tests)."""
    return stamp_parsed(_parse(payload), principal, soft=soft)


def stamp_parsed(
    request: ExportTraceServiceRequest, principal: Principal, soft: bool = False
) -> bytes:
    """Overwrite tenant/project/region on every ResourceSpans.

    Takes an already-parsed request because the quota check upstream had to
    parse it to count spans, and parsing the same protobuf twice per request on
    the ingest hot path is a cost with nothing to show for it.

    The customer's own values are dropped before the trusted ones are appended.
    Appending alone would leave two `tenant.id` entries and make the winner
    depend on whichever consumer read last, which is a coin toss rather than a
    boundary.
    """
    trusted = {
        TENANT_ATTRIBUTE: principal.tenant,
        PROJECT_ATTRIBUTE: principal.project,
        REGION_ATTRIBUTE: REGION,
    }
    for resource_spans in request.resource_spans:
        attributes = resource_spans.resource.attributes
        kept = [kv for kv in attributes if kv.key not in trusted]
        del attributes[:]
        for kv in kept:
            attributes.append(kv)
        for key, value in trusted.items():
            attributes.append(KeyValue(key=key, value=AnyValue(string_value=value)))
        if soft:
            attributes.append(
                KeyValue(key=SOFT_QUOTA_ATTRIBUTE, value=AnyValue(bool_value=True))
            )
    return request.SerializeToString()


@app.post("/v1/traces")
async def traces(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Response:
    principal = principal_from(authorization, x_api_key)
    body = await request.body()

    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        # Deliberately unsupported. The pipeline is protobuf end to end
        # (collector/config.yaml: "never transcode to JSON"), and accepting JSON
        # here would put a second encoding into the one path where we have
        # promised there is only one.
        raise HTTPException(status_code=415, detail="send OTLP protobuf, not JSON")

    # Quota check sits HERE: after authentication, before stamping, on the near
    # side of the ack boundary (Architecture.md §5.1). Rejecting after the ack
    # would mean telling a customer their data was safe and then dropping it,
    # which ADR-008 forbids in as many words.
    #
    # Spans are counted by PARSING first -- a request can carry one span or ten
    # thousand, and metering requests would let the same volume through in a
    # hundredth of the calls.
    try:
        request_proto = _parse(body)
    except Exception as exc:
        log.info("rejected malformed OTLP from %s: %s", principal.tenant, exc)
        raise HTTPException(status_code=400, detail="malformed OTLP payload") from exc

    verdict = quotas.check(
        principal.tenant,
        principal.plan,
        _count_spans(request_proto),
        override_minute=principal.quota_spans_per_minute,
        override_day=principal.quota_spans_per_day,
    )
    if not verdict.allowed:
        _reject(principal, verdict)

    try:
        stamped = stamp_parsed(request_proto, principal, soft=verdict.soft_exceeded)
    except Exception as exc:
        # A malformed body is the customer's bug, not ours, and it must be told
        # apart from our own failures -- 400, never 500.
        log.info("rejected malformed OTLP from %s: %s", principal.tenant, exc)
        raise HTTPException(status_code=400, detail="malformed OTLP payload") from exc

    # Forwarded SYNCHRONOUSLY, and the Collector's status is returned unchanged.
    # Acking before the Collector accepted would move the ack boundary in front
    # of the queue that makes it durable, which is precisely the line
    # Architecture.md §5.1 says nothing may cross.
    try:
        upstream = client().post(
            COLLECTOR_ENDPOINT,
            content=stamped,
            headers={"content-type": "application/x-protobuf"},
        )
    except httpx.RequestError as exc:
        log.warning("collector unavailable for tenant %s: %s", principal.tenant, exc)
        raise HTTPException(status_code=503, detail="telemetry collector unavailable") from exc
    control.touch(principal.key_id)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/x-protobuf"),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    """Report readiness only when every required ingest dependency responds."""
    unavailable: list[str] = []
    try:
        control.ping()
    except Exception:  # noqa: BLE001
        unavailable.append("control-plane")
    try:
        quotas.store.client.ping()
    except Exception:  # noqa: BLE001
        unavailable.append("quota-store")
    try:
        response = client().get(COLLECTOR_HEALTH_ENDPOINT, timeout=1.0)
        response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError):
        unavailable.append("collector")
    if unavailable:
        raise HTTPException(status_code=503, detail={"unavailable": unavailable})
    return {"status": "ready"}
