"""Control-plane admin: orgs, invites, projects, keys.

INVITE-ONLY MEANS SOMEONE HAS TO BE FIRST
    There is no self-service signup, so the first org and the first user cannot
    come from the product. They come from here -- an operator with database
    access, which is the honest bootstrap for an invite-only system and the same
    shape every invite-only product uses.

    `bootstrap` is therefore the ONLY command that creates an org without an
    invite, and it is not reachable over HTTP at all. That is deliberate: the
    moment there is an endpoint that mints an org, invite-only is a policy
    rather than a property.

    python scripts/admin.py bootstrap --org acme --email you@acme.com
    python scripts/admin.py invite --org acme --email teammate@acme.com
    python scripts/admin.py accept --code <code> --name "Team Mate"
    python scripts/admin.py key --org acme --project default --scopes ingest,read
    python scripts/admin.py revoke --prefix a1b2c3d4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import ControlPlane
from control.keys import parse_scopes


def _org_id(cp: ControlPlane, slug: str) -> int:
    row = cp._one("SELECT id FROM orgs WHERE slug = %s", (slug,))
    if row is None:
        raise SystemExit(f"no such org: {slug}")
    return int(row["id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap", help="create an org, its first project and an invite")
    boot.add_argument("--org", required=True)
    boot.add_argument("--email", required=True)
    boot.add_argument("--project", default="default")
    boot.add_argument("--environment", default="production")

    inv = sub.add_parser("invite", help="invite someone to an existing org")
    inv.add_argument("--org", required=True)
    inv.add_argument("--email", required=True)
    inv.add_argument("--role", default="member")

    acc = sub.add_parser("accept", help="redeem an invite code")
    acc.add_argument("--code", required=True)
    acc.add_argument("--name", default="")

    key = sub.add_parser("key", help="issue an API key")
    key.add_argument("--org", required=True)
    key.add_argument("--project", default="default")
    key.add_argument("--scopes", default="ingest")
    key.add_argument("--name", default="")

    quota = sub.add_parser("quota", help="show or override an org's ingest limits")
    quota.add_argument("--org", required=True)
    quota.add_argument("--plan", default=None, help="trial | pro | enterprise")
    quota.add_argument("--per-minute", type=int, default=None,
                       help="span/minute override; 0 = unlimited")
    quota.add_argument("--per-day", type=int, default=None,
                       help="span/day override; 0 = unlimited")
    quota.add_argument("--clear", action="store_true", help="restore the plan limits")

    rev = sub.add_parser("revoke", help="revoke an API key by prefix")
    rev.add_argument("--prefix", required=True)

    sub.add_parser("migrate", help="apply the control-plane schema")

    dev = sub.add_parser(
        "devkeys",
        help="ensure the local dev org and keys exist; write them to .mcpobs-keys.env",
    )
    dev.add_argument("--org", default="local")
    dev.add_argument("--project", default="local")

    args = parser.parse_args()
    cp = ControlPlane()
    cp.wait_ready()
    cp.migrate()

    if args.command == "migrate":
        print("control-plane schema applied")
        return 0

    if args.command == "bootstrap":
        org = cp.create_org(args.org)
        project = cp.create_project(org.id, args.project, environment=args.environment)
        invite, code = cp.invite(org.id, args.email, role="admin")
        print(f"  org       {org.slug} (id {org.id})")
        print(f"  project   {project.slug} / {project.environment}")
        print(f"  invited   {invite.email} as admin, expires {invite.expires_at:%Y-%m-%d}")
        print()
        # Shown ONCE. The database holds only its hash, so there is no command
        # that can print it again -- which is the point, not an omission.
        print(f"  invite code (shown once): {code}")
        print(f"  redeem:  python scripts/admin.py accept --code {code}")
        return 0

    if args.command == "invite":
        invite, code = cp.invite(_org_id(cp, args.org), args.email, role=args.role)
        print(f"  invited {invite.email} to {args.org} as {invite.role}")
        print(f"  invite code (shown once): {code}")
        return 0

    if args.command == "accept":
        user = cp.accept_invite(args.code, name=args.name)
        if user is None:
            # One message for all three failures. Telling a holder WHICH of
            # "unknown", "already used" and "expired" applies would confirm that
            # a code they should not have was once real.
            print("  invite is not valid (unknown, already used, or expired)")
            return 1
        print(f"  {user.email} joined org {user.org_id} as {user.role}")
        return 0

    if args.command == "key":
        org_id = _org_id(cp, args.org)
        project = cp._one(
            "SELECT id FROM projects WHERE org_id = %s AND slug = %s", (org_id, args.project)
        )
        if project is None:
            raise SystemExit(f"no such project: {args.org}/{args.project}")
        scopes = parse_scopes(args.scopes)
        if not scopes:
            raise SystemExit("no valid scopes given (ingest, read)")
        issued = cp.issue_key(org_id, int(project["id"]), scopes=scopes, name=args.name)
        print(f"  project   {args.org}/{issued.project} ({issued.environment})")
        print(f"  scopes    {', '.join(issued.scopes)}")
        print(f"  prefix    {issued.prefix}   <- use this to revoke")
        print()
        print(f"  API key (shown once): {issued.token}")
        return 0

    if args.command == "devkeys":
        # Idempotent, and it has to be: `make demo` and `make verify` call it on
        # every run. Keys cannot be re-read -- only their hashes are stored --
        # so this reuses the cached pair when they still authenticate and mints
        # a fresh pair when they do not. That is exactly how a customer holds a
        # key: in a secrets file, because the server cannot tell them again.
        #
        # The org is `local`, matching the tenant_id already in ClickHouse, so
        # turning auth on did not orphan every span written before it existed.
        cache = Path(__file__).resolve().parent.parent / ".mcpobs-keys.env"
        existing = {}
        if cache.exists():
            for line in cache.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    existing[key.strip()] = value.strip()

        usable = all(
            cp.authenticate(existing.get(name)) is not None
            for name in ("MCPOBS_INGEST_KEY", "MCPOBS_READ_KEY", "MCPOBS_ADMIN_KEY")
        )
        if usable:
            print(f"  reusing keys in {cache.name}")
            return 0

        org = cp.create_org(args.org)
        project = cp.create_project(org.id, args.project, environment="local")
        ingest = cp.issue_key(org.id, project.id, scopes=("ingest",), name="dev ingest")
        read = cp.issue_key(org.id, project.id, scopes=("read",), name="dev read")
        # An admin key is cross-tenant, so locally it is convenient and in
        # production it is a credential an operator holds deliberately. It is
        # minted here ONLY because `devkeys` already requires database access --
        # the same bar the CLI enforces everywhere else.
        admin = cp.issue_key(org.id, project.id, scopes=("admin",), name="dev admin")
        cache.write_text(
            "\n".join([
                "# Local development keys. Gitignored -- these are credentials.",
                "# Regenerate with: python scripts/admin.py devkeys",
                f"MCPOBS_INGEST_KEY={ingest.token}",
                f"MCPOBS_READ_KEY={read.token}",
                f"MCPOBS_ADMIN_KEY={admin.token}",
                "",
            ]),
            encoding="utf-8",
        )
        print(
            f"  wrote {cache.name}: ingest {ingest.prefix}, "
            f"read {read.prefix}, admin {admin.prefix}"
        )
        return 0

    if args.command == "quota":
        from control.quota import QuotaEnforcer

        if args.plan:
            cp.set_plan(args.org, args.plan)
        if args.clear:
            cp.set_quota(args.org, None, None)
        elif args.per_minute is not None or args.per_day is not None:
            cp.set_quota(args.org, args.per_minute, args.per_day)

        row = cp._one(
            "SELECT plan, quota_spans_per_minute, quota_spans_per_day "
            "FROM orgs WHERE slug = %s", (args.org,)
        )
        if row is None:
            raise SystemExit(f"no such org: {args.org}")
        plan = QuotaEnforcer.plan_for(row["plan"])
        per_minute = row["quota_spans_per_minute"]
        per_day = row["quota_spans_per_day"]
        show = lambda v: "unlimited" if v == 0 else f"{v:,}"  # noqa: E731
        print(f"  org    {args.org}")
        print(f"  plan   {plan.name}")
        print(f"  minute {show(per_minute if per_minute is not None else plan.spans_per_minute)}"
              f"{'  (override)' if per_minute is not None else ''}")
        print(f"  day    {show(per_day if per_day is not None else plan.spans_per_day)}"
              f"{'  (override)' if per_day is not None else ''}")
        return 0

    if args.command == "revoke":
        if cp.revoke_key(args.prefix):
            print(f"  revoked {args.prefix}")
            print("  takes effect immediately here; other processes within 30s (cache TTL)")
            return 0
        print(f"  no active key with prefix {args.prefix}")
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
