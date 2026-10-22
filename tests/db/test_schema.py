"""The head schema against the models and the frozen view SQL (redesign plan 5.1).

``alembic check`` compares neither views nor CHECK constraints, so these tests compare a
database migrated to head with one ``metadata.create_all`` builds, structurally, and each view
with the newest migration's frozen SQL. Where the readings must tell two schemas apart, the two
are databases the migrations built at neighbouring revisions; a shape no revision has is read
from a scratch database of valid SQL.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sqlite3
import tokenize
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, get_args

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Table, UniqueConstraint, create_engine, insert, pool
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

from github_activity_db.db.models import (
    Base,
    RepoSyncState,
    SyncFailure,
    SyncPending,
)
from github_activity_db.source.records import AccountType, ReviewState
from tests.db.alembic_support import alembic_config, query, upgrade
from tests.db.schema_support import (
    ALEMBIC_OBJECTS,
    check_constraints,
    create_table_items,
    delete_actions,
    foreign_key_columns,
    frozen_views,
    named_constraints,
    nullable_columns,
    objects_outside_the_comparison,
    schema_of,
    schema_tables,
    unindexed_foreign_keys,
    unique_sets,
    view_differences,
    views,
)
from tests.db.seed import T0, Seed, connect

STEP3A = "33f8570f6540"
STEP3B1 = "94a52e4f1d57"
LAST_RUN_ID = "2d2ba76b177c"
SQLITE = sqlite.dialect()


def _tampered(source: Path, target: Path, *statements: str) -> Path:
    shutil.copyfile(source, target)
    with closing(sqlite3.connect(target)) as connection, connection:
        for statement in statements:
            connection.execute(statement)
    return target


def _redefined(database: Path, table: str, old: str, new: str) -> tuple[str, str]:
    """Statements that recreate ``table`` with ``old`` replaced by ``new`` in its definition."""
    with closing(sqlite3.connect(database)) as connection:
        ((sql,),) = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,))
    assert old in sql, (old, sql)
    return f"DROP TABLE {table}", sql.replace(old, new)


@pytest.fixture(scope="module")
def models_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("models") / "models.db"
    engine = create_engine(f"sqlite:///{path}", poolclass=pool.NullPool)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


@pytest.fixture(scope="module")
def revisions(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Databases the migrations built at Step 3a, at Step 3b-1, and at the revision after it;
    read-only, so no test changes one."""
    folder = tmp_path_factory.mktemp("revisions")
    built = {}
    for revision in (STEP3A, STEP3B1, LAST_RUN_ID):
        built[revision] = folder / f"{revision}.db"
        upgrade(built[revision], revision)
        built[revision].chmod(0o444)
    return built


def revision_module(database: Path, revision: str) -> Any:
    """The revision file's module, for the constants it lists (CHANGED_TABLES, VIEWS, ...)."""
    return ScriptDirectory.from_config(alembic_config(database)).get_revision(revision).module


def scratch(path: Path, *statements: str) -> Path:
    """A database of valid SQL, which no migration built, for a reader's exact readings."""
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in statements:
            connection.execute(statement)
    return path


def is_rowid(table: Table) -> bool:
    """Whether the table's primary key is its rowid: one column declared INTEGER."""
    (*others, first) = table.primary_key.columns
    return not others and str(first.type.compile(dialect=SQLITE)) == "INTEGER"


@pytest.mark.parametrize(
    "separator", [" ", "\t", "\n", "\r\n"], ids=["space", "tab", "newline", "crlf"]
)
def test_a_named_check_is_read_whatever_whitespace_separates_its_words(
    tmp_path: Path, separator: str
) -> None:
    """SQLite accepts any whitespace between a constraint's words and keeps the text as written."""
    database = tmp_path / "probe.db"
    item = f"CONSTRAINT{separator}ck_probe{separator}CHECK{separator}(value != 'blocked')"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            f"CREATE TABLE probe (value TEXT, {item}, "
            "CONSTRAINT ck_tight CHECK(value != 'x'), CONSTRAINT uq_probe UNIQUE (value))"
        )
    assert named_constraints(database, "probe") == {
        "ck_probe": item,
        "ck_tight": "CONSTRAINT ck_tight CHECK(value != 'x')",
        "uq_probe": "CONSTRAINT uq_probe UNIQUE (value)",
    }
    assert check_constraints(database, "probe") == {
        "ck_probe": item,
        "ck_tight": "CONSTRAINT ck_tight CHECK(value != 'x')",
    }


# Shapes no table at head has, each with whitespace inside a literal, which is data.
TABLE = (
    "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
    "CONSTRAINT ck_t_name CHECK (name != 'a  b'))"
)
PARTIAL_INDEX = "CREATE INDEX ix_t_partial ON t (name) WHERE name != 'a  b'"
EXPRESSION_INDEX = "CREATE INDEX ix_t_lower ON t (lower(name))"
TRIGGER = "CREATE TRIGGER tr_t BEFORE INSERT ON t BEGIN SELECT RAISE(ABORT, 'a  b'); END"
VIEW = "CREATE VIEW v AS SELECT 'a  b' AS value"
STRICT_TABLE = "CREATE TABLE s (k TEXT PRIMARY KEY, v INTEGER) STRICT"
WITHOUT_ROWID_TABLE = "CREATE TABLE w (k TEXT PRIMARY KEY) WITHOUT ROWID"


def test_the_readings_hold_what_no_pragma_reports(tmp_path: Path) -> None:
    """A CHECK's text, a partial index's WHERE, an index expression, a trigger, and a view, each
    exactly as written, and the STRICT and WITHOUT ROWID flags."""
    database = scratch(
        tmp_path / "scratch.db",
        TABLE,
        PARTIAL_INDEX,
        EXPRESSION_INDEX,
        TRIGGER,
        VIEW,
        STRICT_TABLE,
        WITHOUT_ROWID_TABLE,
    )
    read = schema_of(database)
    assert sorted(read) == ["s", "t", "w"]
    assert read["t"]["definition"] == [
        "CONSTRAINT ck_t_name CHECK (name != 'a  b')",
        "id INTEGER PRIMARY KEY",
        "name TEXT NOT NULL",
    ]
    # (created name and SQL, unique, origin, partial, index_xinfo rows); -2 is an expression.
    assert read["t"]["indexes"] == [
        (
            ("ix_t_lower", EXPRESSION_INDEX),
            0,
            "c",
            0,
            ((0, -2, None, 0, "BINARY", 1), (1, -1, None, 0, "BINARY", 0)),
        ),
        (
            ("ix_t_partial", PARTIAL_INDEX),
            0,
            "c",
            1,
            ((0, 1, "name", 0, "BINARY", 1), (1, -1, None, 0, "BINARY", 0)),
        ),
    ]
    assert read["t"]["triggers"] == [("tr_t", TRIGGER)]
    assert views(database) == {"v": VIEW}
    # (type, ncol, without rowid, strict)
    assert [read[table]["flags"] for table in ("t", "s", "w")] == [
        [("table", 2, 0, 0)],
        [("table", 2, 0, 1)],
        [("table", 1, 1, 0)],
    ]


class TestModelsComparison:
    def test_head_matches_the_models(self, head_template: Path, models_database: Path) -> None:
        migrated, models = schema_of(head_template), schema_of(models_database)
        assert sorted(migrated) == sorted(models)
        for table in models:
            assert migrated[table] == models[table], table

    def test_the_readings_at_head_are_the_models_declarations(self, head_template: Path) -> None:
        """Each table's reading against SQLAlchemy's metadata, not a pragma: its columns, its
        foreign keys, its created indexes as the models compile them, the items of its compiled
        definition, its flags, and no trigger."""
        read = schema_of(head_template)
        assert sorted(read) == sorted(Base.metadata.tables)
        for name, table in Base.metadata.tables.items():
            reading = read[name]
            assert [(row[1], row[2], bool(row[3]), bool(row[5])) for row in reading["columns"]] == [
                (
                    column.name,
                    str(column.type.compile(dialect=SQLITE)),
                    not column.nullable,
                    column.primary_key,
                )
                for column in table.columns
            ], name
            assert reading["foreign_keys"] == sorted(
                (
                    tuple(
                        (
                            seq,
                            element.column.table.name,
                            element.parent.name,
                            element.column.name,
                            key.onupdate or "NO ACTION",
                            key.ondelete or "NO ACTION",
                            "NONE",
                        )
                        for seq, element in enumerate(key.elements)
                    )
                    for key in table.foreign_key_constraints
                ),
                key=repr,
            ), name
            assert sorted(created for created, *_rest in reading["indexes"] if created) == sorted(
                (index.name, str(CreateIndex(index).compile(dialect=SQLITE)).strip())
                for index in table.indexes
            ), name
            assert reading["definition"] == sorted(
                create_table_items(str(CreateTable(table).compile(dialect=SQLITE)))
            ), name
            assert set(check_constraints(head_template, name)) == {
                constraint.name
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }, name
            options = table.dialect_options["sqlite"]
            without_rowid, strict = int(not options["with_rowid"]), int(options["strict"])
            assert reading["flags"] == [("table", len(table.columns), without_rowid, strict)], name
            assert reading["triggers"] == [], name

    def test_the_comparison_reads_every_table_and_check(self, models_database: Path) -> None:
        """The readings hold the CHECKs and tables the models declare."""
        assert set(schema_tables(models_database)) == set(Base.metadata.tables)
        checks = {
            name
            for table in Base.metadata.tables
            for name in check_constraints(models_database, table)
        }
        assert checks == set(CHECKS) | PAIRED_CHECKS

    def test_a_revision_s_changes_are_read_in_the_tables_it_lists(
        self, revisions: dict[str, Path]
    ) -> None:
        """2d2ba76b177c adds sync_failures.last_run_id, its foreign key to sync_runs, and its
        index; the readings before and after it differ in that table alone, in those readings."""
        before, after = schema_of(revisions[STEP3B1]), schema_of(revisions[LAST_RUN_ID])
        assert sorted(before) == sorted(after)
        changed = {table for table in after if before[table] != after[table]}
        assert changed == set(revision_module(revisions[LAST_RUN_ID], LAST_RUN_ID).CHANGED_TABLES)
        old, new = before["sync_failures"], after["sync_failures"]
        assert [key for key in new if old[key] != new[key]] == [
            "columns",
            "indexes",
            "foreign_keys",
            "definition",
            "flags",
        ]
        assert {row[1] for row in new["columns"]} - {row[1] for row in old["columns"]} == {
            "last_run_id"
        }
        assert {created[0] for created, *_ in new["indexes"] if created} - {
            created[0] for created, *_ in old["indexes"] if created
        } == {"ix_sync_failures_last_run_id"}
        assert set(new["foreign_keys"]) - set(old["foreign_keys"]) == {
            ((0, "sync_runs", "last_run_id", "id", "NO ACTION", "NO ACTION", "NONE"),)
        }
        assert set(old["foreign_keys"]) <= set(new["foreign_keys"])

    def test_a_check_s_changed_text_is_read(self, revisions: dict[str, Path]) -> None:
        """94a52e4f1d57 widened two CHECKs, keeping their names."""
        before, after = revisions[STEP3A], revisions[STEP3B1]
        changed = {
            name
            for table in set(schema_tables(before)) & set(schema_tables(after))
            for name, item in check_constraints(before, table).items()
            if check_constraints(after, table).get(name, item) != item
        }
        assert changed == {"ck_github_users_type", "ck_pull_requests_state"}
        old, new = schema_of(before), schema_of(after)
        assert old["github_users"]["definition"] != new["github_users"]["definition"]
        assert old["pull_requests"]["definition"] != new["pull_requests"]["definition"]

    @pytest.mark.parametrize(
        ("table", "old", "new"),
        [
            pytest.param(
                "automation_emails",
                "CONSTRAINT ck_automation_emails_email_lowercase CHECK",
                "CONSTRAINT ck_automation_emails_lower CHECK",
                id="renamed CHECK",
            ),
            pytest.param(
                "automation_emails",
                "email = lower(email)",
                "email = upper(email)",
                id="changed CHECK text",
            ),
            pytest.param(
                "automation_emails",
                "CONSTRAINT ck_automation_emails_email_lowercase ",
                "",
                id="unnamed CHECK",
            ),
            pytest.param("pr_files", "path TEXT NOT NULL", "path TEXT", id="nullability"),
            pytest.param(
                "pr_commits",
                "authored_at DATETIME NOT NULL",
                "authored_at TEXT NOT NULL",
                id="column type",
            ),
            pytest.param("pr_labels", " ON DELETE CASCADE", "", id="foreign key action"),
            pytest.param(
                "pr_labels",
                "CONSTRAINT fk_pr_labels_pr_id_pull_requests",
                "CONSTRAINT fk_pr_labels_pr",
                id="foreign key name",
            ),
            pytest.param(
                "pr_reviews",
                "CONSTRAINT uq_pr_reviews_github_id UNIQUE",
                "UNIQUE",
                id="unnamed UNIQUE",
            ),
        ],
    )
    def test_a_planted_difference_is_reported(
        self,
        head_template: Path,
        models_database: Path,
        tmp_path: Path,
        table: str,
        old: str,
        new: str,
    ) -> None:
        tampered = _tampered(
            head_template, tmp_path / "tampered.db", *_redefined(head_template, table, old, new)
        )
        assert schema_of(tampered)[table] != schema_of(models_database)[table]

    def test_a_missing_index_is_reported(
        self, head_template: Path, models_database: Path, tmp_path: Path
    ) -> None:
        tampered = _tampered(
            head_template, tmp_path / "tampered.db", "DROP INDEX ix_pr_reviews_reviewer_id"
        )
        assert schema_of(tampered)["pr_reviews"] != schema_of(models_database)["pr_reviews"]

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(
                ["CREATE INDEX ix_probe ON pr_labels (name) WHERE name <> 'a'"],
                ["CREATE INDEX ix_probe ON pr_labels (name) WHERE name <> 'b'"],
                id="partial index WHERE",
            ),
            pytest.param(
                ["CREATE INDEX ix_probe ON pr_labels (lower(name))"],
                ["CREATE INDEX ix_probe ON pr_labels (upper(name))"],
                id="expression index",
            ),
            pytest.param(
                [],
                ["CREATE TRIGGER tr_probe AFTER DELETE ON pr_labels BEGIN SELECT 1; END"],
                id="trigger",
            ),
        ],
    )
    def test_index_sql_and_triggers_are_compared(
        self, head_template: Path, tmp_path: Path, first: list[str], second: list[str]
    ) -> None:
        """Shapes no pragma reports: a partial index's WHERE, an expression, a trigger."""
        one = _tampered(head_template, tmp_path / "one.db", *first)
        two = _tampered(head_template, tmp_path / "two.db", *second)
        assert schema_of(one)["pr_labels"] != schema_of(two)["pr_labels"]

    @pytest.mark.parametrize("kind", ["check", "index", "trigger"])
    def test_whitespace_inside_a_literal_is_a_difference(
        self, head_template: Path, tmp_path: Path, kind: str
    ) -> None:
        """Two schemas whose SQL differs only in a literal's whitespace read differently."""

        def read(literal: str, name: str) -> dict[str, Any]:
            statements: tuple[str, ...]
            if kind == "check":
                statements = _redefined(
                    head_template,
                    "pr_files",
                    "path TEXT NOT NULL",
                    f"path TEXT NOT NULL CHECK (path != {literal})",
                )
            elif kind == "index":
                statements = (f"CREATE INDEX ix_probe ON pr_files (path) WHERE path != {literal}",)
            else:
                statements = (
                    "CREATE TRIGGER tr_probe BEFORE INSERT ON pr_files "
                    f"BEGIN SELECT RAISE(ABORT, {literal}); END",
                )
            tampered = _tampered(head_template, tmp_path / f"{name}.db", *statements)
            return schema_of(tampered)["pr_files"]

        one, again, two = read("'a b'", "one"), read("'a b'", "again"), read("'a  b'", "two")
        assert one == again
        # The difference is read where the kind's SQL is: a table's items, an index, a trigger.
        assert [key for key in one if one[key] != two[key]] == [
            {"check": "definition", "index": "indexes", "trigger": "triggers"}[kind]
        ]

    @pytest.mark.parametrize("option", ["STRICT", "WITHOUT ROWID"])
    def test_a_table_option_is_reported(
        self, head_template: Path, models_database: Path, tmp_path: Path, option: str
    ) -> None:
        tampered = _tampered(
            head_template,
            tmp_path / "tampered.db",
            *_redefined(head_template, "pr_labels", "\n)", f"\n) {option}"),
        )
        assert (
            schema_of(tampered)["pr_labels"]["flags"]
            != schema_of(models_database)["pr_labels"]["flags"]
        )


class TestViews:
    def test_views_match_the_newest_frozen_sql(self, head_template: Path) -> None:
        frozen = frozen_views(alembic_config(head_template))
        assert set(frozen) == {
            "v_github_users",
            "v_account_profiles",
            "v_pr_reviews",
            "v_pr_comments",
            "v_commit_authors",
            "v_pr_relationships",
        }
        assert view_differences(head_template, frozen) == []
        # Each view compared, by plain equality of its SQL.
        assert views(head_template) == {name: sql.strip() for name, sql in frozen.items()}

    def test_an_earlier_revision_s_views_are_reported(self, revisions: dict[str, Path]) -> None:
        """Against Step 3b-1's frozen views, Step 3a lacks the views Step 3b-1 adds, has the two
        it drops, and has the two it replaces with other SQL."""
        at_step3b1 = frozen_views(alembic_config(revisions[STEP3B1]), through=STEP3B1)
        step3a = revision_module(revisions[STEP3B1], STEP3A)
        step3b1 = revision_module(revisions[STEP3B1], STEP3B1)
        assert view_differences(revisions[STEP3A], at_step3b1) == [
            *(f"missing {name}" for name in sorted(set(at_step3b1) - dict(step3a.VIEWS).keys())),
            *(f"not frozen {name}" for name in sorted(step3b1.DROPPED_VIEWS)),
            *(f"differs {name}" for name in sorted(step3b1.REPLACED_VIEWS)),
        ]
        assert view_differences(revisions[STEP3B1], at_step3b1) == []

    def test_a_changed_view_is_reported(self, head_template: Path, tmp_path: Path) -> None:
        tampered = _tampered(
            head_template,
            tmp_path / "tampered.db",
            "DROP VIEW v_github_users",
            "CREATE VIEW v_github_users AS SELECT github_id, login FROM github_users",
            "CREATE VIEW v_extra AS SELECT 1 AS one",
            "DROP VIEW v_commit_authors",
        )
        assert view_differences(tampered, frozen_views(alembic_config(head_template))) == [
            "missing v_commit_authors",
            "not frozen v_extra",
            "differs v_github_users",
        ]

    def test_whitespace_inside_a_literal_is_a_difference(
        self, head_template: Path, tmp_path: Path
    ) -> None:
        tampered = _tampered(
            head_template,
            tmp_path / "tampered.db",
            "CREATE VIEW v_literal AS SELECT 'a  b' AS value",
            "CREATE VIEW v_kept AS SELECT 'a b' AS value",
        )
        frozen = {
            **frozen_views(alembic_config(head_template)),
            "v_literal": "\nCREATE VIEW v_literal AS SELECT 'a b' AS value\n",
            "v_kept": "\nCREATE VIEW v_kept AS SELECT 'a b' AS value\n",
        }
        assert view_differences(tampered, frozen) == ["differs v_literal"]


class TestObjectsOutsideTheComparison:
    """schema_of reads tables and the indexes and triggers on them, view_differences the views;
    an object attached to neither, such as a trigger on a view, would pass both."""

    def test_head_has_none(self, head_template: Path) -> None:
        frozen = frozen_views(alembic_config(head_template))
        assert objects_outside_the_comparison(head_template, frozen) == []
        # Every row of sqlite_master, counted from the models, the frozen views, and alembic's
        # own: each table, its created indexes, the index behind each UNIQUE, and the one behind
        # a primary key that is not the rowid.
        tables = Base.metadata.tables.values()
        indexes = sum(
            len(table.indexes)
            + sum(isinstance(constraint, UniqueConstraint) for constraint in table.constraints)
            + (not is_rowid(table))
            for table in tables
        )
        assert query(head_template, "SELECT COUNT(*) FROM sqlite_master") == [
            (len(tables) + indexes + len(frozen) + len(ALEMBIC_OBJECTS),)
        ]

    def test_the_views_a_later_revision_drops_are_reported(
        self, revisions: dict[str, Path]
    ) -> None:
        frozen = frozen_views(alembic_config(revisions[STEP3B1]), through=STEP3B1)
        dropped = revision_module(revisions[STEP3B1], STEP3B1).DROPPED_VIEWS
        assert objects_outside_the_comparison(revisions[STEP3A], frozen) == [
            ("view", name) for name in sorted(dropped)
        ]

    def test_each_planted_object_is_reported(self, head_template: Path, tmp_path: Path) -> None:
        tampered = _tampered(
            head_template,
            tmp_path / "tampered.db",
            "CREATE TRIGGER tr_view INSTEAD OF DELETE ON v_github_users BEGIN SELECT 1; END",
            "CREATE INDEX ix_version ON alembic_version (version_num)",
            "CREATE TRIGGER tr_version AFTER UPDATE ON alembic_version BEGIN SELECT 1; END",
            "ANALYZE",
        )
        frozen = frozen_views(alembic_config(head_template))
        assert objects_outside_the_comparison(tampered, frozen) == [
            ("index", "ix_version"),
            ("table", "sqlite_stat1"),
            ("trigger", "tr_version"),
            ("trigger", "tr_view"),
        ]


# Plan 5.1, transcribed: the comparison above holds the models and the migration to each other,
# and these hold both to the plan.
NULLABLE = frozenset(
    {
        ("repositories", "is_security_policy_enabled"),
        ("repositories", "description"),
        ("repositories", "homepage_url"),
        ("repositories", "pushed_at"),
        ("repositories", "archived_at"),
        ("repositories", "disk_usage"),
        ("repositories", "primary_language"),
        ("repositories", "license_spdx_id"),
        ("repo_labels", "description"),
        ("repo_labels", "created_at"),
        ("repo_labels", "updated_at"),
        # Null only for an EnterpriseUserAccount, which has no databaseId (5.2).
        ("github_users", "github_id"),
        # Null until the account's first lookup.
        ("github_users", "name"),
        ("github_users", "created_at"),
        ("github_users", "updated_at"),
        ("github_users", "linked_user_github_id"),
        ("github_users", "claimant_github_id"),
        ("github_users", "not_found_at"),
        # Every profile field, as GitHub's are, and the organization fields for a user.
        ("account_profiles", "company"),
        ("account_profiles", "location"),
        ("account_profiles", "website_url"),
        ("account_profiles", "description"),
        ("account_profiles", "email"),
        ("account_profiles", "twitter_username"),
        ("account_profiles", "is_verified"),
        ("account_profiles", "archived_at"),
        ("account_profiles", "has_sponsors_listing"),
        ("account_profiles", "pinned_items_remaining"),
        ("pull_requests", "author_id"),
        ("pull_requests", "merged_at"),
        ("pull_requests", "closed_at"),
        ("pull_requests", "published_at"),
        ("pull_requests", "last_edited_at"),
        ("pull_requests", "editor_id"),
        ("pull_requests", "merged_by_id"),
        ("pull_requests", "head_repo_github_id"),
        ("pull_requests", "head_repo_full_name"),
        ("pull_requests", "head_repo_owner_node_id"),
        ("pull_requests", "head_repo_owner_login"),
        ("pull_requests", "merge_commit_oid"),
        ("pull_requests", "milestone_number"),
        ("pull_requests", "milestone_title"),
        ("pull_requests", "review_decision"),
        ("pull_requests", "potential_merge_commit_oid"),
        ("pull_requests", "stack_node_id"),
        ("pull_requests", "stack_number"),
        ("pull_requests", "stack_size"),
        ("pull_requests", "stack_base_ref_name"),
        ("pull_requests", "stack_entry_node_id"),
        ("pull_requests", "stack_position"),
        ("pull_requests", "total_comments_count"),
        ("pr_reviews", "reviewer_id"),
        ("pr_reviews", "submitted_at"),
        ("pr_reviews", "published_at"),
        ("pr_reviews", "last_edited_at"),
        ("pr_reviews", "editor_id"),
        ("pr_reviews", "commit_oid"),
        ("pr_comments", "author_id"),
        ("pr_comments", "published_at"),
        ("pr_comments", "last_edited_at"),
        ("pr_comments", "editor_id"),
        ("pr_commits", "author_id"),
        ("pr_commits", "linked_github_id"),
        # Optional in the source record (CommitRecord).
        ("pr_commits", "author_name"),
        ("pr_commits", "author_email"),
        ("pr_review_requests", "request_github_id"),
        ("pr_review_requests", "requested_type"),
        ("pr_review_requests", "requested_github_id"),
        ("pr_review_requests", "requested_node_id"),
        ("pr_review_requests", "account_id"),
        ("pr_review_requests", "team_slug"),
        ("sync_runs", "deadline_at"),
        ("sync_runs", "finished_at"),
        ("sync_runs", "exit_code"),
        ("sync_runs", "deletions_approved"),
        ("repo_sync_state", "last_index_started_at"),
        ("repo_sync_state", "index_pr_total"),
        ("repo_sync_state", "index_issue_total"),
        ("repo_sync_state", "last_success_at"),
        ("repo_sync_state", "halt_reason"),
        ("user_tags", "description"),
        ("user_tags", "color"),
        ("pr_user_tags", "created_at"),
    }
)
UNIQUE: dict[str, set[tuple[str, tuple[str, ...]]]] = {
    "repositories": {("u", ("github_id",)), ("u", ("node_id",))},
    "repo_labels": {("pk", ("repository_id", "node_id"))},
    "github_users": {("u", ("github_id",)), ("u", ("node_id",))},
    "account_profiles": set(),  # its primary key, account_id, is the rowid
    "pull_requests": {("u", ("repository_id", "number")), ("u", ("github_id",))},
    "pr_labels": {("pk", ("pr_id", "name"))},
    "pr_reviews": {("u", ("github_id",))},
    "pr_comments": {("u", ("github_id",))},
    "pr_commits": {("pk", ("pr_id", "sha"))},
    "pr_files": {("pk", ("pr_id", "ordinal"))},
    "pr_closing_issues": {("pk", ("pr_id", "issue_repo_github_id", "issue_number"))},
    "pr_review_requests": {("pk", ("pr_id", "ordinal"))},
    "pr_assignees": {("pk", ("pr_id", "account_id"))},
    "automation_accounts": {("u", ("github_id",))},
    "automation_emails": {("u", ("email",))},
    "sync_runs": set(),
    "repo_sync_state": set(),  # its primary key, repository_id, is the rowid
    "sync_pending": {("pk", ("repository_id", "item_kind", "number"))},
    "sync_failures": {("u", ("repository_id", "item_kind", "number"))},
    # Kept from the first schema unchanged.
    "user_tags": {("u", ("name",))},
    "pr_user_tags": {("pk", ("pr_id", "user_tag_id"))},
}
# References to repositories, pull_requests, and issues cascade; references to github_users are
# set null, except those in a primary key, which cascade; references to sync_runs take SQLite's
# default, no action (5.1, D3, D28).
DELETE_ACTIONS = {
    "repositories": "CASCADE",
    "pull_requests": "CASCADE",
    "issues": "CASCADE",
    "github_users": "SET NULL",
    "sync_runs": "NO ACTION",
}
GITHUB_USERS_IN_A_PRIMARY_KEY = frozenset(
    {("pr_assignees", "account_id"), ("account_profiles", "account_id")}
)
# References to other tables, which the rule does not cover.
OUTSIDE_THE_DELETE_RULE = frozenset({("pr_user_tags", "user_tag_id")})


def delete_rule_problems(
    database: Path,
) -> tuple[set[tuple[str, str, str]], frozenset[tuple[str, str]]]:
    """References whose action breaks the rule, and the references the rule does not cover."""
    actions = delete_actions(database)

    def expected(table: str, column: str, referenced: str) -> str:
        if (table, column) in GITHUB_USERS_IN_A_PRIMARY_KEY:
            return "CASCADE"
        return DELETE_ACTIONS[referenced]

    wrong = {
        (table, column, action)
        for table, column, referenced, action in actions
        if referenced in DELETE_ACTIONS and action != expected(table, column, referenced)
    }
    return wrong, frozenset(
        (table, column)
        for table, column, referenced, _ in actions
        if referenced not in DELETE_ACTIONS
    )


class TestPlanRules:
    def test_the_nullable_columns_are_the_plans(self, head_template: Path) -> None:
        assert nullable_columns(head_template) == NULLABLE

    def test_the_unique_column_sets_are_the_plans(self, head_template: Path) -> None:
        assert unique_sets(head_template) == UNIQUE

    def test_references_cascade_or_set_null_as_the_plan_says(self, head_template: Path) -> None:
        assert delete_rule_problems(head_template) == (set(), OUTSIDE_THE_DELETE_RULE)
        # Every foreign-key column the models declare was read.
        assert len(delete_actions(head_template)) == sum(
            len(table.foreign_keys) for table in Base.metadata.tables.values()
        )

    def test_an_earlier_revision_reads_its_own_columns_and_keys(
        self, revisions: dict[str, Path]
    ) -> None:
        """94a52e4f1d57 dropped two nullable columns, added a UNIQUE to repositories, and added
        four tables; its own references keep the rule, as Step 3a's do."""
        before, after = revisions[STEP3A], revisions[STEP3B1]
        assert nullable_columns(before) - nullable_columns(after) == {
            ("github_users", "deleted_at"),
            ("sync_runs", "halt_reason"),
        }
        old, new = unique_sets(before), unique_sets(after)
        assert {table for table in new if old.get(table) != new[table]} == (
            set(schema_tables(after)) - set(schema_tables(before))
        ) | {"repositories"}
        assert new["repositories"] - old["repositories"] == {("u", ("node_id",))}
        for database in (before, after):
            assert delete_rule_problems(database) == (set(), OUTSIDE_THE_DELETE_RULE)

    def test_the_cascade_exceptions_are_the_primary_key_references(
        self, head_template: Path
    ) -> None:
        in_a_primary_key = {
            (table, str(row[3]))
            for table in schema_tables(head_template)
            for row in query(head_template, f"PRAGMA foreign_key_list({table})")
            if row[2] == "github_users"
            and any(
                column[1] == row[3] and column[5]
                for column in query(head_template, f"PRAGMA table_info({table})")
            )
        }
        assert in_a_primary_key == GITHUB_USERS_IN_A_PRIMARY_KEY
        # Each reference to github_users the models declare was read, per column.
        assert len(
            {action for action in delete_actions(head_template) if action[2] == "github_users"}
        ) == sum(
            key.column.table.name == "github_users"
            for table in Base.metadata.tables.values()
            for key in table.foreign_keys
        )

    def test_every_composite_primary_key_column_is_declared_not_null(
        self, head_template: Path
    ) -> None:
        """SQLite holds a composite primary key's columns NOT NULL only as declared."""
        composite = {
            (table.name, column.name)
            for table in Base.metadata.tables.values()
            if len(table.primary_key.columns) > 1
            for column in table.primary_key.columns
        }
        assert composite
        assert composite.isdisjoint(nullable_columns(head_template))

    def test_the_plan_readers_read_each_column_and_key(self, tmp_path: Path) -> None:
        """A composite primary key's column without NOT NULL is nullable, and each foreign key
        of a column that references two tables is read."""
        database = scratch(
            tmp_path / "scratch.db",
            "CREATE TABLE p (id INTEGER NOT NULL PRIMARY KEY)",
            "CREATE TABLE q (id INTEGER NOT NULL PRIMARY KEY)",
            "CREATE TABLE c (a INTEGER, b TEXT NOT NULL, PRIMARY KEY (a, b), "
            "FOREIGN KEY (a) REFERENCES p (id) ON DELETE CASCADE, "
            "FOREIGN KEY (a) REFERENCES q (id) ON DELETE SET NULL)",
        )
        assert nullable_columns(database) == {("c", "a")}
        assert unique_sets(database) == {"c": {("pk", ("a", "b"))}, "p": set(), "q": set()}
        assert delete_actions(database) == {
            ("c", "a", "p", "CASCADE"),
            ("c", "a", "q", "SET NULL"),
        }

    def test_a_changed_rule_is_reported(self, head_template: Path, tmp_path: Path) -> None:
        tampered = _tampered(
            head_template,
            tmp_path / "tampered.db",
            *_redefined(head_template, "pr_files", "path TEXT NOT NULL", "path TEXT"),
            *_redefined(
                head_template,
                "pr_reviews",
                "REFERENCES github_users (id) ON DELETE SET NULL, \n\t"
                "CONSTRAINT uq_pr_reviews_github_id UNIQUE (github_id)",
                "REFERENCES github_users (id) ON DELETE CASCADE",
            ),
            *_redefined(
                head_template,
                "sync_failures",
                "REFERENCES sync_runs (id), \n\tCONSTRAINT uq_sync_failures",
                "REFERENCES sync_runs (id) ON DELETE CASCADE, \n\tCONSTRAINT uq_sync_failures",
            ),
        )
        assert nullable_columns(tampered) - NULLABLE == {("pr_files", "path")}
        assert unique_sets(tampered)["pr_reviews"] == set()
        assert delete_rule_problems(tampered) == (
            {("pr_reviews", "editor_id", "CASCADE"), ("sync_failures", "last_run_id", "CASCADE")},
            OUTSIDE_THE_DELETE_RULE,
        )

    def test_a_primary_key_reference_set_null_is_reported(
        self, head_template: Path, tmp_path: Path
    ) -> None:
        """The cascade exception is read per column: SET NULL on pr_assignees.account_id breaks
        it."""
        tampered = _tampered(
            head_template,
            tmp_path / "tampered.db",
            *_redefined(
                head_template,
                "pr_assignees",
                "REFERENCES github_users (id) ON DELETE CASCADE",
                "REFERENCES github_users (id) ON DELETE SET NULL",
            ),
        )
        assert delete_rule_problems(tampered) == (
            {("pr_assignees", "account_id", "SET NULL")},
            OUTSIDE_THE_DELETE_RULE,
        )

    def test_a_primary_key_column_and_a_second_reference_are_read(
        self, head_template: Path, tmp_path: Path
    ) -> None:
        """SQLite holds a composite primary key's columns NOT NULL only as declared, and one
        column can carry two foreign keys."""
        tampered = _tampered(
            head_template,
            tmp_path / "tampered.db",
            *_redefined(head_template, "pr_labels", "name TEXT NOT NULL", "name TEXT"),
            *_redefined(
                head_template,
                "pr_reviews",
                "ON DELETE SET NULL, \n\t",
                "ON DELETE SET NULL, \n\t"
                "FOREIGN KEY(reviewer_id) REFERENCES github_users (id) ON DELETE CASCADE, \n\t",
            ),
        )
        assert nullable_columns(tampered) - NULLABLE == {("pr_labels", "name")}
        assert delete_rule_problems(tampered) == (
            {("pr_reviews", "reviewer_id", "CASCADE")},
            OUTSIDE_THE_DELETE_RULE,
        )


class TestForeignKeyIndexes:
    # Kept from the first schema unchanged (plan 5.1).
    EXEMPT = frozenset({("pr_user_tags", ("user_tag_id",))})

    def test_every_foreign_key_column_leads_an_index(self, head_template: Path) -> None:
        # Every foreign key the models declare is examined.
        assert len(foreign_key_columns(head_template)) == sum(
            len(table.foreign_key_constraints) for table in Base.metadata.tables.values()
        )
        # Equality, not a subset: the exemption is still needed, and nothing else is missing.
        assert unindexed_foreign_keys(head_template) == self.EXEMPT

    def test_a_composite_primary_key_and_the_rowid_count(self, head_template: Path) -> None:
        """pr_labels.pr_id leads its primary key; repo_sync_state.repository_id is the rowid."""
        foreign_keys = {
            (table, row[3])
            for table in ("pr_labels", "repo_sync_state")
            for row in query(head_template, f"PRAGMA foreign_key_list({table})")
        }
        assert {("pr_labels", "pr_id"), ("repo_sync_state", "repository_id")} <= foreign_keys
        assert not {
            entry
            for entry in unindexed_foreign_keys(head_template)
            if entry[0] in ("pr_labels", "repo_sync_state")
        }

    def test_a_missing_index_is_reported(self, head_template: Path, tmp_path: Path) -> None:
        tampered = _tampered(
            head_template, tmp_path / "tampered.db", "DROP INDEX ix_pr_comments_author_id"
        )
        assert unindexed_foreign_keys(tampered) == self.EXEMPT | {("pr_comments", ("author_id",))}


def _sync_run(seed: Seed, **values: object) -> int:
    return seed.sync_run(**values)


def _repo_sync_state(seed: Seed, outcome: str) -> None:
    run = _sync_run(seed)
    seed.connection.execute(
        insert(RepoSyncState).values(
            repository_id=seed.repository(), last_run_id=run, last_outcome=outcome
        )
    )


def _sync_pending(seed: Seed, kind: str) -> None:
    run = _sync_run(seed)
    seed.connection.execute(
        insert(SyncPending).values(
            repository_id=seed.repository(), item_kind=kind, number=1, run_id=run
        )
    )


def _sync_failure(seed: Seed, kind: str) -> None:
    seed.connection.execute(
        insert(SyncFailure).values(
            repository_id=seed.repository(),
            item_kind=kind,
            number=1,
            attempts=1,
            last_error="e",
            first_failed_at=T0,
            last_failed_at=T0,
            last_run_id=seed.run_id(),
        )
    )


def _review(seed: Seed, state: str) -> None:
    pr = seed.pull_request(seed.repository(), 1)
    seed.review(pr, seed.next_id(), None, state)


def failed_check(error: pytest.ExceptionInfo[IntegrityError]) -> str:
    """The CHECK SQLite refused a row for: the driver's error code says a CHECK failed, and the
    constraint's name is in SQLite's fixed ``CHECK constraint failed: <name>`` form."""
    driver_error = error.value.orig
    assert isinstance(driver_error, sqlite3.IntegrityError), repr(driver_error)
    assert driver_error.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"
    named = re.fullmatch(r"CHECK constraint failed: (\w+)", str(driver_error))
    assert named is not None, str(driver_error)
    return named[1]


Write = Callable[[Seed, str], object]
# CHECKs tying two columns together, each with its own test below.
PAIRED_CHECKS = frozenset(
    {
        "ck_pull_requests_merged_at_iff_merged",
        "ck_github_users_github_id_null_iff_enterprise_account",
    }
)
SYNC_STATUSES = ("running", "complete", "time_limited", "failed", "aborted")
TRIGGERS = ("schedule", "workflow_dispatch", "manual")
OUTCOMES = ("complete", "time_limited", "failed", "incomplete_index", "halted", "untracked")
ITEM_KINDS = ("pull_request", "issue")

# Every CHECK at head: a write of one value, the values it must accept, and one it must reject.
CHECKS: dict[str, tuple[Write, tuple[str, ...], str]] = {
    "ck_github_users_type": (
        lambda seed, value: seed.user("u", type=value),
        get_args(AccountType),
        "Ghost",
    ),
    "ck_pull_requests_state": (
        lambda seed, value: seed.pull_request(seed.repository(), 1, state=value),
        ("OPEN", "CLOSED"),
        "QUEUED",
    ),
    "ck_pr_reviews_state": (_review, get_args(ReviewState), "PENDING"),
    "ck_automation_emails_email_lowercase": (
        lambda seed, value: seed.automation_email(value),
        ("noreply@anthropic.com",),
        "NoReply@Anthropic.com",
    ),
    "ck_automation_emails_email_ascii": (
        lambda seed, value: seed.automation_email(value),
        ("noreply@anthropic.com", "dev+bot@example.org"),
        "émile@example.org",
    ),
    "ck_sync_runs_status": (
        lambda seed, value: _sync_run(seed, status=value),
        SYNC_STATUSES,
        "done",
    ),
    "ck_sync_runs_trigger": (lambda seed, value: _sync_run(seed, trigger=value), TRIGGERS, "push"),
    "ck_repo_sync_state_last_outcome": (_repo_sync_state, OUTCOMES, "skipped"),
    "ck_sync_pending_item_kind": (_sync_pending, ITEM_KINDS, "discussion"),
    "ck_sync_failures_item_kind": (_sync_failure, ITEM_KINDS, "discussion"),
}


class TestCheckConstraints:
    def test_every_check_at_head_has_a_case(self, head_template: Path) -> None:
        at_head = {
            name
            for table in schema_tables(head_template)
            for name, item in named_constraints(head_template, table).items()
            if name.startswith("ck_")
        }
        assert at_head == set(CHECKS) | PAIRED_CHECKS

    @pytest.mark.parametrize("name", sorted(CHECKS))
    def test_accepts_its_values_and_rejects_another(
        self, migrated_database: Path, name: str
    ) -> None:
        write, accepted, rejected = CHECKS[name]
        for value in accepted:
            with connect(migrated_database) as connection:
                write(Seed(connection), value)
        with pytest.raises(IntegrityError) as error:
            with connect(migrated_database) as connection:
                write(Seed(connection), rejected)
        assert failed_check(error) == name

    @pytest.mark.parametrize(
        ("state", "merged_at", "accepted"),
        [
            ("MERGED", T0, True),
            ("OPEN", None, True),
            ("MERGED", None, False),
            ("OPEN", T0, False),
        ],
    )
    def test_merged_at_is_set_iff_merged(
        self, migrated_database: Path, state: str, merged_at: object, accepted: bool
    ) -> None:
        def write() -> None:
            with connect(migrated_database) as connection:
                seed = Seed(connection)
                seed.pull_request(seed.repository(), 1, state=state, merged_at=merged_at)

        if accepted:
            write()
        else:
            with pytest.raises(IntegrityError) as error:
                write()
            assert failed_check(error) == "ck_pull_requests_merged_at_iff_merged"


class TestEnterpriseAccountId:
    @pytest.mark.parametrize(
        ("type_", "github_id", "accepted"),
        [
            ("EnterpriseUserAccount", None, True),
            ("User", 4242, True),
            ("EnterpriseUserAccount", 4242, False),
            ("User", None, False),
        ],
        ids=["enterprise-without-id", "user-with-id", "enterprise-with-id", "user-without-id"],
    )
    def test_github_id_is_null_exactly_for_an_enterprise_account(
        self, migrated_database: Path, type_: str, github_id: int | None, accepted: bool
    ) -> None:
        def write() -> None:
            with connect(migrated_database) as connection:
                Seed(connection).user("u", type=type_, github_id=github_id, node_id="EUA_x")

        if accepted:
            write()
        else:
            with pytest.raises(IntegrityError) as error:
                write()
            assert failed_check(error) == "ck_github_users_github_id_null_iff_enterprise_account"


ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("src", "tests", "scripts", "alembic")


def create_all_uses(*roots: Path) -> tuple[list[str], int]:
    """Every ``.create_all`` attribute, called or passed as ``run_sync(metadata.create_all)``,
    and the number of files read."""
    found, files = [], 0
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            files += 1
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, ast.Attribute) and node.attr == "create_all":
                    found.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    return found, files


def create_all_tokens(*roots: Path) -> tuple[int, set[str]]:
    """As the tokenizer reads the same files: the ``create_all`` names, and the lines of the
    strings and comments that hold the text."""
    text_tokens = {tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE}
    names, texts = 0, set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            with path.open("rb") as source:
                for token in tokenize.tokenize(source.readline):
                    if token.type == tokenize.NAME and token.string == "create_all":
                        names += 1
                    elif token.type in text_tokens and "create_all" in token.string:
                        texts.add(f"{path.relative_to(root.parent)}:{token.start[0]}")
    return names, texts


class TestBuiltByMigrations:
    """Databases are built by ``alembic upgrade head``; ``create_all`` only builds the models'
    side of the comparison above (plan Step 3)."""

    def test_create_all_is_used_only_by_the_comparison(self) -> None:
        roots = [ROOT / name for name in ROOTS]
        uses, files = create_all_uses(*roots)
        assert files == sum(
            name.endswith(".py")
            for root in roots
            for _directory, _subdirectories, names in os.walk(root)
            for name in names
        )
        assert [use.split(":")[0] for use in uses] == ["tests/db/test_schema.py"], uses
        # Each use is a name the tokenizer reads too; the text in strings and comments is none.
        names, texts = create_all_tokens(*roots)
        assert len(uses) == names
        assert texts
        assert texts.isdisjoint(uses)

    @pytest.mark.parametrize(
        ("source", "flagged"),
        [
            ("Base.metadata.create_all(engine)\n", True),
            ("conn.run_sync(Base.metadata.create_all)\n", True),
            ("def f():\n    metadata.create_all(bind=engine)\n", True),
            ('DECOY = "Base.metadata.create_all(engine)"\n', False),
            ("# Base.metadata.create_all(engine)\n", False),
            ("Base.metadata.drop_all(engine)\n", False),
        ],
    )
    def test_flags_every_use_and_no_text(self, tmp_path: Path, source: str, flagged: bool) -> None:
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "module.py").write_text(source)
        assert bool(create_all_uses(package)[0]) is flagged
