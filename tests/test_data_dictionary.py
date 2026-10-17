"""docs/data-dictionary.md and the schema_version lock against the head schema (plan 5.1)."""

from __future__ import annotations

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

    def test_a_consumer_tables_row_ids_are_consumer(
        self, entries: dict[str, dict[str, str]]
    ) -> None:
        """Its rows join their children and accounts only by row id (plan 5.1)."""
        consumer_tables = {
            name for name, columns in entries.items() if "consumer" in columns.values()
        }
        assert "pr_labels" in consumer_tables
        for table in Base.metadata.tables.values():
            if table.name not in consumer_tables:
                continue
            joins = {"id"} & set(table.c.keys())
            joins |= {foreign_key.parent.name for foreign_key in table.foreign_keys}
            assert {entries[table.name][column] for column in joins} == {"consumer"}, table.name
            for foreign_key in table.foreign_keys:
                assert foreign_key.column.table.name in consumer_tables, foreign_key

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
        ],
        ids=[
            "unknown use",
            "missing meaning",
            "object twice",
            "column twice",
            "row outside",
            "empty object",
        ],
    )
    def test_rejects_shapes_it_does_not_model(self, text: str, message: str) -> None:
        with pytest.raises(DictionaryError, match=message):
            read_dictionary(text)


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
