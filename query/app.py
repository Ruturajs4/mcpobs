"""Query API.

The read plane over the tables Days 1-2 built. Endpoints follow V2 §13.2.

TENANCY COMES FROM THE KEY, NEVER FROM A PARAMETER.
    Until DF-9 landed, `?tenant=` was a query parameter, and the docstring here
    promised that "when API keys land the only change is where the values come
    from -- not whether the filter is applied". That promise is now cashed: the
    repository is unchanged, and `Scope` reads the values off an authenticated
    `Principal` instead of off the query string.

    The parameters are GONE, not deprecated. Leaving them in as an override
    "for admins" is how a tenancy boundary becomes optional, and an optional
    boundary is not one. Assertion F4 sends `?tenant=` for another org and
    checks it changes nothing.
"""

# NO `from __future__ import annotations` here, deliberately. It turns every
# `Annotated[str, Query(...)]` into a string ForwardRef that FastAPI cannot
# resolve, and the failure surfaces as an opaque PydanticUserError at request
# time rather than at import. Python 3.11 handles `X | None` natively anyway.
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from control.interfaces import Authenticator
from control.keys import READ
from control.models import Principal
from control.resolve import authenticator as _resolve_authenticator
from query.dtos import (
    CapabilityPage,
    Overview,
    Page,
    ServerSummary,
    SpanDetail,
    ToolSummary,
    TraceDetail,
)
from query.filters import Filters, catalog, openapi_parameters, parse
from query.repository import SpanRepository, decode_cursor


def _docs_enabled() -> bool:
    """Whether to publish OpenAPI, Swagger and ReDoc.

    OFF unless explicitly switched on, because these are served to anyone who
    finds the host. `/openapi.json` enumerated every route including
    `/api/v1/admin/keys/{prefix}/revoke`, `/api/v1/admin/tenants/{tenant}/quota`
    and `/api/v1/admin/invites` -- a complete map of the operator surface,
    handed to unauthenticated callers. Knowing a route is not the same as
    reaching one, but it is the first thing an attacker would otherwise have to
    guess.

    Default-off rather than default-on-with-a-flag-to-disable: forgetting to set
    a variable should leave the exposed thing closed, not open.
    """
    return os.getenv("EXPOSE_API_DOCS", "").lower() in ("1", "true", "yes")


app = FastAPI(
    title="MCP Observability Query API",
    version="0.1.0",
    description="MCP-native observability: servers, tools, failures and traces.",
    docs_url="/docs" if _docs_enabled() else None,
    redoc_url="/redoc" if _docs_enabled() else None,
    openapi_url="/openapi.json" if _docs_enabled() else None,
)

log = logging.getLogger("mcpobs.query")

STATIC = Path(__file__).parent / "static"

_repository: SpanRepository | None = None

MAX_WINDOW_MINUTES = 60 * 24 * 7
MAX_PAGE = 200


def repository() -> SpanRepository:
    global _repository
    if _repository is None:
        _repository = SpanRepository(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            database=os.getenv("CLICKHOUSE_DB", "mcpobs"),
            username=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
            # False for every compose-managed ClickHouse this project ships
            # (plain HTTP on the docker network); a user-provided instance --
            # ClickHouse Cloud in particular -- is TLS-only. Same flag,
            # independently read here because this repository's ClickHouse
            # client is constructed separately from normalizer's
            # (normalizer/clickhouse_sink.py), not through normalizer.config.
            secure=os.getenv("CLICKHOUSE_SECURE", "false").lower() in ("1", "true", "yes"),
            # This repository is shared by FastAPI's worker threads. A generated
            # ClickHouse session id would be shared too, and clickhouse-connect
            # rejects overlapping requests in one session. The HTTP connection
            # pool remains shared and concurrent; only server-session affinity
            # is disabled because none of these read-only queries needs it.
            autogenerate_session_id=False,
            # Bounded server-side so one expensive query cannot occupy a
            # connection indefinitely (V2 §13.1).
            settings={"max_execution_time": 20},
        )
    return _repository


def control_plane() -> Authenticator:
    """The resolved Authenticator -- real ControlPlane or the local default.

    Kept as a standalone function, not inlined at the two call sites below,
    because tests/test_control_plane.py replaces it wholesale
    (`app_module.control_plane = lambda: Plane()`) to inject a fake for a
    single test without touching control/resolve.py's own module-level
    cache. No caching needed here: control/resolve.py's `authenticator()`
    already resolves once and caches for the process's lifetime.
    """
    return _resolve_authenticator()


class Scope:
    """Tenant, project and time window -- resolved once, for every endpoint.

    Tenant and project are taken from the API key and are NOT parameters. An
    endpoint cannot scope a query to the wrong tenant, because an endpoint
    never chooses one.
    """

    def __init__(
        self,
        request: Request,
        window_minutes: Annotated[int, Query(ge=1, le=MAX_WINDOW_MINUTES)] = 60,
    ) -> None:
        principal = authenticate(request)
        self.principal = principal
        self.tenant = principal.tenant
        self.project = principal.project
        self.window_minutes = window_minutes
        self.since = (datetime.now(UTC) - timedelta(minutes=window_minutes)).replace(tzinfo=None)


def authenticate(request: Request) -> Principal:
    """Resolve the caller's key, or refuse.

    Read keys only. An ingest key is deliberately not accepted here even though
    it identifies the same org: the two live in different places -- an ingest key
    sits in a customer's server process and deployment config, a read key in a
    browser -- so one being compromised must not imply the other (control/
    schema/001).
    """
    header = request.headers.get("authorization", "")
    token = request.headers.get("x-api-key") or (
        header[7:] if header.lower().startswith("bearer ") else None
    )
    principal = control_plane().authenticate(token)
    if principal is None or not principal.can(READ):
        raise HTTPException(
            status_code=401,
            detail="invalid or insufficient API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


ScopeDep = Annotated[Scope, Depends()]
RepoDep = Annotated[SpanRepository, Depends(repository)]


def request_filters(view: str, request: Request) -> Filters:
    """Parse user-controlled filters into a stable 422 API contract."""
    try:
        return parse(view, request.query_params, request.query_params.getlist("where"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def validated_cursor(cursor: str | None) -> str | None:
    """Reject malformed opaque cursors before entering the repository."""
    if cursor is None:
        return None
    try:
        decode_cursor(cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc
    return cursor


@app.get("/health")
def health(repo: RepoDep) -> dict[str, str]:
    """Liveness. Unauthenticated, so it says whether -- never why.

    The failure body used to interpolate the driver exception, which carries the
    ClickHouse hostname and port:

        clickhouse unavailable: Error HTTPConnectionPool(
            host='clickhouse-internal.prod.local', port=8123) ...

    That is internal topology, published to anyone who can reach the load
    balancer while the database is down -- which is exactly when someone is
    probing. The detail goes to the log, where an operator can read it and a
    stranger cannot.
    """
    try:
        repo.client.query("SELECT 1")
    except Exception as exc:
        log.warning("health check failed: %s", exc)
        raise HTTPException(503, "dependency unavailable") from exc
    return {"status": "ok"}


@app.get("/api/v1/overview", response_model=Overview)
def overview(scope: ScopeDep, repo: RepoDep) -> Overview:
    """Fleet health: how much traffic, how much is failing, and how."""
    return repo.overview(scope.tenant, scope.project, scope.since, scope.window_minutes)


@app.get("/api/v1/servers", response_model=list[ServerSummary])
def servers(scope: ScopeDep, repo: RepoDep) -> list[ServerSummary]:
    return repo.servers(scope.tenant, scope.project, scope.since)


@app.get("/api/v1/servers/{server}/tools", response_model=list[ToolSummary])
def server_tools(server: str, scope: ScopeDep, repo: RepoDep) -> list[ToolSummary]:
    return repo.tools(scope.tenant, scope.project, scope.since, server=server)


@app.get("/api/v1/tools", response_model=list[ToolSummary])
def tools(scope: ScopeDep, repo: RepoDep) -> list[ToolSummary]:
    return repo.tools(scope.tenant, scope.project, scope.since)


@app.get(
    "/api/v1/capabilities",
    response_model=CapabilityPage,
    openapi_extra={"parameters": openapi_parameters("capabilities")},
)
def capabilities(
    scope: ScopeDep,
    repo: RepoDep,
    request: Request,
    kind: Annotated[str, Query(pattern="^(tool|prompt|resource|protocol)$")] = "tool",
) -> CapabilityPage:
    """Tools, prompts, resources, or protocol methods.

    `protocol` is the one that was missing: `tools/list` and `server/discover`
    are 38% of stored spans and had no home in the console. `tools/list` runs on
    every client connect, so if it is slow that is a real customer symptom the
    UI was silent about.
    """
    return repo.capabilities(
        scope.tenant,
        scope.project,
        scope.since,
        kind=kind,
        filters=request_filters("capabilities", request),
    )


@app.get("/api/v1/filters")
def filters(
    view: Annotated[str, Query(pattern="^(traces|errors|capabilities)$")],
    scope: ScopeDep,
    repo: RepoDep,
) -> dict[str, object]:
    """Generic filter-panel contract, including values available in this scope."""
    return catalog(view, repo.filter_options(scope.tenant, scope.project, scope.since))


@app.get(
    "/api/v1/traces",
    response_model=Page,
    openapi_extra={"parameters": openapi_parameters("traces")},
)
def traces(
    scope: ScopeDep,
    repo: RepoDep,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: str | None = None,
) -> Page:
    """Trace list, keyset-paginated and filtered entirely in SQL."""
    return repo.traces(
        scope.tenant,
        scope.project,
        scope.since,
        limit=limit,
        cursor=validated_cursor(cursor),
        filters=request_filters("traces", request),
    )


@app.get("/api/v1/traces/{trace_id}", response_model=TraceDetail)
def trace(trace_id: str, scope: ScopeDep, repo: RepoDep) -> TraceDetail:
    """One trace, with its spans ordered and depth-annotated for a waterfall.

    Deliberately NOT window-scoped: you follow a link to a trace, and it is
    infuriating for that to 404 because the default window moved past it. The
    locator carries the date, so the read is still partition-pruned.
    """
    detail = repo.trace(scope.tenant, scope.project, trace_id)
    if detail is None:
        raise HTTPException(404, f"trace {trace_id} not found")
    return detail


@app.get("/api/v1/traces/{trace_id}/spans/{span_id}", response_model=SpanDetail)
def span_detail(
    trace_id: str, span_id: str, scope: ScopeDep, repo: RepoDep
) -> SpanDetail:
    """One span's detail, for traces too large to ship the whole map.

    A large trace omits the bulk `detail` map for payload size; without this
    route that meant no span in such a trace could be inspected at all,
    including the ones on screen. The cap is a payload optimisation, not a loss
    of capability.

    Same tenant scoping as everything else: the repository applies it, and an
    endpoint never chooses a tenant.
    """
    detail = repo.span_detail(scope.tenant, scope.project, trace_id, span_id)
    if detail is None:
        raise HTTPException(404, "span not found")
    return detail


@app.get(
    "/api/v1/errors",
    response_model=Page,
    openapi_extra={"parameters": openapi_parameters("errors")},
)
def errors(
    scope: ScopeDep,
    repo: RepoDep,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: str | None = None,
) -> Page:
    """Failing traces only.

    `pending_input` is NOT an error: it is an MRTR interim round, and counting
    it here would be the single most likely way to corrupt an error rate
    (Day-1 §3.2, D20).

    The failures-only clause is dropped when an explicit category is asked for,
    so `?failure_category=cancelled` can still show cancellations -- they are
    excluded from the default failure set on purpose (D20), and a filter the
    list refuses to honour is worse than one it does not offer.
    """
    parsed_filters = request_filters("errors", request)
    has_category_filter = "failure_category" in parsed_filters.values or any(
        condition.field == "failure_category" for condition in parsed_filters.conditions
    )
    return repo.traces(
        scope.tenant,
        scope.project,
        scope.since,
        limit=limit,
        cursor=validated_cursor(cursor),
        filters=parsed_filters,
        failures_only=not has_category_filter,
    )


# The admin router (query/admin.py) and its console (query/static/admin.*)
# are NOT part of OSS -- they are the cross-tenant operator surface, and live
# in the private ECC repo alongside control/repository.py. See
# docs/decisions.md D180. An ECC deployment mounts its own admin router the
# same way this used to: `app.include_router(admin_router)`.


class RevalidatingStatics(StaticFiles):
    """Static assets that must be revalidated before use.

    The console ships unversioned URLs -- `/static/app.js`, not
    `app.<hash>.js` -- and FastAPI's StaticFiles sends ETag and Last-Modified
    but NO Cache-Control. With no Cache-Control a browser is free to apply
    heuristic freshness and serve the file from cache without asking, which is
    exactly what happened here: a deployed fix kept not taking effect, and the
    page was running the previous build while the server served the new one.

    That is a correctness problem, not a nuisance. A browser holding yesterday's
    JavaScript against today's API is the skew that produces bug reports nobody
    can reproduce.

    `no-cache` does NOT mean "do not cache" -- it means "revalidate before
    using". The ETag is already sent, so the usual answer is a 304 with no body,
    and the cost of correctness here is one conditional request per asset.

    The alternative, hashed filenames with a long max-age, is better and needs a
    build step. This repo deliberately has none (query/app.py: "no build step,
    no CORS"), so the cheap correct thing wins until that changes.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        # setdefault, not assignment: a 304 built by StaticFiles carries its own
        # headers and must not have them rewritten underneath it.
        response.headers.setdefault("cache-control", "no-cache")
        return response


app.mount("/static", RevalidatingStatics(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    """The console.

    Served by the API rather than a separate origin: one process, no build step,
    no CORS. A Phase-0 proof does not need a frontend toolchain, and adding one
    would be the largest thing in the repo by a wide margin.
    """
    return FileResponse(
        STATIC / "index.html",
        # The shell is what points at every other asset. Cached, it pins a
        # user to an entire old build.
        headers={"cache-control": "no-cache"},
    )


# No /admin route here -- the operator console (query/static/admin.*) ships
# with the ECC repo, which mounts it the same way this file mounts "/".
