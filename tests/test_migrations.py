"""Migration runner.

Exists because `/docker-entrypoint-initdb.d` only runs against an empty data
directory: adding a new .sql file and restarting silently did nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from normalizer.migrations import Migration, MigrationRunner

SCHEMA_DIR = Path(__file__).parent.parent / "normalizer" / "schema"


class TestSplitStatements:
    def test_semicolon_inside_a_comment_does_not_split(self) -> None:
        """The bug that broke the first migration run.

        Stripping comments AFTER splitting leaves the text following an
        in-comment semicolon un-prefixed, so it survives as a bogus statement.
        """
        sql = """
        -- keep these columns; they are NULL/empty today.
        CREATE TABLE t (x UInt8) ENGINE = Memory;
        """
        statements = MigrationRunner.split_statements(sql)
        assert len(statements) == 1
        assert statements[0].startswith("CREATE TABLE t")

    def test_semicolon_in_a_trailing_comment_does_not_split(self) -> None:
        """The second half of the same bug -- produced 'Unmatched parentheses'."""
        sql = """
        CREATE TABLE t
        (
            a UInt8,
            b Nullable(String),   -- see section 3.2; expected NULL
            c UInt8
        )
        ENGINE = Memory;
        """
        statements = MigrationRunner.split_statements(sql)
        assert len(statements) == 1
        assert statements[0].count("(") == statements[0].count(")")

    def test_double_dash_inside_a_string_literal_is_kept(self) -> None:
        sql = "CREATE TABLE t (x String DEFAULT 'a--b') ENGINE = Memory;"
        assert "a--b" in MigrationRunner.split_statements(sql)[0]

    def test_multiple_statements(self) -> None:
        sql = "CREATE DATABASE a; CREATE TABLE a.t (x UInt8) ENGINE = Memory;"
        assert len(MigrationRunner.split_statements(sql)) == 2

    def test_comment_only_file_yields_nothing(self) -> None:
        assert MigrationRunner.split_statements("-- just a note\n-- and another") == []

    def test_trailing_semicolon_does_not_yield_empty(self) -> None:
        assert len(MigrationRunner.split_statements("SELECT 1;")) == 1


class TestRealSchemaFiles:
    """The shipped migrations must actually be splittable and ordered."""

    def test_every_schema_file_parses(self) -> None:
        for path in sorted(SCHEMA_DIR.glob("*.sql")):
            statements = MigrationRunner.split_statements(path.read_text(encoding="utf-8"))
            assert statements, f"{path.name} produced no statements"
            for statement in statements:
                assert statement.upper().startswith(("CREATE", "ALTER", "DROP", "INSERT")), (
                    f"{path.name} produced a bogus fragment: {statement[:60]!r}"
                )
                assert statement.count("(") == statement.count(")"), (
                    f"{path.name} statement has unbalanced parentheses"
                )

    def test_versions_are_unique_and_ordered(self) -> None:
        migrations = [Migration.from_path(p) for p in sorted(SCHEMA_DIR.glob("*.sql"))]
        versions = [m.version for m in migrations]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)

    def test_checksum_changes_when_content_changes(self, tmp_path: Path) -> None:
        """Detecting an edited-after-apply migration depends on this."""
        path = tmp_path / "001_x.sql"
        path.write_text("CREATE TABLE a (x UInt8) ENGINE = Memory;")
        first = Migration.from_path(path).checksum
        path.write_text("CREATE TABLE a (x UInt16) ENGINE = Memory;")
        assert Migration.from_path(path).checksum != first

    def test_version_parsed_from_filename(self, tmp_path: Path) -> None:
        path = tmp_path / "007_add_rollups.sql"
        path.write_text("SELECT 1;")
        migration = Migration.from_path(path)
        assert migration.version == "007"
        assert migration.name == "007_add_rollups"


class TestDiscovery:
    def test_missing_schema_dir_is_not_an_error(self, tmp_path: Path) -> None:
        runner = MigrationRunner(client=None, database="x", schema_dir=tmp_path / "nope")
        assert runner.discover() == []

    def test_discovery_is_sorted(self, tmp_path: Path) -> None:
        for name in ("003_c.sql", "001_a.sql", "002_b.sql"):
            (tmp_path / name).write_text("SELECT 1;")
        runner = MigrationRunner(client=None, database="x", schema_dir=tmp_path)
        assert [m.version for m in runner.discover()] == ["001", "002", "003"]


@pytest.mark.parametrize("filename", ["001_spans_raw.sql", "002_dead_letter.sql"])
def test_expected_migrations_are_shipped(filename: str) -> None:
    assert (SCHEMA_DIR / filename).exists()
