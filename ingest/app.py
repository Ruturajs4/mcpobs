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

log = logging.getLogger("ingest")

COLLECTOR_ENDPOINT = os.getenv("COLLECTOR_ENDPOINT", "http://otel-collector:4318/v1/traces")

#: Written by us, from the authenticated key, over anything the customer sent.
TENANT_ATTRIBUTE = "tenant.id"
PROJECT_ATTRIBUTE = "project.id"
REGION_ATTRIBUTE = "data.region"
REGION = os.getenv("DATA_REGION", "local")

control = ControlPlane()


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


app = FastAPI(title="mcpobs ingest", version="0.1.0", lifespan=lifespan)
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


def stamp(payload: bytes, principal: Principal) -> bytes:
    """Overwrite tenant/project/region on every ResourceSpans.

    Parsed and re-serialised rather than passed through, because the whole point
    is that the customer's own values must not survive. `upsert` semantics are
    implemented by dropping any existing key of the same name first -- appending
    would leave two `tenant.id` entries and make the winner depend on whichever
    consumer read last.
    """
    request = ExportTraceServiceRequest()
    request.ParseFromString(payload)

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

    try:
        stamped = stamp(body, principal)
    except Exception as exc:
        # A malformed body is the customer's bug, not ours, and it must be told
        # apart from our own failures -- 400, never 500.
        log.info("rejected malformed OTLP from %s: %s", principal.tenant, exc)
        raise HTTPException(status_code=400, detail="malformed OTLP payload") from exc

    # Forwarded SYNCHRONOUSLY, and the Collector's status is returned unchanged.
    # Acking before the Collector accepted would move the ack boundary in front
    # of the queue that makes it durable, which is precisely the line
    # Architecture.md §5.1 says nothing may cross.
    upstream = client().post(
        COLLECTOR_ENDPOINT,
        content=stamped,
        headers={"content-type": "application/x-protobuf"},
    )
    control.touch(principal.key_id)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/x-protobuf"),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
