"""docs/data-dictionary.md and the schema_version lock against the head schema (plan 5.1)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from github_activity_db.db.models import Base
from github_activity_db.db.schema import SCHEMA_VERSION
from tests.data_dictionary import (
    DICTIONARY,
    DictionaryError,
    consumer_pairs,
    database_columns,
    differences,
    lock_differences,
    read_dictionary,
    read_lock,
)

ENTRY = """\
## Tables

### `t`

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `name` | consumer | Name |

## Views

### `v_t`

| Column | Use | Meaning |
|--------|-----|---------|
| `name` | consumer | Name |
"""


# Sync bookkeeping, the tag tables nothing writes yet (D17), and alembic's own table.
INTERNAL_TABLES = (
    "sync_runs",
    "repo_sync_state",
    "sync_pending",
    "sync_failures",
    "user_tags",
    "pr_user_tags",
    "alembic_version",
)


@pytest.fixture(scope="module")
def entries() -> dict[str, dict[str, str]]:
    return read_dictionary(DICTIONARY.read_text())


class TestDictionary:
    def test_every_object_and_column_at_head_has_an_entry_and_none_is_orphaned(
        self, entries: dict[str, dict[str, str]], head_template: Path
    ) -> None:
        at_head = database_columns(head_template)
        assert {"alembic_version", "pull_requests", "v_pr_reviewers"} <= at_head.keys()
        assert differences(entries, at_head) == []

    def test_every_view_column_is_consumer(self, entries: dict[str, dict[str, str]]) -> None:
        views = {name: columns for name, columns in entries.items() if name.startswith("v_")}
        assert len(views) == 4
        assert {use for columns in views.values() for use in columns.values()} == {"consumer"}

    def test_bookkeeping_tag_and_migration_tables_are_internal(
        self, entries: dict[str, dict[str, str]]
    ) -> None:
        for table in INTERNAL_TABLES:
            assert set(entries[table].values()) == {"internal"}, table

    def test_row_ids_are_consumer_exactly_where_consumers_join_on_them(
        self, entries: dict[str, dict[str, str]]
    ) -> None:
        """A consumer table's foreign keys, and the row ids they reference, are consumer: a pull
        request's labels, reviews, and author are reachable only through them (plan 5.1). Any
        other row id is internal."""
        consumer_tables = {
            name for name, columns in entries.items() if "consumer" in columns.values()
        }
        foreign_keys = [
            foreign_key
            for table in Base.metadata.tables.values()
            if table.name in consumer_tables
            for foreign_key in table.foreign_keys
        ]
        joins = {(fk.parent.table.name, fk.parent.name) for fk in foreign_keys}
        joins |= {(fk.column.table.name, fk.column.name) for fk in foreign_keys}
        assert {("pr_labels", "pr_id"), ("pull_requests", "id")} <= joins
        assert ("pr_reviews", "id") not in joins
        assert all(table in consumer_tables for table, _column in joins), joins
        for table in Base.metadata.tables.values():
            if table.name not in consumer_tables:
                continue
            for column in table.columns:
                if column.name != "id" and not column.foreign_keys:
                    continue
                expected = "consumer" if (table.name, column.name) in joins else "internal"
                assert entries[table.name][column.name] == expected, (table.name, column.name)

    @pytest.mark.parametrize(
        ("database", "expected"),
        [
            ({"t": ["id", "name"], "v_t": ["name"]}, []),
            ({"t": ["id", "name", "added"], "v_t": ["name"]}, ["no entry for t.added"]),
            ({"t": ["id"], "v_t": ["name"]}, ["t.name is not at head"]),
            ({"t": ["id", "name"], "v_t": ["name"], "v_new": ["x"]}, ["no entry for v_new"]),
            ({"t": ["id", "name"]}, ["v_t is not at head"]),
        ],
        ids=["equal", "column at head only", "entry only", "view at head only", "view entry only"],
    )
    def test_differences_are_reported_both_ways(
        self, database: dict[str, list[str]], expected: list[str]
    ) -> None:
        assert differences(read_dictionary(ENTRY), database) == expected

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (
                ENTRY.replace("| `name` | consumer | Name |", "| `name` | public | Name |", 1),
                "use 'public'",
            ),
            (
                ENTRY.replace("| `name` | consumer | Name |", "| `name` | consumer |", 1),
                "not a `| column",
            ),
            (ENTRY + "\n### `t`\n\n| `x` | internal | X |\n", "t is listed twice"),
            (
                ENTRY.replace(
                    "| `id` | internal | Row id |",
                    "| `id` | internal | Row id |\n| `id` | internal | Again |",
                ),
                "t.id is listed twice",
            ),
            ("## Tables\n\n| `x` | internal | X |\n", "outside an object"),
            (ENTRY + "\n### `empty`\n\nNo rows.\n", "objects without columns: empty"),
            # An orphan row written slightly wrong would otherwise be skipped, not compared.
            (ENTRY.replace("| `name` |", "| name |", 1), "not a `| column"),
            (ENTRY.replace("| `name` |", " | `name` |", 1), "not a `| column"),
            (ENTRY.replace("| `name` |", "|`name`|", 1), "not a `| column"),
            # GFM continues a table on a non-blank line without the leading pipe.
            (ENTRY.replace("| `name` | consumer |", "`name` | consumer |", 1), "continues a table"),
        ],
        ids=[
            "unknown use",
            "missing meaning",
            "object twice",
            "column twice",
            "row outside",
            "empty object",
            "unquoted column",
            "indented row",
            "unspaced row",
            "row without its leading pipe",
        ],
    )
    def test_rejects_shapes_it_does_not_model(self, text: str, message: str) -> None:
        with pytest.raises(DictionaryError, match=message):
            read_dictionary(text)


def test_database_columns_reads_every_user_object(tmp_path: Path) -> None:
    """Names that look like SQLite's own are kept, SQLite's own are not, generated columns
    are listed."""
    path = tmp_path / "planted.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sqliteXstats (x INTEGER)")
        connection.execute("CREATE TABLE SQLite1 (x INTEGER)")
        connection.execute("CREATE TABLE counted (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        connection.execute("CREATE TABLE t (a INTEGER, g INTEGER GENERATED ALWAYS AS (a + 1))")
        connection.execute("CREATE VIEW v_t AS SELECT g FROM t")
        connection.execute("INSERT INTO counted DEFAULT VALUES")
        connection.commit()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
        ).fetchall() == [(1,)]
    assert database_columns(path) == {
        "SQLite1": ["x"],
        "counted": ["id"],
        "sqliteXstats": ["x"],
        "t": ["a", "g"],
        "v_t": ["g"],
    }


class TestSchemaLock:
    def test_the_current_entry_lists_the_consumer_columns_at_head(
        self, entries: dict[str, dict[str, str]]
    ) -> None:
        lock = read_lock()
        assert sorted(lock, key=int) == [str(v) for v in range(1, SCHEMA_VERSION + 1)]
        assert lock_differences(lock[str(SCHEMA_VERSION)], consumer_pairs(entries)) == []

    def test_a_listed_pair_that_is_not_consumer_at_head_is_reported(self) -> None:
        consumer = consumer_pairs(read_dictionary(ENTRY))
        entry = [*consumer, ["t", "id"], ["v_t", "gone"]]
        assert lock_differences(entry, consumer) == [
            "t.id is not a consumer column at head",
            "v_t.gone is not a consumer column at head",
        ]

    def test_a_consumer_pair_missing_from_the_entry_is_reported(self) -> None:
        consumer = consumer_pairs(read_dictionary(ENTRY))
        assert consumer == [["t", "name"], ["v_t", "name"]]
        assert lock_differences([["t", "name"]], consumer) == ["v_t.name is missing from the entry"]
