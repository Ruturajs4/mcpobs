"""Admin API: the operator's cross-tenant surface.

THE ONE THING THAT MATTERS HERE
    Every endpoint requires the `admin` scope, and `admin` is never granted by
    any HTTP endpoint -- only by `scripts/admin.py`, which needs database
    access. An API able to mint a cross-tenant credential is one authorization
    bug away from a customer minting one, and there is no product reason for
    that endpoint to exist.

    The scope check lives in ONE dependency used by every route, for the same
    reason tenant scoping lives in one repository layer: a check repeated per
    endpoint is a check that will eventually be forgotten on one of them.
    Assertion K2 fires an ordinary read key at these routes and requires a 401.

WHY IT IS MOUNTED ON THE QUERY SERVICE
    It reads the same two databases the query service already holds connections
    to. A separate service would isolate it more strongly, and would also
    duplicate the ClickHouse client, the Postgres pool, the auth path and the
    deployment. The isolation that actually matters is the credential, not the
    process -- and that is enforced above.
"""

# NO `from __future__ import annotations`: FastAPI resolves Annotated types at
# import, and the string ForwardRefs it produces fail at request time instead.

import contextlib
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from control.keys import ADMIN
from control.models import Principal
from query.admin_repository import AdminRepository
from query.dtos import AdminOverview, PipelineHealth, TenantRow

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def operator(request: Request) -> Principal:
    """Resolve an ADMIN principal, or refuse.

    A read key is refused here even though it authenticates perfectly well. The
    scopes are different KINDS of credential: `read` is bounded by one org and
    this is not, so treating admin as "read, but more" would make every read key
    one bug away from a cross-tenant view.
    """
    from query.app import control_plane

    header = request.headers.get("authorization", "")
    token = request.headers.get("x-api-key") or (
        header[7:] if header.lower().startswith("bearer ") else None
    )
    principal = control_plane().authenticate(token)
    if principal is None or not principal.can(ADMIN):
        # A REFUSED attempt is recorded, and these are the most interesting
        # rows in the table: somebody presenting a credential that does not
        # work at the cross-tenant surface is exactly the event an audit log
        # exists to surface. Best-effort, because failing to log a refusal must
        # not turn a 401 into a 500 and tell the caller something they should
        # not learn from an error code.
        with contextlib.suppress(Exception):
            control_plane().audit(
                "admin.denied",
                target=principal.tenant if principal else "",
                outcome="denied",
                detail={
                    "path": request.url.path,
                    "reason": "wrong scope" if principal else "no valid key",
                },
                actor=principal,
                source_ip=_client_ip(request),
            )
        raise HTTPException(
            status_code=401,
            detail="admin scope required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def _client_ip(request: Request) -> str:
    """Best-effort caller address.

    `x-forwarded-for` is TRUSTED here only because this service sits behind our
    own edge; a caller can set it freely, so this is an operational hint and
    never an authorization input. Saying so matters: an audit field that looks
    authoritative and is not is worse than one that is obviously approximate.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return getattr(getattr(request, "client", None), "host", "") or ""


def repository(request: Request) -> AdminRepository:
    from query.app import control_plane
    from query.app import repository as span_repository

    return AdminRepository(span_repository().client, control_plane())


OperatorDep = Annotated[Principal, Depends(operator)]
AdminRepoDep = Annotated[AdminRepository, Depends(repository)]


@router.get("/overview", response_model=AdminOverview)
def overview(
    _: OperatorDep,
    repo: AdminRepoDep,
    window_minutes: Annotated[int, Query(ge=1, le=10080)] = 1440,
) -> AdminOverview:
    """Every tenant, plus pipeline health."""
    return repo.overview(window_minutes)


@router.get("/tenants", response_model=list[TenantRow])
def tenants(
    _: OperatorDep,
    repo: AdminRepoDep,
    window_minutes: Annotated[int, Query(ge=1, le=10080)] = 1440,
) -> list[TenantRow]:
    return repo.tenants(window_minutes)


@router.get("/pipeline", response_model=PipelineHealth)
def pipeline(_: OperatorDep, repo: AdminRepoDep) -> PipelineHealth:
    return repo.pipeline()


@router.get("/keys")
def keys(_: OperatorDep, repo: AdminRepoDep) -> list[dict[str, Any]]:
    """Every key. Never a secret -- only hashes are stored, so there is nothing
    here to leak even to an operator."""
    return repo.keys()


@router.get("/invites")
def invites(_: OperatorDep, repo: AdminRepoDep) -> list[dict[str, Any]]:
    return repo.invites()


@router.post("/tenants/{tenant}/quota")
def set_quota(
    tenant: str,
    request: Request,
    actor: OperatorDep,
    per_minute: Annotated[int | None, Body(embed=True)] = None,
    per_day: Annotated[int | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Override a tenant's ingest limits.

    One of the two emergency levers Architecture §8 names for a whale flooding
    ingest ("hard-quota the tenant"), so it belongs where the operator is
    already looking rather than only in a CLI they would have to go and find.

    `null` restores the plan limit; `0` means unlimited. Those are different
    values, which is why the column is NULLable rather than defaulting to 0.
    """
    from query.app import control_plane

    if not control_plane().set_quota(
        tenant, per_minute, per_day,
        actor=actor, source="console", source_ip=_client_ip(request),
    ):
        raise HTTPException(status_code=404, detail=f"no such org: {tenant}")
    return {"tenant": tenant, "per_minute": per_minute, "per_day": per_day}


@router.post("/keys/{prefix}/revoke")
def revoke(prefix: str, request: Request, actor: OperatorDep) -> dict[str, Any]:
    """Revoke a key by prefix -- the other §8 lever, and the one that is an
    emergency when a credential leaks.

    Takes effect immediately in this process and within the cache TTL (30s)
    everywhere else. That delay is the documented promise, not an accident.
    """
    from query.app import control_plane

    if not control_plane().revoke_key(
        prefix, actor=actor, source="console", source_ip=_client_ip(request)
    ):
        raise HTTPException(status_code=404, detail=f"no active key with prefix {prefix}")
    return {"prefix": prefix, "revoked": True}


@router.get("/audit")
def audit(
    _: OperatorDep,
    target: Annotated[str, Query(description="org slug or key prefix")] = "",
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[dict[str, Any]]:
    """The operator action trail.

    Readable by any admin key, deliberately. An audit log only some operators
    can read is one the others cannot use to check each other, and mutual
    visibility is most of what makes it a control rather than a formality.
    """
    from query.app import control_plane

    return control_plane().audit_trail(limit=limit, target=target)
