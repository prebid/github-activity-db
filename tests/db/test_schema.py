"""The head schema against the models and the frozen view SQL (redesign plan 5.1).

``alembic check`` compares neither views nor CHECK constraints, so these tests compare a
database migrated to head with one ``metadata.create_all`` builds, structurally, and each view
with the newest migration's frozen SQL.
"""

from __future__ import annotations

import ast
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import create_engine, insert, pool
from sqlalchemy.exc import IntegrityError

from github_activity_db.db.models import (
    Base,
    RepoSyncState,
    SyncFailure,
    SyncPending,
    SyncRun,
)
from github_activity_db.source.records import AccountType, ReviewState
from tests.db.alembic_support import alembic_config, query
from tests.db.schema_support import (
    frozen_views,
    named_constraints,
    schema_of,
    schema_tables,
    unindexed_foreign_keys,
    view_differences,
)
from tests.db.seed import T0, Seed, connect


@pytest.fixture(scope="module")
def models_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("models") / "models.db"
    engine = create_engine(f"sqlite:///{path}", poolclass=pool.NullPool)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


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


class TestModelsComparison:
    def test_head_matches_the_models(self, head_template: Path, models_database: Path) -> None:
        migrated, models = schema_of(head_template), schema_of(models_database)
        assert sorted(migrated) == sorted(models)
        for table in models:
            assert migrated[table] == models[table], table

    def test_the_comparison_reads_every_table_and_check(self, models_database: Path) -> None:
        """Positive control: the readings hold the CHECKs and tables the models declare."""
        assert set(schema_tables(models_database)) == set(Base.metadata.tables)
        checks = {
            name
            for table in Base.metadata.tables
            for name, item in named_constraints(models_database, table).items()
            if " CHECK " in f" {item} "
        }
        assert checks == set(CHECKS) | {"ck_pull_requests_merged_at_iff_merged"}

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
            head_template, tmp_path / "tampered.db", "DROP INDEX ix_pr_reviews_pr_id"
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
            "v_pr_reviewers",
            "v_pr_commenters",
            "v_commit_authors",
        }
        assert view_differences(head_template, frozen) == []

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


class TestForeignKeyIndexes:
    # Kept from the first schema unchanged (plan 5.1).
    EXEMPT = frozenset({("pr_user_tags", ("user_tag_id",))})

    def test_every_foreign_key_column_leads_an_index(self, head_template: Path) -> None:
        # Equality, not a subset: the exemption is still needed, and nothing else is missing.
        assert unindexed_foreign_keys(head_template) == self.EXEMPT

    def test_a_missing_index_is_reported(self, head_template: Path, tmp_path: Path) -> None:
        tampered = _tampered(
            head_template, tmp_path / "tampered.db", "DROP INDEX ix_pr_comments_author_id"
        )
        assert unindexed_foreign_keys(tampered) == self.EXEMPT | {("pr_comments", ("author_id",))}

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


def _sync_run(seed: Seed, **values: object) -> int:
    row = {
        "started_at": T0,
        "status": "running",
        "trigger": "manual",
        "code_version": "test",
        "graphql_points": 0,
        "rest_calls": 0,
        "items_planned": 0,
        "items_hydrated": 0,
        "items_deleted": 0,
        "failures": 0,
    }
    result = seed.connection.execute(insert(SyncRun).values(**{**row, **values}))
    assert result.inserted_primary_key is not None
    return int(result.inserted_primary_key[0])


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
        )
    )


def _review(seed: Seed, state: str) -> None:
    pr = seed.pull_request(seed.repository(), 1)
    seed.review(pr, seed.next_id(), None, state)


Write = Callable[[Seed, str], object]
SYNC_STATUSES = ("running", "complete", "time_limited", "failed", "aborted")
TRIGGERS = ("schedule", "workflow_dispatch", "manual")
OUTCOMES = ("complete", "time_limited", "failed", "incomplete_index", "halted", "untracked")
ITEM_KINDS = ("pull_request", "issue")

# Every CHECK at head: a write of one value, the values it must accept, and one it must reject.
CHECKS: dict[str, tuple[Write, tuple[str, ...], str]] = {
    "ck_github_users_type": (
        lambda seed, value: seed.user("u", type=value),
        get_args(AccountType),
        "EnterpriseUserAccount",
    ),
    "ck_pull_requests_state": (
        lambda seed, value: seed.pull_request(seed.repository(), 1, state=value),
        ("OPEN",),
        "CLOSED",
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
        assert at_head == set(CHECKS) | {"ck_pull_requests_merged_at_iff_merged"}

    @pytest.mark.parametrize("name", sorted(CHECKS))
    def test_accepts_its_values_and_rejects_another(
        self, migrated_database: Path, name: str
    ) -> None:
        write, accepted, rejected = CHECKS[name]
        for value in accepted:
            with connect(migrated_database) as connection:
                write(Seed(connection), value)
        with pytest.raises(IntegrityError, match=f"CHECK constraint failed: {name}"):
            with connect(migrated_database) as connection:
                write(Seed(connection), rejected)

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
            with pytest.raises(
                IntegrityError,
                match="CHECK constraint failed: ck_pull_requests_merged_at_iff_merged",
            ):
                write()


ROOT = Path(__file__).resolve().parents[2]


def create_all_uses(*roots: Path) -> list[str]:
    """Every ``.create_all`` attribute, called or passed as ``run_sync(metadata.create_all)``."""
    found = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, ast.Attribute) and node.attr == "create_all":
                    found.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    return found


class TestBuiltByMigrations:
    """Databases are built by ``alembic upgrade head``; ``create_all`` only builds the models'
    side of the comparison above (plan Step 3)."""

    def test_create_all_is_used_only_by_the_comparison(self) -> None:
        uses = create_all_uses(ROOT / "src", ROOT / "tests", ROOT / "scripts", ROOT / "alembic")
        assert [use.split(":")[0] for use in uses] == ["tests/db/test_schema.py"], uses

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
        assert bool(create_all_uses(package)) is flagged
