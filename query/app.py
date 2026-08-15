"""Query API.

The read plane over the tables Days 1-2 built. Endpoints follow V2 §13.2.

Tenant and project arrive as query parameters TODAY because there is no auth
yet (DF-9). They are still applied by the repository rather than by any
endpoint, so when API keys land the only change is where the values come from --
not whether the filter is applied. An endpoint cannot forget to scope a query,
because an endpoint never writes one.
"""

# NO `from __future__ import annotations` here, deliberately. It turns every
# `Annotated[str, Query(...)]` into a string ForwardRef that FastAPI cannot
# resolve, and the failure surfaces as an opaque PydanticUserError at request
# time rather than at import. Python 3.11 handles `X | None` natively anyway.
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from query.dtos import (
    CapabilityRow,
    Overview,
    Page,
    ServerSummary,
    ToolSummary,
    TraceDetail,
)
from query.repository import SpanRepository

app = FastAPI(
    title="MCP Observability Query API",
    version="0.1.0",
    description="MCP-native observability: servers, tools, failures and traces.",
)

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
            # Bounded server-side so one expensive query cannot occupy a
            # connection indefinitely (V2 §13.1).
            settings={"max_execution_time": 20},
        )
    return _repository


class Scope:
    """Tenant, project and time window -- resolved once, for every endpoint."""

    def __init__(
        self,
        tenant: Annotated[str, Query(description="Tenant id")] = "local",
        project: Annotated[str, Query(description="Project id")] = "local",
        window_minutes: Annotated[int, Query(ge=1, le=MAX_WINDOW_MINUTES)] = 60,
    ) -> None:
        self.tenant = tenant
        self.project = project
        self.window_minutes = window_minutes
        self.since = (datetime.now(UTC) - timedelta(minutes=window_minutes)).replace(tzinfo=None)


ScopeDep = Annotated[Scope, Depends()]
RepoDep = Annotated[SpanRepository, Depends(repository)]


@app.get("/health")
def health(repo: RepoDep) -> dict[str, str]:
    try:
        repo.client.query("SELECT 1")
    except Exception as exc:
        raise HTTPException(503, f"clickhouse unavailable: {exc}") from exc
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


@app.get("/api/v1/capabilities", response_model=list[CapabilityRow])
def capabilities(
    scope: ScopeDep,
    repo: RepoDep,
    kind: Annotated[str, Query(pattern="^(tool|prompt|resource|protocol)$")] = "tool",
    server: str | None = None,
) -> list[CapabilityRow]:
    """Tools, prompts, resources, or protocol methods.

    `protocol` is the one that was missing: `tools/list` and `server/discover`
    are 38% of stored spans and had no home in the console. `tools/list` runs on
    every client connect, so if it is slow that is a real customer symptom the
    UI was silent about.
    """
    return repo.capabilities(scope.tenant, scope.project, scope.since, kind=kind, server=server)


@app.get("/api/v1/traces", response_model=Page)
def traces(
    scope: ScopeDep,
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: str | None = None,
    failure_category: str | None = None,
    tool: str | None = None,
) -> Page:
    """Trace list, keyset-paginated and filterable by failure kind."""
    return repo.traces(
        scope.tenant,
        scope.project,
        scope.since,
        limit=limit,
        cursor=cursor,
        failure_category=failure_category,
        tool=tool,
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


@app.get("/api/v1/errors", response_model=Page)
def errors(
    scope: ScopeDep,
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: str | None = None,
    failure_category: str | None = None,
) -> Page:
    """Failing traces only.

    `pending_input` is NOT an error: it is an MRTR interim round, and counting
    it here would be the single most likely way to corrupt an error rate
    (Day-1 §3.2, D20).
    """
    return repo.traces(
        scope.tenant,
        scope.project,
        scope.since,
        limit=limit,
        cursor=cursor,
        failure_category=failure_category,
        failures_only=failure_category is None,
    )


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    """The console.

    Served by the API rather than a separate origin: one process, no build step,
    no CORS. A Phase-0 proof does not need a frontend toolchain, and adding one
    would be the largest thing in the repo by a wide margin.
    """
    return FileResponse(STATIC / "index.html")
