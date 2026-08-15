"""Control-plane value objects.

Pydantic rather than raw tuples for the same reason `normalizer/models.py` is:
these cross process boundaries -- the ingest gateway and the query API both
resolve keys -- and a positional tuple is how the wrong column ends up in the
wrong field the day someone adds one.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Org(_Row):
    id: int
    #: Equals `tenant_id` in ClickHouse and the Kafka partition key.
    slug: str
    name: str = ""
    plan: str = "trial"


class Project(_Row):
    id: int
    org_id: int
    slug: str
    name: str = ""
    environment: str = "production"


class User(_Row):
    id: int
    org_id: int
    email: str
    name: str = ""
    role: str = "member"


class Invite(_Row):
    id: int
    org_id: int
    email: str
    role: str = "member"
    expires_at: datetime
    accepted_at: datetime | None = None


class Principal(_Row):
    """Who an authenticated request is, resolved from one API key.

    THE ONLY SOURCE OF TENANCY IN THE SYSTEM. Every read and every write scopes
    to the values on this object, and nothing anywhere accepts a tenant from a
    caller-supplied parameter or resource attribute. That is what makes tenant
    isolation a property of the code rather than a habit of whoever wrote the
    last endpoint.
    """

    key_id: int
    org_id: int
    #: `tenant_id`. Named for what it is here and what it means downstream.
    tenant: str
    project: str
    environment: str
    scopes: tuple[str, ...] = ()

    def can(self, scope: str) -> bool:
        return scope in self.scopes


class IssuedKey(_Row):
    """A newly minted key. The secret exists in this object and nowhere else.

    Returned exactly once, at creation. There is no endpoint that reveals it
    again, because the database only holds its hash -- which is the point, and
    is worth stating in a type so nobody designs a "show key" screen.
    """

    prefix: str
    token: str = Field(repr=False)
    project: str
    environment: str
    scopes: tuple[str, ...]
