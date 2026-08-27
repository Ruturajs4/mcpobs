"""ClickHouse schema migrations.

Replaces `/docker-entrypoint-initdb.d`, which only runs against an *empty* data
directory. Adding a new .sql file and restarting silently did nothing -- proven
by adding 003_probe.sql, restarting, and finding the table absent. That is a
first-boot script, not a migration system.

Why not Alembic: it assumes transactional DDL and autogenerate-by-introspection.
ClickHouse ALTER is an asynchronous mutation, not a transaction, and Alembic
cannot introspect engines, partition keys, TTLs or codecs -- exactly the parts
of this schema that matter. Same discipline, ClickHouse-aware runner.

Applied migrations are recorded with a checksum, so an edited-after-apply file
is an error rather than a silent divergence between environments.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import NamedTuple

from clickhouse_connect.driver.client import Client

log = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).parent / "schema"


class Migration(NamedTuple):
    version: str
    name: str
    sql: str
    checksum: str

    @classmethod
    def from_path(cls, path: Path) -> Migration:
        sql = path.read_text(encoding="utf-8")
        version = path.stem.split("_", 1)[0]
        return cls(
            version=version,
            name=path.stem,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16],
        )


class MigrationError(RuntimeError):
    pass


class MigrationRunner:
    """Applies ordered .sql files exactly once, recording what ran."""

    LEDGER = "schema_migrations"

    def __init__(
        self,
        client: Client,
        database: str,
        schema_dir: Path = SCHEMA_DIR,
        override_dir: Path | None = None,
        cloud: bool = False,
    ) -> None:
        self.client = client
        self.database = database
        self.schema_dir = schema_dir
        #: A second, separate directory whose files win over `schema_dir` for
        #: the same version, without editing or forking the base schema.
        #: Exists for ClickHouse Cloud: its managed "Replicated" database
        #: engine assigns zookeeper_path/replica_name itself and REJECTS a
        #: CREATE TABLE that also specifies them explicitly (measured, not
        #: assumed -- confirmed against a real Cloud service at both 1 and 2
        #: replicas; the rejection is a property of the database engine, not
        #: the replica count) -- exactly what every self-hosted
        #: `ReplicatedMergeTree(...)` call in schema/ does, since self-hosted
        #: has no such database engine to assign them for you.
        #: `schema-cloud/` carries only the handful of migrations that touch
        #: a Replicated*MergeTree engine, each identical to its schema/
        #: counterpart except for that one ENGINE line.
        self.override_dir = override_dir
        #: Only affects the ledger table this class creates itself (every
        #: migration's own engine clause is a property of whichever .sql
        #: file discover() picks, base or override). Same Cloud-vs-
        #: self-hosted distinction as override_dir, for the one
        #: ReplicatedMergeTree this module writes in Python rather than SQL.
        self.cloud = cloud

    def run(self) -> list[str]:
        """Apply pending migrations. Returns the names applied."""
        self._ensure_database()
        self._ensure_ledger()
        applied = self._applied()
        pending = [m for m in self.discover() if m.version not in applied]

        for migration in self.discover():
            recorded = applied.get(migration.version)
            if recorded and recorded != migration.checksum:
                raise MigrationError(
                    f"migration {migration.name} was edited after being applied "
                    f"(recorded {recorded}, now {migration.checksum}). "
                    "Add a new migration instead of editing an applied one."
                )

        for migration in pending:
            log.info("applying migration %s", migration.name)
            self._execute(migration)
            self._record(migration)
        return [m.name for m in pending]

    def discover(self) -> list[Migration]:
        if not self.schema_dir.is_dir():
            return []
        base = {
            m.version: m
            for m in (Migration.from_path(p) for p in self.schema_dir.glob("*.sql"))
        }
        if self.override_dir and self.override_dir.is_dir():
            for path in self.override_dir.glob("*.sql"):
                override = Migration.from_path(path)
                if override.version not in base:
                    raise MigrationError(
                        f"{self.override_dir}/{path.name} overrides version "
                        f"{override.version!r}, which has no counterpart in "
                        f"{self.schema_dir} -- an override replaces one base "
                        "migration, it does not add a new one."
                    )
                base[override.version] = override
        return [base[version] for version in sorted(base)]

    # -- internals ---------------------------------------------------------
    def _ensure_database(self) -> None:
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self.database}")

    def _ensure_ledger(self) -> None:
        # Cloud's "Replicated" database engine assigns zookeeper_path/
        # replica_name itself and rejects a CREATE TABLE that also names
        # them -- see override_dir's docstring in __init__. Self-hosted has
        # no such database engine, so it needs them spelled out.
        engine = (
            "ReplicatedMergeTree"
            if self.cloud
            else "ReplicatedMergeTree('/clickhouse/tables/{shard}/schema_migrations', '{replica}')"
        )
        self.client.command(
            f"""
            CREATE TABLE IF NOT EXISTS {self.database}.{self.LEDGER}
            (
                version    String,
                name       String,
                checksum   String,
                applied_at DateTime DEFAULT now()
            )
            ENGINE = {engine}
            ORDER BY version
            """
        )

    def _applied(self) -> dict[str, str]:
        rows = self.client.query(
            f"SELECT version, checksum FROM {self.database}.{self.LEDGER}"
        ).result_rows
        return {version: checksum for version, checksum in rows}

    def _execute(self, migration: Migration) -> None:
        for statement in self.split_statements(migration.sql):
            self.client.command(statement)

    @staticmethod
    def strip_comments(sql: str) -> str:
        """Remove `--` comments, including trailing ones, ignoring quoted text.

        Both halves matter. A full-line comment containing a semicolon splits a
        statement in two; so does a *trailing* one, e.g.

            mcp_session_id Nullable(String),  -- see §3.2; expected NULL

        which cut a CREATE TABLE in half and produced "Unmatched parentheses".
        Quote tracking keeps a literal like 'a--b' intact.
        """
        out: list[str] = []
        for line in sql.splitlines():
            quote: str | None = None
            cut = len(line)
            index = 0
            while index < len(line):
                char = line[index]
                if quote:
                    if char == quote:
                        quote = None
                elif char in "'\"`":
                    quote = char
                elif char == "-" and line.startswith("--", index):
                    cut = index
                    break
                index += 1
            cleaned = line[:cut].rstrip()
            if cleaned:
                out.append(cleaned)
        return "\n".join(out)

    @classmethod
    def split_statements(cls, sql: str) -> list[str]:
        """Split a migration into individual statements.

        Comments are stripped BEFORE splitting on `;` -- doing it the other way
        round leaves the text after an in-comment semicolon un-prefixed, so it
        survives as a bogus statement. ClickHouse HTTP takes one statement per
        request, so this split is required.
        """
        return [s.strip() for s in cls.strip_comments(sql).split(";") if s.strip()]

    def _record(self, migration: Migration) -> None:
        self.client.insert(
            f"{self.database}.{self.LEDGER}",
            [[migration.version, migration.name, migration.checksum]],
            column_names=["version", "name", "checksum"],
        )
