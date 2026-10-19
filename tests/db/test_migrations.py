"""The migration chain and alembic/env.py (redesign plan 5.1, Step 3a).

Every upgrade here runs the repository's own env.py and revisions; test-only revisions are
added after the head through ``version_locations``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import itertools
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from github_activity_db.config import get_settings
from github_activity_db.db.models import Base
from github_activity_db.db.schema import HEAD_REVISION, MIGRATION_REVISIONS, SCHEMA_VERSION
from tests.db.alembic_support import (
    FIRST_SCHEMA_HEAD,
    REPO_ROOT,
    alembic_config,
    query,
    revision_of,
    table_names,
    upgrade,
    write_revision,
)
from tests.db.schema_support import frozen_views, named_constraints, schema_of
from tests.db.seed import T0, Seed, connect
from tests.test_import_boundaries import imported_modules

TRACKED = REPO_ROOT / "github_activity.db"
TEST_REVISION = "f1e57000a001"
# A pr_labels row whose pull request does not exist.
PLANTED_LABEL = "INSERT INTO pr_labels (pr_id, name, label_node_id) VALUES (999, {!r}, 'x')"
ORPHAN = f"op.execute({PLANTED_LABEL.format('orphan')!r})"
VIEWS_READING_PULL_REQUESTS = (
    "v_pr_reviews",
    "v_pr_comments",
    "v_commit_authors",
    "v_pr_relationships",
)
PULL_REQUEST_INDEXES = (
    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'pull_requests' "
    "ORDER BY name"
)


def snapshot(database: Path) -> tuple[Any, ...]:
    """Everything an upgrade could change: the schema, every table's rows, and user_version."""
    with closing(sqlite3.connect(database)) as connection:
        schema = sorted(
            (tuple(row) for row in connection.execute("SELECT * FROM sqlite_master")), key=repr
        )
        rows = {
            name: sorted(map(tuple, connection.execute(f'SELECT * FROM "{name}"')), key=repr)
            for kind, name, *_rest in schema
            if kind == "table"
        }
        (user_version,) = connection.execute("PRAGMA user_version").fetchone()
    return schema, rows, user_version


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refusal(error: pytest.ExceptionInfo[BaseException]) -> str:
    """env.py is loaded fresh by every command, so its error class is matched by name."""
    assert type(error.value).__name__ == "MigrationRefused", repr(error.value)
    return str(error.value)


@pytest.fixture(scope="module")
def first_schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database at the first schema's head holding a repository, a PR, and a sync failure."""
    path = tmp_path_factory.mktemp("first-schema") / "first.db"
    upgrade(path, FIRST_SCHEMA_HEAD)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO repositories (id, owner, name, full_name, is_active, created_at) "
            "VALUES (1, 'prebid', 'Prebid.js', 'prebid/Prebid.js', 1, '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO pull_requests (id, repository_id, number, link, open_date, submitter, "
            "title, last_update_date, state, files_changed, lines_added, lines_deleted, "
            "commits_count, github_labels, file_changes, commits_breakdown, reviewers, "
            "assignees, participants, created_at, updated_at) VALUES (1, 1, 13481, "
            "'https://github.com/prebid/Prebid.js/pull/13481', '2026-01-01 00:00:00', "
            "'dgirardi', 'link CLAUDE.md', '2026-01-02 00:00:00', 'MERGED', 3, 1, 1, 4, '[]', "
            "'[]', '[]', '[]', '[]', '{}', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO sync_failures (repository_id, pr_number, error_message, error_type, "
            "retry_count, status, failed_at, created_at) VALUES (1, 7, 'e', 'ValueError', 0, "
            "'PENDING', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    path.chmod(0o444)
    return path


@pytest.fixture
def first_schema(first_schema_template: Path, tmp_path: Path) -> Path:
    path = tmp_path / "first.db"
    shutil.copyfile(first_schema_template, path)
    return path


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestChain:
    def test_upgrade_builds_head_with_no_autogenerate_diff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_settings: None
    ) -> None:
        target = tmp_path / "migrations.db"
        # If env.py ignored the database_url attribute it would migrate DATABASE_URL instead;
        # point that at a decoy to make it visible.
        decoy = tmp_path / "decoy.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{decoy}")
        config = alembic_config(target)
        command.upgrade(config, "head")
        command.check(config)  # raises AutogenerateDiffsDetected when models and chain disagree
        assert not decoy.exists()
        assert revision_of(target) == HEAD_REVISION

    def test_the_packaged_revision_tuple_equals_the_chain(self, tmp_path: Path) -> None:
        script = ScriptDirectory.from_config(alembic_config(tmp_path / "unused.db"))
        revisions = list(reversed(list(script.walk_revisions())))
        assert tuple(revision.revision for revision in revisions) == MIGRATION_REVISIONS
        assert script.get_heads() == [HEAD_REVISION]
        for previous, revision in itertools.pairwise(revisions):
            assert revision.down_revision == previous.revision

    def test_user_version_holds_schema_version(self, head_template: Path) -> None:
        assert query(head_template, "PRAGMA user_version") == [(SCHEMA_VERSION,)]

    def test_downgrade_raises_and_changes_nothing(self, migrated_database: Path) -> None:
        before = snapshot(migrated_database)
        with pytest.raises(RuntimeError, match="migrations are never downgraded"):
            command.downgrade(alembic_config(migrated_database), FIRST_SCHEMA_HEAD)
        assert snapshot(migrated_database) == before

    @pytest.mark.parametrize(
        "run",
        [
            pytest.param(command.current, id="current"),
            pytest.param(command.check, id="check"),
            pytest.param(lambda config: command.stamp(config, "head"), id="stamp head"),
            pytest.param(lambda config: command.upgrade(config, "head"), id="upgrade at head"),
        ],
    )
    def test_every_command_refuses_a_file_holding_a_foreign_key_violation(
        self, migrated_database: Path, run: Any
    ) -> None:
        """No command commits a file that fails the check, whether it ran a revision or not."""
        with closing(sqlite3.connect(migrated_database)) as connection, connection:
            connection.execute(PLANTED_LABEL.format("orphan"))
        before = snapshot(migrated_database)
        with pytest.raises(RuntimeError) as error:
            run(alembic_config(migrated_database))
        assert "1 foreign-key violations in the file before the commit" in refusal(error)
        assert snapshot(migrated_database) == before

    @pytest.mark.parametrize(
        ("run", "statement"),
        [
            *(
                pytest.param(run, statement, id=f"{change}, {name}")
                for change, statement in (
                    ("a row", ORPHAN),
                    # The seeded pull request's author row goes with the table.
                    ("the schema", 'op.execute("DROP TABLE github_users")'),
                )
                for name, run in (
                    ("current", command.current),
                    ("stamp head", lambda config: command.stamp(config, "head")),
                    ("upgrade at head", lambda config: command.upgrade(config, "head")),
                )
            ),
            # With the table gone, check's own comparison fails before the commit instead.
            pytest.param(command.check, ORPHAN, id="a row, check"),
        ],
    )
    def test_revision_module_code_that_breaks_a_reference_is_refused(
        self, migrated_database: Path, tmp_path: Path, run: Any, statement: str
    ) -> None:
        """alembic loads each revision module inside the transaction, so code at a module's top
        level runs even when no upgrade() does."""
        with connect(migrated_database) as seeded:
            seed = Seed(seeded)
            seed.pull_request(seed.repository(), 1, seed.user("author"))
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / f"{TEST_REVISION}_module_level.py").write_text(
            "from alembic import op\n\n"
            f"revision = {TEST_REVISION!r}\n"
            f"down_revision = {HEAD_REVISION!r}\n"
            "branch_labels = None\n"
            "depends_on = None\n\n"
            f"{statement}\n\n\n"
            "def upgrade() -> None:\n"
            "    pass\n\n\n"
            "def downgrade() -> None:\n"
            "    raise RuntimeError('test-only revision')\n"
        )
        # At the test revision already, so no command runs a revision or finds the file behind.
        with closing(sqlite3.connect(migrated_database)) as connection, connection:
            connection.execute("UPDATE alembic_version SET version_num = ?", (TEST_REVISION,))
        before = snapshot(migrated_database)
        with pytest.raises(RuntimeError) as error:
            run(alembic_config(migrated_database, test_versions=versions))
        assert "1 foreign-key violations" in refusal(error)
        assert snapshot(migrated_database) == before

    def test_an_in_memory_database_is_migrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        config = alembic_config(tmp_path / "unused.db")
        config.attributes["database_url"] = "sqlite+aiosqlite://"
        command.upgrade(config, "head")
        assert list(tmp_path.iterdir()) == []


class TestStep3Migration:
    def test_upgrades_a_seeded_first_schema_database(self, first_schema: Path) -> None:
        user_tags = query(first_schema, "SELECT sql FROM sqlite_master WHERE name = 'user_tags'")
        upgrade(first_schema)
        assert revision_of(first_schema) == HEAD_REVISION
        for table in ("repositories", "pull_requests", "sync_failures", "pr_user_tags"):
            assert query(first_schema, f"SELECT COUNT(*) FROM {table}") == [(0,)], table
        assert "link" not in {
            row[1] for row in query(first_schema, "PRAGMA table_info(pull_requests)")
        }
        # user_tags is kept as it was; the rest matches a fresh build.
        assert (
            query(first_schema, "SELECT sql FROM sqlite_master WHERE name = 'user_tags'")
            == user_tags
        )
        assert query(first_schema, "PRAGMA foreign_key_check") == []

    def test_matches_a_fresh_build(self, first_schema: Path, head_template: Path) -> None:
        upgrade(first_schema)
        assert schema_of(first_schema) == schema_of(head_template)
        assert query(
            first_schema, "SELECT name, sql FROM sqlite_master WHERE type = 'view' ORDER BY name"
        ) == query(
            head_template, "SELECT name, sql FROM sqlite_master WHERE type = 'view' ORDER BY name"
        )

    def test_upgrades_a_scratch_copy_of_the_tracked_file(self, tmp_path: Path) -> None:
        tracked_digest = digest(TRACKED)
        copy = tmp_path / "tracked-copy.db"
        shutil.copyfile(TRACKED, copy)
        assert revision_of(copy) == FIRST_SCHEMA_HEAD
        upgrade(copy)
        assert revision_of(copy) == HEAD_REVISION
        assert query(copy, "PRAGMA integrity_check") == [("ok",)]
        assert query(copy, "PRAGMA foreign_key_check") == []
        assert query(copy, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
        assert digest(TRACKED) == tracked_digest

    @pytest.mark.parametrize(
        ("insert", "held"),
        [
            pytest.param(
                "INSERT INTO user_tags (name, created_at) VALUES ('keep', '2026-01-01 00:00:00')",
                "user_tags 1, pr_user_tags 0",
                id="user_tags",
            ),
            pytest.param(
                # Foreign keys are off on this connection: a link whose tag row is missing.
                "INSERT INTO pr_user_tags (pr_id, user_tag_id) VALUES (1, 99)",
                "user_tags 0, pr_user_tags 1",
                id="pr_user_tags",
            ),
        ],
    )
    def test_aborts_while_a_tag_table_holds_rows(
        self, first_schema: Path, insert: str, held: str
    ) -> None:
        with closing(sqlite3.connect(first_schema)) as connection, connection:
            connection.execute(insert)
        before = snapshot(first_schema)
        with pytest.raises(RuntimeError, match=rf"tag rows would be lost \({held}\)"):
            upgrade(first_schema)
        assert snapshot(first_schema) == before


STEP3A = "33f8570f6540"
STEP3A_ROWS = {
    "repositories": (
        "INSERT INTO repositories (id, github_id, owner, name, full_name, repo_group, is_private, "
        "is_archived, default_branch, is_tracked) "
        "VALUES (1, 39321023, 'prebid', 'Prebid.js', 'prebid/Prebid.js', 'Client', 0, 0, "
        "'master', 1)"
    ),
    "github_users": (
        "INSERT INTO github_users (github_id, node_id, login, type, refreshed_at) "
        "VALUES (1683175, 'MDQ6VXNlcjE2ODMxNzU=', 'patmmccann', 'User', "
        "'2026-01-01 00:00:00.000000')"
    ),
    "sync_runs": (
        'INSERT INTO sync_runs (started_at, status, "trigger", code_version, graphql_points, '
        "rest_calls, items_planned, items_hydrated, items_deleted, failures) "
        "VALUES ('2026-01-01 00:00:00.000000', 'running', 'manual', 'test', 0, 0, 0, 0, 0, 0)"
    ),
    "pull_requests": (
        "INSERT INTO pull_requests (repository_id, number, github_id, node_id, state, is_draft, "
        "title, body, author_association, created_at, base_ref, targets_default_branch, "
        "is_cross_repository, additions, deletions, changed_files, commits_total, files_total, "
        "reviews_total, comments_total, labels_total, closing_issues_total, github_updated_at, "
        "hydrated_at, hydration_version) VALUES (1, 1, 1, 'PR_1', 'OPEN', 0, 't', '', 'MEMBER', "
        "'2026-01-01 00:00:00.000000', 'master', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "
        "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000', 1)"
    ),
}


@pytest.fixture
def step3a_database(tmp_path: Path) -> Path:
    """A database at the Step 3a revision, which no sync has written."""
    database = tmp_path / "step3a.db"
    upgrade(database, STEP3A)
    assert revision_of(database) == STEP3A
    return database


STEP3B1 = "94a52e4f1d57"
LAST_RUN_ID = "2d2ba76b177c"


@pytest.fixture
def step3b1_database(tmp_path: Path) -> Path:
    """A database at the Step 3b-1 revision, which no sync has written."""
    database = tmp_path / "step3b1.db"
    upgrade(database, STEP3B1)
    assert revision_of(database) == STEP3B1
    return database


class TestStep3b1Migration:
    @pytest.mark.parametrize(
        ("tables", "held"),
        [
            (("repositories", "pull_requests"), "repositories 1, pull_requests 1"),
            (("repositories",), "repositories 1"),
            (("github_users",), "github_users 1"),
            (("sync_runs",), "sync_runs 1"),
        ],
        ids=["pull_requests", "repositories", "github_users", "sync_runs"],
    )
    def test_refuses_a_database_holding_rows_in_a_table_it_adds_columns_to(
        self, step3a_database: Path, tables: tuple[str, ...], held: str
    ) -> None:
        with closing(sqlite3.connect(step3a_database)) as connection, connection:
            for table in tables:
                connection.execute(STEP3A_ROWS[table])
        before = snapshot(step3a_database)
        with pytest.raises(RuntimeError, match=rf"rows would be lost \({held}\)"):
            upgrade(step3a_database)
        assert snapshot(step3a_database) == before

    def test_upgrades_a_database_without_rows(
        self, step3a_database: Path, head_template: Path
    ) -> None:
        upgrade(step3a_database)
        assert revision_of(step3a_database) == HEAD_REVISION
        assert query(step3a_database, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
        assert schema_of(step3a_database) == schema_of(head_template)

    def test_a_row_in_a_table_it_keeps_is_not_refused(self, step3a_database: Path) -> None:
        """A user tag is kept as it was; only the tables the revision recreates must be empty."""
        with closing(sqlite3.connect(step3a_database)) as connection, connection:
            connection.execute(
                "INSERT INTO user_tags (name, created_at) VALUES ('kept', '2026-01-01 00:00:00')"
            )
        upgrade(step3a_database)
        assert query(step3a_database, "SELECT name FROM user_tags") == [("kept",)]

    def test_the_refusal_covers_every_table_the_revision_recreates(
        self, step3a_database: Path, head_template: Path, tmp_path: Path
    ) -> None:
        """CHANGED_TABLES, which the refusal counts, is exactly the set of tables present at
        Step 3a whose definition differs at Step 3b-1; a table it leaves alone is unchanged."""
        module = (
            ScriptDirectory.from_config(alembic_config(head_template)).get_revision(STEP3B1).module
        )
        step3b1 = tmp_path / "at-step3b1.db"
        upgrade(step3b1, STEP3B1)
        before, after = table_definitions(step3a_database), table_definitions(step3b1)
        changed = {table for table in before if before[table] != after.get(table)}
        assert set(module.CHANGED_TABLES) == changed
        assert after.keys() - before.keys() == {
            "repo_labels",
            "pr_review_requests",
            "pr_assignees",
            "account_profiles",
        }

    def test_the_definition_reading_sees_a_changed_table(
        self, step3a_database: Path, tmp_path: Path
    ) -> None:
        """Positive control: a column added to a table is a changed definition."""
        changed = tmp_path / "changed.db"
        shutil.copyfile(step3a_database, changed)
        with closing(sqlite3.connect(changed)) as connection, connection:
            connection.execute("ALTER TABLE pr_files ADD COLUMN probe INTEGER")
        assert (
            table_definitions(changed)["pr_files"] != table_definitions(step3a_database)["pr_files"]
        )

    def test_the_definition_reading_sees_a_literal_s_whitespace(
        self, step3a_database: Path, tmp_path: Path
    ) -> None:
        """Whitespace inside a literal is data: a CHECK on 'a b' and one on 'a  b' differ."""

        def defined(literal: str, name: str) -> str:
            copy = tmp_path / f"{name}.db"
            shutil.copyfile(step3a_database, copy)
            with closing(sqlite3.connect(copy)) as connection, connection:
                ((sql,),) = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'pr_files'"
                ).fetchall()
                assert "path TEXT NOT NULL" in sql
                connection.execute("DROP TABLE pr_files")
                connection.execute(
                    sql.replace(
                        "path TEXT NOT NULL", f"path TEXT NOT NULL CHECK (path != {literal})"
                    )
                )
            return table_definitions(copy)["pr_files"]

        assert defined("'a b'", "one") == defined("'a b'", "again")
        assert defined("'a  b'", "two") != defined("'a b'", "one")


class TestSyncFailuresLastRunIdMigration:
    FAILURE = (
        "INSERT INTO sync_failures (repository_id, item_kind, number, attempts, last_error, "
        "first_failed_at, last_failed_at) VALUES (?, 'pull_request', 1, 1, 'e', "
        "'2026-01-01 00:00:00.000000', '2026-01-01 00:00:00.000000')"
    )

    def _repository(self, database: Path) -> int:
        with connect(database) as connection:
            return Seed(connection).repository()

    def test_refuses_a_database_holding_a_sync_failures_row(self, step3b1_database: Path) -> None:
        repository = self._repository(step3b1_database)
        with closing(sqlite3.connect(step3b1_database)) as connection, connection:
            connection.execute(self.FAILURE, (repository,))
        before = snapshot(step3b1_database)
        with pytest.raises(RuntimeError, match=r"rows would be lost \(sync_failures 1\)"):
            upgrade(step3b1_database)
        assert snapshot(step3b1_database) == before
        assert revision_of(step3b1_database) == STEP3B1

    def test_upgrades_a_database_without_rows(
        self, step3b1_database: Path, head_template: Path
    ) -> None:
        upgrade(step3b1_database)
        assert revision_of(step3b1_database) == HEAD_REVISION
        assert query(step3b1_database, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
        assert schema_of(step3b1_database) == schema_of(head_template)

    def test_a_row_in_a_table_it_keeps_is_not_refused(self, step3b1_database: Path) -> None:
        repository = self._repository(step3b1_database)
        upgrade(step3b1_database)
        assert query(step3b1_database, "SELECT id FROM repositories") == [(repository,)]

    def test_the_refusal_covers_every_table_the_revision_recreates(
        self, step3b1_database: Path, head_template: Path, tmp_path: Path
    ) -> None:
        """CHANGED_TABLES is exactly the set of tables whose definition the revision changes;
        it adds no table."""
        module = (
            ScriptDirectory.from_config(alembic_config(head_template))
            .get_revision(LAST_RUN_ID)
            .module
        )
        migrated = tmp_path / "at-last-run-id.db"
        upgrade(migrated, LAST_RUN_ID)
        before, after = table_definitions(step3b1_database), table_definitions(migrated)
        changed = {table for table in before if before[table] != after.get(table)}
        assert set(module.CHANGED_TABLES) == changed == {"sync_failures"}
        assert after.keys() == before.keys()


def table_definitions(database: Path) -> dict[str, str]:
    """Each table's CREATE statement as SQLite stores it, alembic's own table excluded."""
    return {
        str(name): str(sql)
        for name, sql in query(
            database,
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name <> 'alembic_version'",
        )
    }


class TestOneTransaction:
    def test_a_failure_in_the_second_revision_applies_neither(
        self, first_schema: Path, tmp_path: Path
    ) -> None:
        versions = tmp_path / "versions"
        write_revision(
            versions, TEST_REVISION, HEAD_REVISION, "raise RuntimeError('planted failure')"
        )
        before = snapshot(first_schema)
        with pytest.raises(RuntimeError, match="planted failure"):
            upgrade(first_schema, test_versions=versions)
        assert snapshot(first_schema) == before
        assert revision_of(first_schema) == FIRST_SCHEMA_HEAD

    def test_a_foreign_key_violation_fails_the_upgrade_before_commit(
        self, migrated_database: Path, tmp_path: Path
    ) -> None:
        versions = tmp_path / "versions"
        write_revision(
            versions,
            TEST_REVISION,
            HEAD_REVISION,
            ORPHAN,
        )
        before = snapshot(migrated_database)
        with pytest.raises(RuntimeError) as error:
            upgrade(migrated_database, test_versions=versions)
        assert "1 foreign-key violations in the file before the commit" in refusal(error)
        assert "('pr_labels', 1, 'pull_requests', 0)" in refusal(error)
        assert snapshot(migrated_database) == before

    def test_the_migration_connection_has_foreign_keys_off_inside_one_transaction(
        self, migrated_database: Path, tmp_path: Path
    ) -> None:
        versions = tmp_path / "versions"
        write_revision(
            versions,
            TEST_REVISION,
            HEAD_REVISION,
            """
            bind = op.get_bind()
            dbapi = bind.connection.dbapi_connection
            foreign_keys = bind.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            op.execute(
                "CREATE TABLE probe (foreign_keys INTEGER, in_transaction INTEGER, "
                "autocommit INTEGER)"
            )
            bind.exec_driver_sql(
                "INSERT INTO probe VALUES (?, ?, ?)",
                (foreign_keys, dbapi.in_transaction, dbapi.isolation_level is None),
            )
            """,
        )
        upgrade(migrated_database, test_versions=versions)
        assert query(migrated_database, "SELECT * FROM probe") == [(0, 1, 1)]

    @pytest.mark.parametrize(
        ("foreign_keys", "message"),
        [
            pytest.param("ON", "PRAGMA foreign_keys reads 1 after turning it off", id="fk on"),
            pytest.param("OFF", "already inside a transaction", id="fk off"),
        ],
    )
    def test_a_connection_already_inside_a_transaction_is_refused(
        self,
        first_schema: Path,
        monkeypatch: pytest.MonkeyPatch,
        foreign_keys: str,
        message: str,
    ) -> None:
        """Planted through env.py's own engine: the DBAPI connect function SQLAlchemy calls."""
        real_connect = sqlite3.dbapi2.connect
        planted: list[sqlite3.Connection] = []

        def connect_inside_a_transaction(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection = real_connect(*args, **kwargs)
            connection.execute(f"PRAGMA foreign_keys={foreign_keys}")
            connection.execute("BEGIN")
            planted.append(connection)
            return connection

        before = snapshot(first_schema)
        monkeypatch.setattr(sqlite3.dbapi2, "connect", connect_inside_a_transaction)
        with pytest.raises(RuntimeError) as error:
            upgrade(first_schema)
        monkeypatch.undo()
        assert message in refusal(error)
        assert len(planted) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            planted[0].execute("SELECT 1")
        assert snapshot(first_schema) == before


# SQLite rolls back by itself; the revision swallows the error and goes on.
SWALLOWED_ROLLBACK = (
    "try:\n"
    '    op.execute("INSERT OR ROLLBACK INTO alembic_version "\n'
    f"               \"VALUES ('{HEAD_REVISION}')\")\n"
    "except Exception:\n"
    "    pass\n"
)
STOP_TRIGGER = (
    "CREATE TRIGGER stop BEFORE INSERT ON pr_labels WHEN NEW.name = 'stop' "
    "BEGIN SELECT RAISE(ROLLBACK, 'stop'); END"
)
STOPPED_INSERT = (
    f"try:\n    op.execute({PLANTED_LABEL.format('stop')!r})\nexcept Exception:\n    pass\n"
)
BOUND_INSERT = (
    'op.get_bind().exec_driver_sql("INSERT INTO pr_labels (pr_id, name, label_node_id) '
    "VALUES (?, ?, 'LA_bound')\", (999, {!r}))"
)


class TestTransactionControl:
    """Only env.py opens, commits, and rolls back the upgrade's transaction (plan 5.1)."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(f"{ORPHAN}\nop.execute('COMMIT')", id="COMMIT"),
            pytest.param(f"{ORPHAN}\nop.execute('END')", id="END"),
            pytest.param(f"{ORPHAN}\nop.execute('ROLLBACK')\n{ORPHAN}", id="ROLLBACK"),
            pytest.param(f"op.execute('BEGIN')\n{ORPHAN}", id="BEGIN"),
            pytest.param(
                f"with op.get_context().autocommit_block():\n    {ORPHAN}", id="autocommit_block"
            ),
            pytest.param(
                "op.execute('CREATE TABLE early (x INTEGER)')\n"
                f"{SWALLOWED_ROLLBACK}"
                f"op.execute('CREATE TABLE late (x INTEGER)')\n{ORPHAN}",
                id="INSERT OR ROLLBACK",
            ),
            pytest.param(
                "op.execute('CREATE TRIGGER stop BEFORE INSERT ON pr_labels "
                "BEGIN SELECT RAISE(ROLLBACK, \\'stop\\'); END')\n"
                "try:\n"
                f"    {ORPHAN}\n"
                "except Exception:\n"
                "    pass\n"
                "op.execute('CREATE TABLE late (x INTEGER)')",
                id="RAISE(ROLLBACK)",
            ),
        ],
    )
    def test_a_revision_cannot_end_the_transaction(
        self, first_schema: Path, tmp_path: Path, body: str
    ) -> None:
        versions = tmp_path / "versions"
        write_revision(versions, TEST_REVISION, HEAD_REVISION, body)
        before = snapshot(first_schema)
        with pytest.raises(Exception, match="not authorized"):
            upgrade(first_schema, test_versions=versions)
        assert snapshot(first_schema) == before

    @pytest.mark.parametrize(
        ("body", "url_query"),
        [
            pytest.param(f"{ORPHAN}\n{SWALLOWED_ROLLBACK}{ORPHAN}", "", id="INSERT OR ROLLBACK"),
            pytest.param(
                f"{ORPHAN}\n{STOPPED_INSERT}{ORPHAN}",
                "",
                id="RAISE(ROLLBACK) of a trigger in the file",
            ),
            pytest.param(
                f"{BOUND_INSERT.format('a')}\n{SWALLOWED_ROLLBACK}{BOUND_INSERT.format('b')}",
                "",
                id="bound parameters",
            ),
            pytest.param(
                f"{ORPHAN}\n{SWALLOWED_ROLLBACK}{ORPHAN}",
                "?cached_statements=128",
                id="statement cache asked for by the URL",
            ),
        ],
    )
    def test_a_statement_run_before_sqlite_ended_the_transaction_is_refused_after(
        self, migrated_database: Path, tmp_path: Path, body: str, url_query: str
    ) -> None:
        """SQLite authorizes a statement when it prepares it. A data-only upgrade changes no
        schema, so SQLite's own rollback expires nothing the driver could reuse by its text."""
        with closing(sqlite3.connect(migrated_database)) as connection, connection:
            connection.execute(STOP_TRIGGER)
        versions = tmp_path / "versions"
        write_revision(versions, TEST_REVISION, HEAD_REVISION, body)
        config = alembic_config(migrated_database, test_versions=versions)
        config.attributes["database_url"] += url_query
        before = snapshot(migrated_database)
        with pytest.raises(Exception, match="not authorized"):
            command.upgrade(config, "head")
        assert snapshot(migrated_database) == before

    @pytest.mark.parametrize(
        "statement",
        [
            # The rollback of a failed upgrade could not restore pages spilled to the file.
            "PRAGMA journal_mode=OFF",
            "PRAGMA main.JOURNAL_MODE=OFF",
            # A batch alter under a view would pass (gap 71).
            "PRAGMA legacy_alter_table=ON",
            # Rows the named CHECK constraints refuse would be committed.
            "PRAGMA ignore_check_constraints=ON",
            "PRAGMA writable_schema=ON",
            "SAVEPOINT early",
            "CREATE TEMP TABLE scratch (x INTEGER)",
            # Schema-qualified, these reach SQLite as the main database's action codes.
            "CREATE TABLE temp.scratch (x INTEGER)",
            "CREATE VIEW temp.scratch AS SELECT 1 AS x",
            "CREATE TRIGGER temp.drop_labels BEFORE INSERT ON pr_labels "
            "BEGIN SELECT RAISE(IGNORE); END",
            "ANALYZE",
        ],
    )
    def test_a_revision_runs_only_the_actions_the_authorizer_lists(
        self, migrated_database: Path, tmp_path: Path, statement: str
    ) -> None:
        versions = tmp_path / "versions"
        write_revision(versions, TEST_REVISION, HEAD_REVISION, f"op.execute({statement!r})")
        before = snapshot(migrated_database)
        with pytest.raises(Exception, match="not authorized"):
            upgrade(migrated_database, test_versions=versions)
        assert snapshot(migrated_database) == before

    def test_a_revision_cannot_attach_another_database(
        self, migrated_database: Path, tmp_path: Path
    ) -> None:
        """ATTACH would let a revision write a file the refusals never saw, such as another
        checkout's github_activity.db."""
        other = tmp_path / "other-checkout" / "github_activity.db"
        other.parent.mkdir()
        versions = tmp_path / "versions"
        write_revision(
            versions,
            TEST_REVISION,
            HEAD_REVISION,
            f'op.execute("ATTACH DATABASE {str(other)!r} AS other")\n'
            "op.execute('CREATE TABLE other.probe (x INTEGER)')",
        )
        before = snapshot(migrated_database)
        with pytest.raises(Exception, match="not authorized"):
            upgrade(migrated_database, test_versions=versions)
        assert snapshot(migrated_database) == before
        assert not other.exists()

    def test_a_failed_commit_rolls_back_cleanly(
        self,
        migrated_database: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A reader's lock makes the COMMIT fail; the rollback that follows is env.py's own."""
        versions = tmp_path / "versions"
        write_revision(
            versions, TEST_REVISION, HEAD_REVISION, "op.execute('CREATE TABLE probe (x INTEGER)')"
        )
        before = snapshot(migrated_database)
        real_connect = sqlite3.dbapi2.connect

        def connect_with_a_short_timeout(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **{**kwargs, "timeout": 0.1})

        reader = sqlite3.connect(migrated_database, isolation_level=None)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM pr_labels").fetchall()  # holds a shared lock
            monkeypatch.setattr(sqlite3.dbapi2, "connect", connect_with_a_short_timeout)
            with pytest.raises(Exception, match="database is locked"):
                upgrade(migrated_database, test_versions=versions)
            monkeypatch.undo()
        finally:
            reader.close()
        assert snapshot(migrated_database) == before
        assert not [record for record in caplog.records if "not authorized" in str(record.exc_info)]

    def test_the_upgrade_takes_the_write_lock_before_any_revision_runs(
        self, migrated_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BEGIN IMMEDIATE: while another connection holds the write lock, no revision runs."""
        marker = tmp_path / "revision-ran"
        versions = tmp_path / "versions"
        write_revision(
            versions,
            TEST_REVISION,
            HEAD_REVISION,
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n{ORPHAN}",
        )
        before = snapshot(migrated_database)
        real_connect = sqlite3.dbapi2.connect

        def connect_with_a_short_timeout(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return real_connect(*args, **{**kwargs, "timeout": 0.1})

        holder = sqlite3.connect(migrated_database, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            monkeypatch.setattr(sqlite3.dbapi2, "connect", connect_with_a_short_timeout)
            with pytest.raises(Exception, match="database is locked"):
                upgrade(migrated_database, test_versions=versions)
            monkeypatch.undo()
        finally:
            holder.close()
        assert not marker.exists()
        assert snapshot(migrated_database) == before


def _batch_alter_revision(views_sql: tuple[tuple[str, str], ...] | None) -> str:
    """A revision that batch-recreates pull_requests; with ``views_sql`` it drops and recreates
    the views reading it, from that frozen copy, as plan 5.1 requires."""
    drop = recreate = ""
    if views_sql is not None:
        drop = (
            f"for name in {VIEWS_READING_PULL_REQUESTS!r}:\n    op.execute(f'DROP VIEW {{name}}')\n"
        )
        recreate = f"for _name, sql in {views_sql!r}:\n    op.execute(sql)\n"
    return (
        drop
        + "with op.batch_alter_table('pull_requests', recreate='always') as batch:\n"
        + "    batch.add_column(sa.Column('probe', sa.Integer(), nullable=True))\n"
        + recreate
    )


def _seed_a_pull_request_with_children(database: Path) -> None:
    with connect(database) as connection:
        seed = Seed(connection)
        repository = seed.repository()
        author = seed.user("author")
        reviewer = seed.user("reviewer")
        pr = seed.pull_request(repository, 13481, author, state="MERGED", merged_at=T0)
        seed.review(pr, 1, reviewer, "APPROVED")
        seed.comment(pr, reviewer)
        seed.comment(pr, seed.user("commenter"))
        seed.commit(pr, "a" * 40, author)
        connection.exec_driver_sql(
            f"INSERT INTO pr_labels (pr_id, name, label_node_id) VALUES ({pr}, 'bug', 'LA_bug')"
        )
        connection.exec_driver_sql(
            "INSERT INTO pr_files (pr_id, ordinal, path, change_type, additions, deletions) "
            f"VALUES ({pr}, 0, 'a.js', 'MODIFIED', 1, 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO pr_closing_issues VALUES "
            f"({pr}, {repository + 1000}, 'prebid/Prebid.js', 1, 5846445299, 1)"
        )
        seed.review_request(pr, 0, account_id=reviewer, requested_type="User")
        seed.assignee(pr, author)
        connection.exec_driver_sql(
            "INSERT INTO user_tags (id, name, created_at) "
            "VALUES (1, 'kept', '2026-01-01 00:00:00.000000')"
        )
        connection.exec_driver_sql(
            f"INSERT INTO pr_user_tags (pr_id, user_tag_id) VALUES ({pr}, 1)"
        )


CHILD_TABLES = (
    "pr_labels",
    "pr_reviews",
    "pr_comments",
    "pr_commits",
    "pr_files",
    "pr_closing_issues",
    "pr_review_requests",
    "pr_assignees",
    "pr_user_tags",
)


class TestBatchAlter:
    def test_a_batch_alter_of_pull_requests_keeps_child_rows_and_constraints(
        self, migrated_database: Path, tmp_path: Path, head_template: Path
    ) -> None:
        _seed_a_pull_request_with_children(migrated_database)
        children = {
            table: query(migrated_database, f"SELECT * FROM {table}") for table in CHILD_TABLES
        }
        view_rows = {
            view: query(migrated_database, f"SELECT * FROM {view}")
            for view in VIEWS_READING_PULL_REQUESTS
        }
        constraints = named_constraints(migrated_database, "pull_requests")
        indexes = query(migrated_database, PULL_REQUEST_INDEXES)
        assert all(children.values())
        assert all(view_rows.values())
        views = frozen_views(alembic_config(head_template))
        frozen = tuple((name, views[name]) for name in VIEWS_READING_PULL_REQUESTS if name in views)
        assert len(frozen) == len(VIEWS_READING_PULL_REQUESTS)
        versions = tmp_path / "versions"
        write_revision(versions, TEST_REVISION, HEAD_REVISION, _batch_alter_revision(frozen))

        upgrade(migrated_database, test_versions=versions)

        assert revision_of(migrated_database) == TEST_REVISION
        assert "probe" in {
            row[1] for row in query(migrated_database, "PRAGMA table_info(pull_requests)")
        }
        assert {
            table: query(migrated_database, f"SELECT * FROM {table}") for table in CHILD_TABLES
        } == children
        assert {
            view: query(migrated_database, f"SELECT * FROM {view}")
            for view in VIEWS_READING_PULL_REQUESTS
        } == view_rows
        # Batch recreate keeps named CHECKs (gap 72), the named foreign keys, and the indexes.
        assert named_constraints(migrated_database, "pull_requests") == constraints
        assert {
            "ck_pull_requests_state",
            "ck_pull_requests_merged_at_iff_merged",
        } <= constraints.keys()
        assert query(migrated_database, PULL_REQUEST_INDEXES) == indexes

    def test_a_batch_alter_under_a_view_fails_and_applies_nothing(
        self, migrated_database: Path, tmp_path: Path
    ) -> None:
        """legacy_alter_table stays off, so a view left in place fails the migration (gap 71)."""
        versions = tmp_path / "versions"
        write_revision(versions, TEST_REVISION, HEAD_REVISION, _batch_alter_revision(None))
        before = snapshot(migrated_database)
        with pytest.raises(Exception, match=r"error in view v_\w+"):
            upgrade(migrated_database, test_versions=versions)
        assert snapshot(migrated_database) == before


class TestRefusals:
    @pytest.fixture(autouse=True)
    def tracked_file_unchanged(self) -> Iterator[None]:
        before = (digest(TRACKED), TRACKED.stat().st_mtime_ns)
        yield
        assert (digest(TRACKED), TRACKED.stat().st_mtime_ns) == before

    def test_refuses_the_tracked_file_named_by_database_url(
        self, monkeypatch: pytest.MonkeyPatch, isolated_settings: None
    ) -> None:
        """The spelling gap 73 found: DATABASE_URL, as the documented .env line sets it."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{TRACKED}")
        config = alembic_config(TRACKED)
        del config.attributes["database_url"]
        with pytest.raises(RuntimeError) as error:
            command.upgrade(config, "head")
        assert "refusing to migrate" in refusal(error)

    @pytest.mark.parametrize(
        "spelling",
        [
            "absolute",
            "relative",
            "dotted",
            "symlink",
            "after /tmp/..",
            "URI",
            "hard link",
            "symlinked directory then ..",
            "symlinked directory then .. to a hard link",
            "another checkout's file",
        ],
    )
    def test_refuses_the_tracked_file_of_a_copied_checkout(
        self, tmp_path: Path, first_schema: Path, spelling: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through a copy of alembic/, whose env.py takes the copy's parent as the checkout, so
        a regression migrates a stand-in, never the repository's own tracked file."""
        checkout = tmp_path / "checkout"
        shutil.copytree(
            REPO_ROOT / "alembic",
            checkout / "alembic",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        tracked = checkout / "github_activity.db"
        shutil.copyfile(first_schema, tracked)
        target = tracked
        (tmp_path / "far" / "deep").mkdir(parents=True)
        (checkout / "link").symlink_to(tmp_path / "far" / "deep")
        database = str(tracked)
        if spelling == "relative":
            monkeypatch.chdir(checkout)
            database = "github_activity.db"
        elif spelling == "dotted":
            database = f"{checkout}/alembic/../github_activity.db"
        elif spelling == "symlink":
            (tmp_path / "symlink.db").symlink_to(tracked)
            database = str(tmp_path / "symlink.db")
        elif spelling == "after /tmp/..":
            # Where /tmp is a symlink (macOS), resolving the path names a missing file, while
            # the driver drops `..` as text; the name check refuses it before connecting.
            database = f"/tmp/..{tracked}"  # noqa: S108 - the spelling under test
        elif spelling == "URI":
            database = f"file:{tracked}?uri=true"
        elif spelling == "hard link":
            (tmp_path / "linked.db").hardlink_to(tracked)
            database = str(tmp_path / "linked.db")
        elif spelling == "symlinked directory then ..":
            database = f"{checkout}/link/../github_activity.db"
        elif spelling == "symlinked directory then .. to a hard link":
            # Only the check of the file SQLite opened refuses this: the name is not the
            # tracked one, and the path resolved through the symlink names a missing file.
            (checkout / "linked.db").hardlink_to(tracked)
            database = f"{checkout}/link/../linked.db"
        else:
            target = tmp_path / "other-checkout" / "github_activity.db"
            target.parent.mkdir()
            shutil.copyfile(first_schema, target)
            database = str(target)
        before = snapshot(target)
        config = alembic_config(tmp_path / "unused.db", script_location=checkout / "alembic")
        config.attributes["database_url"] = f"sqlite+aiosqlite:///{database}"
        with pytest.raises(RuntimeError) as error:
            command.upgrade(config, "head")
        assert "refusing to migrate" in refusal(error)
        assert snapshot(target) == before
        # Positive control: the copied environment migrates a file that is not the tracked one.
        other = tmp_path / "other.db"
        shutil.copyfile(first_schema, other)
        upgrade(other, script_location=checkout / "alembic")
        assert revision_of(other) == HEAD_REVISION

    def test_the_file_sqlite_opened_is_refused_by_its_name(
        self, tmp_path: Path, first_schema: Path
    ) -> None:
        """The opened-file name check alone: the URL names alias.db, Python's realpath (symlinks
        first, then ..) names a missing file, and the driver, which drops .. as text, opens
        alias.db, a symlink to another checkout's github_activity.db."""
        target = tmp_path / "other-checkout" / "github_activity.db"
        target.parent.mkdir()
        shutil.copyfile(first_schema, target)
        checkout = tmp_path / "checkout"
        (tmp_path / "far" / "deep").mkdir(parents=True)
        checkout.mkdir()
        (checkout / "link").symlink_to(tmp_path / "far" / "deep")
        (checkout / "alias.db").symlink_to(target)
        before = snapshot(target)
        config = alembic_config(tmp_path / "unused.db")
        config.attributes["database_url"] = f"sqlite+aiosqlite:///{checkout}/link/../alias.db"
        with pytest.raises(RuntimeError) as error:
            command.upgrade(config, "head")
        assert "(the file SQLite opened)" in refusal(error)
        assert snapshot(target) == before

    @pytest.mark.parametrize(
        "spelling",
        [
            "path",
            "upper case",
            "symlink to a missing file",
            "symlinked directory then .. to a symlink to a missing file",
        ],
    )
    def test_a_missing_file_of_that_name_is_refused_before_it_is_created(
        self, tmp_path: Path, spelling: str
    ) -> None:
        directory = tmp_path / "fresh"
        directory.mkdir()
        database = directory / "github_activity.db"
        if spelling == "upper case":
            database = directory / "GITHUB_ACTIVITY.DB"
        elif spelling == "symlink to a missing file":
            (directory / "alias.db").symlink_to(directory / "github_activity.db")
            database = directory / "alias.db"
        elif spelling == "symlinked directory then .. to a symlink to a missing file":
            # The driver drops `..` as text and opens alias.db; Python's realpath of the URL's
            # path follows link first and names a missing far/alias.db.
            (tmp_path / "far" / "deep").mkdir(parents=True)
            (directory / "link").symlink_to(tmp_path / "far" / "deep")
            (directory / "alias.db").symlink_to(directory / "github_activity.db")
            database = directory / "link" / ".." / "alias.db"
        entries = sorted(directory.iterdir())
        with pytest.raises(RuntimeError) as error:
            upgrade(database)
        assert "refusing to migrate" in refusal(error)
        assert sorted(directory.iterdir()) == entries
        assert not (directory / "github_activity.db").exists()

    @pytest.mark.parametrize("name", ["other.db", "github_activity.db"])
    def test_sqlite_uri_filenames_are_refused(
        self, tmp_path: Path, first_schema: Path, name: str
    ) -> None:
        """URI parameters such as nolock or immutable would remove the locking an upgrade relies
        on, and only SQLite reads the path a URI names; github_activity.db here is missing."""
        target = tmp_path / name
        if name == "other.db":
            shutil.copyfile(first_schema, target)
        entries = sorted(tmp_path.iterdir())
        before = snapshot(target) if target.exists() else None
        config = alembic_config(tmp_path / "unused.db")
        config.attributes["database_url"] = f"sqlite+aiosqlite:///file:{target}?uri=true"
        with pytest.raises(RuntimeError) as error:
            command.upgrade(config, "head")
        assert "SQLite URI filenames are not supported" in refusal(error)
        assert sorted(tmp_path.iterdir()) == entries
        assert (snapshot(target) if target.exists() else None) == before

    def test_a_caller_supplied_connection_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_settings: None
    ) -> None:
        """alembic's documented attributes["connection"] would otherwise be ignored, and the
        settings' database migrated instead."""
        decoy = tmp_path / "decoy.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{decoy}")
        callers = tmp_path / "callers.db"
        config = alembic_config(callers)
        del config.attributes["database_url"]
        engine = create_engine(f"sqlite:///{callers}")
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                with pytest.raises(RuntimeError) as error:
                    command.upgrade(config, "head")
        finally:
            engine.dispose()
        assert "a caller-supplied connection is not used" in refusal(error)
        assert not decoy.exists()
        assert table_names(callers) == set()

    def test_sqlalchemy_url_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_settings: None
    ) -> None:
        decoy = tmp_path / "decoy.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{decoy}")
        named = tmp_path / "named.db"
        config = alembic_config(named)
        del config.attributes["database_url"]
        config.set_main_option("sqlalchemy.url", f"sqlite:///{named}")
        with pytest.raises(RuntimeError) as error:
            command.upgrade(config, "head")
        assert "sqlalchemy.url is not used" in refusal(error)
        assert not decoy.exists()
        assert not named.exists()

    def test_the_alembic_command_refuses_the_tracked_file(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={"PATH": "", "DATABASE_URL": "sqlite+aiosqlite:///./github_activity.db"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "MigrationRefused: refusing to migrate" in result.stderr

    def test_offline_mode_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "offline.db"
        with pytest.raises(RuntimeError) as error:
            command.upgrade(alembic_config(target), "head", sql=True)
        assert "offline --sql mode is not supported" in refusal(error)
        assert not target.exists()

    def test_refuses_other_databases(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        config = alembic_config(tmp_path / "unused.db")
        config.attributes["database_url"] = "postgresql://localhost/activity"
        with pytest.raises(RuntimeError) as error:
            command.upgrade(config, "head")
        assert "only SQLite is supported, not 'postgresql'" in refusal(error)
        assert list(tmp_path.iterdir()) == []


def test_head_tables_are_the_models_tables(head_template: Path) -> None:
    assert table_names(head_template) == set(Base.metadata.tables) | {"alembic_version"}


def revision_source_problems(path: Path) -> list[str]:
    """Plan 5.1's rules for a revision file: it imports only the standard library, alembic, and
    SQLAlchemy (no application code, no relative import), and its ``VIEWS`` is a literal."""
    tree = ast.parse(path.read_bytes())
    problems = [
        f"{path.name}:{line}: imports {module}"
        for line, module in imported_modules(path)
        if module.split(".")[0] not in REVISION_IMPORTS | sys.stdlib_module_names
    ]
    problems += [
        f"{path.name}:{node.lineno}: relative import"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "VIEWS" for t in targets):
            try:
                ast.literal_eval(value)
            except ValueError:
                problems.append(f"{path.name}:{node.lineno}: VIEWS is not a literal")
    return problems


REVISION_IMPORTS = frozenset({"alembic", "sqlalchemy"})


class TestRevisionSources:
    def test_every_revision_follows_the_rules(self) -> None:
        revisions = sorted((REPO_ROOT / "alembic" / "versions").glob("*.py"))
        assert len(revisions) == len(MIGRATION_REVISIONS)
        assert [problem for path in revisions for problem in revision_source_problems(path)] == []

    def test_the_newest_view_revision_has_a_literal_views_constant(self) -> None:
        """Positive control: the rule reads the VIEWS the chain actually has, in the newest
        revision that defines any."""

        def defines_views(path: Path) -> bool:
            return any(
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "VIEWS"
                for node in ast.parse(path.read_bytes()).body
            )

        versions = REPO_ROOT / "alembic" / "versions"
        newest = next(
            path
            for revision in reversed(MIGRATION_REVISIONS)
            for path in versions.glob(f"{revision}_*.py")
            if defines_views(path)
        )
        assert newest.name.startswith(STEP3B1)

    @pytest.mark.parametrize(
        ("source", "flagged"),
        [
            ("import json\nimport sqlalchemy as sa\nfrom alembic import op\n", False),
            ("VIEWS = (('v', 'CREATE VIEW v AS SELECT 1'),)\n", False),
            ("from typing import Final\nVIEWS: Final = (('v', 'SELECT 1'),)\n", False),
            ("from github_activity_db.db import models\n", True),
            ("import githubkit\n", True),
            ("from . import shared\n", True),
            ("import importlib\nimportlib.import_module('github_activity_db')\n", True),
            ("VIEWS = SHARED_VIEWS\n", True),
            ("VIEWS: tuple = tuple(view for view in ())\n", True),
            ("VIEWS = (('v', 'CREATE VIEW v AS SELECT ' + '1'),)\n", True),
        ],
    )
    def test_flags_each_rule_on_planted_files(
        self, tmp_path: Path, source: str, flagged: bool
    ) -> None:
        path = tmp_path / "f00000000001_planted.py"
        path.write_text(source)
        assert bool(revision_source_problems(path)) is flagged


def test_a_generated_revision_raises_in_downgrade_and_follows_the_rules(tmp_path: Path) -> None:
    """``alembic revision`` writes from alembic/script.py.mako."""
    versions = tmp_path / "versions"
    versions.mkdir()
    config = alembic_config(tmp_path / "unused.db", test_versions=versions)
    command.revision(config, message="probe", rev_id=TEST_REVISION, version_path=str(versions))
    (path,) = versions.glob(f"{TEST_REVISION}_*.py")
    assert revision_source_problems(path) == []
    spec = importlib.util.spec_from_file_location("generated_revision", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == HEAD_REVISION
    with pytest.raises(RuntimeError, match="migrations are never downgraded"):
        module.downgrade()
