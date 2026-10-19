"""The writer's direct tests (plan 5.1, 5.2, 5.3 steps 1, 3, 6, 7; D17, D27, D28, D31)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from github_activity_db.source.records import (
    ConnectionTotals,
    FileChange,
    LinkedClosingIssue,
    Milestone,
    OrganizationProfile,
    PullRequestRecord,
    RepositoryLabel,
    RepositoryRecord,
    Stack,
    StackEntry,
    UserProfile,
)
from github_activity_db.sync.connection import RunAborted, open_writer
from github_activity_db.sync.deletions import (
    Candidate,
    DeletionApproval,
    Deletions,
    GuardDecision,
)
from github_activity_db.sync.plan import InPlaceUpdate
from github_activity_db.sync.writer import (
    REFRESH_AFTER,
    AccountMissing,
    Found,
    ItemConflict,
    LinkAccount,
    LookupConflict,
    NotFound,
    Pruned,
    RepositoryConflict,
    SecondNodeId,
    Sighted,
    accounts_to_refresh,
    apply_in_place_updates,
    apply_item_deletions,
    full_name_conflict,
    mark_untracked,
    prune_repositories,
    record_failure,
    stored_accounts,
    tracked_repository_named,
    write_lookup,
    write_pull_request,
    write_repository,
)
from github_activity_db.tracking import TrackedRepository
from tests.db.seed import Seed, connect
from tests.sync.records import (
    HYDRATED,
    T0,
    account,
    closing_issue,
    comment,
    commit,
    fetch,
    link,
    looked_up,
    pull_request,
    repository,
    request_for,
    request_for_team,
    request_without_reviewer,
    review,
    ts,
)

ALICE = account("alice", 101)
BOB = account("bob", 102)
CAROL = account("carol", 103)
DAVE = account("dave", 104)
DEPENDABOT = account("dependabot", 49699333, "Bot", node_id="MDM6Qm90NDk2OTkzMzM=")
JS = repository()
TRACKED_JS = TrackedRepository(owner="prebid", name="Prebid.js", group="Client")


def _url(database: Path) -> str:
    return f"sqlite+aiosqlite:///{database}"


def _run(database: Path) -> int:
    with connect(database) as connection:
        return Seed(connection).sync_run()


class Writer:
    """The writer on a migrated database with one running run."""

    def __init__(self, database: Path) -> None:
        self.database = database
        self.run_id = _run(database)

    async def repository(self, record: Any = JS, group: str = "Client") -> Any:
        async with open_writer(_url(self.database)) as writer:
            return await write_repository(writer, self.run_id, record, group=group)

    async def item(
        self, record: Any, links: dict[int, LinkAccount] | None = None, repo: int = 11
    ) -> Any:
        async with open_writer(_url(self.database)) as writer:
            return await write_pull_request(
                writer, self.run_id, record, repository_github_id=repo, links=links or {}
            )

    def rows(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        return fetch(self.database, sql, *params)

    def account_row(self, login: str) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM github_users WHERE login = ?", login)
        return row

    def pr(self, number: int) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM pull_requests WHERE number = ?", number)
        return row

    def without_ids(self, table: str) -> list[dict[str, Any]]:
        """A table's rows with their own row id dropped, in id order."""
        rows = self.rows(f"SELECT * FROM {table} ORDER BY id")
        for row in rows:
            assert isinstance(row.pop("id"), int)
        return rows

    def dump(self) -> dict[str, list[str]]:
        """Every table's rows, for comparing the whole file before and after."""
        tables = self.rows("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        return {
            table["name"]: sorted(
                repr(sorted(row.items())) for row in self.rows(f"SELECT * FROM {table['name']}")
            )
            for table in tables
        }


@pytest.fixture
def w(migrated_database: Path) -> Writer:
    return Writer(migrated_database)


# --------------------------------------------------------------------------------------------
# Step 1: repositories
# --------------------------------------------------------------------------------------------
async def test_a_repository_is_written_tracked_with_its_group_and_labels(w: Writer) -> None:
    """Every column, from a record whose values differ column to column where they can."""
    record = replace(
        repository(
            description="a repository",
            homepage_url="https://prebid.org",
            is_security_policy_enabled=True,
            updated_at=T0 - timedelta(hours=1),
            pushed_at=T0 - timedelta(hours=2),
            fork_count=3,
        ),
        labels=(
            RepositoryLabel(
                node_id="LA_1",
                name="bug",
                color="d73a4a",
                description="Something is broken",
                is_default=True,
                created_at=T0 - timedelta(days=5),
                updated_at=T0 - timedelta(days=4),
            ),
            RepositoryLabel(
                node_id="LA_2",
                name="feature",
                color="a2eeef",
                description=None,
                is_default=False,
                created_at=T0 - timedelta(days=3),
                updated_at=None,
            ),
        ),
        labels_total=2,
    )
    result = await w.repository(record, group="Client")
    assert (result.inserted, result.default_branch_changed) == (True, False)
    assert w.rows("SELECT * FROM repositories") == [
        {
            "id": result.row_id,
            "github_id": 11,
            "node_id": "R_11",
            "owner": "prebid",
            "name": "Prebid.js",
            "full_name": "prebid/Prebid.js",
            "repo_group": "Client",
            "is_private": 0,
            "is_archived": 0,
            "default_branch": "master",
            "labels_total": 2,
            "is_tracked": 1,
            "resolved_at": ts(T0),
            "is_security_policy_enabled": 1,
            "visibility": "PUBLIC",
            "description": "a repository",
            "homepage_url": "https://prebid.org",
            "created_at": ts(T0 - timedelta(days=1000)),
            "updated_at": ts(T0 - timedelta(hours=1)),
            "pushed_at": ts(T0 - timedelta(hours=2)),
            "archived_at": None,
            "stargazer_count": 5,
            "fork_count": 3,
            "disk_usage": 100,
            "forking_allowed": 1,
            "allow_update_branch": 0,
            "web_commit_signoff_required": 0,
            "has_wiki_enabled": 1,
            "has_discussions_enabled": 0,
            "has_projects_enabled": 1,
            "has_vulnerability_alerts_enabled": 1,
            "uses_custom_open_graph_image": 0,
            "merge_commit_allowed": 1,
            "squash_merge_allowed": 1,
            "rebase_merge_allowed": 0,
            "delete_branch_on_merge": 1,
            "merge_commit_title": "MERGE_MESSAGE",
            "merge_commit_message": "PR_TITLE",
            "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
            "squash_merge_commit_message": "COMMIT_MESSAGES",
            "primary_language": "JavaScript",
            "license_spdx_id": "Apache-2.0",
            "plan_features_codeowners": 1,
            "plan_features_team_review_requests": 1,
            "plan_features_maximum_manual_review_requests": 15,
        }
    ]
    assert w.rows("SELECT * FROM repo_labels ORDER BY node_id") == [
        {
            "repository_id": result.row_id,
            "node_id": "LA_1",
            "name": "bug",
            "color": "d73a4a",
            "description": "Something is broken",
            "is_default": 1,
            "created_at": ts(T0 - timedelta(days=5)),
            "updated_at": ts(T0 - timedelta(days=4)),
        },
        {
            "repository_id": result.row_id,
            "node_id": "LA_2",
            "name": "feature",
            "color": "a2eeef",
            "description": None,
            "is_default": 0,
            "created_at": ts(T0 - timedelta(days=3)),
            "updated_at": None,
        },
    ]


def _flags(record_type: type) -> tuple[str, ...]:
    return tuple(field.name for field in fields(record_type) if "bool" in str(field.type))


def _bit(code: int, record: int) -> int:
    return (code >> record) & 1


async def test_each_repository_flag_is_written_to_its_own_column(w: Writer) -> None:
    """Five repositories; flag i holds the bits of i + 1 across them, a pattern no other flag
    has, so a flag written to another flag's column fails."""
    flags = _flags(RepositoryRecord)
    assert len(flags) == 17
    for record in range(5):
        values: dict[str, Any] = {
            flag: bool(_bit(code, record)) for code, flag in enumerate(flags, start=1)
        }
        await w.repository(
            repository(11 + record, f"prebid/r{record}", node_id=f"R_{record}", **values)
        )
    rows = w.rows("SELECT * FROM repositories ORDER BY github_id")
    assert {flag: tuple(row[flag] for row in rows) for flag in flags} == {
        flag: tuple(_bit(code, record) for record in range(5))
        for code, flag in enumerate(flags, start=1)
    }


async def test_a_repository_update_keeps_its_row_and_replaces_its_labels(w: Writer) -> None:
    first = await w.repository(repository(labels=[("LA_1", "bug"), ("LA_2", "feature")]))
    with connect(w.database) as connection:
        connection.execute(text("UPDATE repositories SET is_tracked = 0"))
    renamed = repository(
        full_name="prebid/Prebid-js",
        labels=[("LA_1", "defect")],
        resolved_at=T0 + timedelta(hours=1),
        stargazer_count=9,
    )
    second = await w.repository(renamed, group="Tools")
    assert (second.row_id, second.inserted) == (first.row_id, False)
    (row,) = w.rows(
        "SELECT full_name, name, repo_group, is_tracked, stargazer_count, resolved_at, "
        "labels_total FROM repositories"
    )
    assert row == {
        "full_name": "prebid/Prebid-js",
        "name": "Prebid-js",
        "repo_group": "Tools",
        "is_tracked": 1,
        "stargazer_count": 9,
        "resolved_at": ts(T0 + timedelta(hours=1)),
        "labels_total": 1,
    }
    assert w.rows("SELECT node_id, name FROM repo_labels") == [
        {"node_id": "LA_1", "name": "defect"}
    ]


async def test_a_default_branch_change_recomputes_targets_default_branch(w: Writer) -> None:
    await w.repository(repository(default_branch="master"))
    await w.item(pull_request(1, 10, base_ref="master"))
    await w.item(pull_request(2, 20, base_ref="main"))
    assert [w.pr(1)["targets_default_branch"], w.pr(2)["targets_default_branch"]] == [1, 0]
    result = await w.repository(repository(default_branch="main"))
    assert result.default_branch_changed
    assert [w.pr(1)["targets_default_branch"], w.pr(2)["targets_default_branch"]] == [0, 1]


async def test_an_unchanged_default_branch_leaves_targets_default_branch(w: Writer) -> None:
    """A planted wrong value stays: only a change of default branch recomputes it."""
    await w.repository(repository(default_branch="master"))
    await w.item(pull_request(1, 10, base_ref="master"))
    with connect(w.database) as connection:
        connection.execute(text("UPDATE pull_requests SET targets_default_branch = 0"))
    result = await w.repository(repository(default_branch="master"))
    assert not result.default_branch_changed
    assert w.pr(1)["targets_default_branch"] == 0


@pytest.mark.parametrize("spelling", ["prebid/Prebid.js", "PREBID/prebid.JS"])
async def test_a_name_another_tracked_repository_holds_is_refused(w: Writer, spelling: str) -> None:
    await w.repository(repository(github_id=11, full_name="prebid/Prebid.js"))
    with pytest.raises(RepositoryConflict) as refused:
        await w.repository(repository(github_id=12, full_name=spelling, labels=[("LA_9", "x")]))
    assert refused.value.holder_github_id == 11
    assert w.rows("SELECT github_id FROM repositories") == [{"github_id": 11}]
    assert w.rows("SELECT count(*) AS n FROM repo_labels") == [{"n": 0}]


async def test_an_untracked_holder_of_the_name_does_not_conflict(w: Writer) -> None:
    await w.repository(repository(github_id=11, full_name="prebid/Prebid.js"))
    async with open_writer(_url(w.database)) as writer:
        await mark_untracked(writer, w.run_id, [11])
    await w.repository(repository(github_id=12, full_name="prebid/Prebid.js", node_id="R_12"))
    assert w.rows("SELECT github_id, is_tracked FROM repositories ORDER BY github_id") == [
        {"github_id": 11, "is_tracked": 0},
        {"github_id": 12, "is_tracked": 1},
    ]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ([(1, "prebid/X", True)], 1),
        ([(1, "PREBID/x", True)], 1),
        ([(1, "prebid/X", False)], None),
        ([(2, "prebid/X", True)], None),
        ([(1, "prebid/Y", True)], None),
    ],
    ids=["tracked", "ignoring-case", "untracked", "the-same-repository", "another-name"],
)
def test_full_name_conflict(stored: list[tuple[int, str, bool]], expected: int | None) -> None:
    assert full_name_conflict(stored, 2, "prebid/x") == expected


async def test_the_tracked_repository_for_a_configured_name(w: Writer) -> None:
    await w.repository(repository(github_id=11, full_name="prebid/Prebid.js"))
    await w.repository(repository(github_id=12, full_name="prebid/old", node_id="R_12"))
    async with open_writer(_url(w.database)) as writer:
        await mark_untracked(writer, w.run_id, [12])
        found = await tracked_repository_named(writer, "PREBID/prebid.js")
        untracked = await tracked_repository_named(writer, "prebid/old")
    assert found is not None and (found.github_id, found.full_name) == (11, "prebid/Prebid.js")
    assert untracked is None


# --------------------------------------------------------------------------------------------
# The run guard
# --------------------------------------------------------------------------------------------
GuardedCall = Callable[[AsyncConnection, int, int], Awaitable[object]]
GUARDED: dict[str, GuardedCall] = {
    "write_repository": lambda c, run, pr: write_repository(
        c, run, repository(stargazer_count=99), group="Tools"
    ),
    "mark_untracked": lambda c, run, pr: mark_untracked(c, run, [11]),
    "apply_in_place_updates": lambda c, run, pr: apply_in_place_updates(
        c, run, [InPlaceUpdate(11, "pull_request", pr, 1, True, "CONTRIBUTOR")]
    ),
    "write_pull_request": lambda c, run, pr: write_pull_request(
        c, run, pull_request(2, 20, author=BOB), repository_github_id=11, links={}
    ),
    "write_lookup-found": lambda c, run, pr: write_lookup(
        c, run, "N_alice", Found(looked_up(ALICE, name="New"), T0 + timedelta(days=1))
    ),
    "write_lookup-not-found": lambda c, run, pr: write_lookup(
        c, run, "N_alice", NotFound(T0 + timedelta(days=1))
    ),
    "record_failure": lambda c, run, pr: record_failure(
        c,
        run,
        repository_github_id=11,
        number=9,
        error=ValueError("x"),
        failed_at=T0,
        run_started_at=T0,
    ),
    "apply_item_deletions": lambda c, run, pr: apply_item_deletions(
        c, run, _deletions((pr,), (10,), runs=True)
    ),
    "prune_repositories": lambda c, run, pr: prune_repositories(c, run, [12]),
}


@pytest.mark.parametrize("call", list(GUARDED.values()), ids=list(GUARDED))
async def test_a_run_another_process_aborted_writes_nothing(w: Writer, call: GuardedCall) -> None:
    """Each writer function checks the run's row before writing (5.3 step 6): aborted, the
    file is unchanged; running again, the same call changes it."""
    await w.repository()
    await w.repository(repository(github_id=12, full_name="prebid/gone", node_id="R_12"))
    pr = (await w.item(pull_request(1, 10, author=ALICE))).row_id
    async with open_writer(_url(w.database)) as writer:
        await mark_untracked(writer, w.run_id, [12])
    with connect(w.database) as connection:
        connection.execute(text("UPDATE sync_runs SET status = 'aborted'"))
    before = w.dump()
    with pytest.raises(RunAborted):
        async with open_writer(_url(w.database)) as writer:
            await call(writer, w.run_id, pr)
    assert w.dump() == before
    with connect(w.database) as connection:
        connection.execute(text("UPDATE sync_runs SET status = 'running'"))
    running = w.dump()
    async with open_writer(_url(w.database)) as writer:
        await call(writer, w.run_id, pr)
    assert w.dump() != running


# --------------------------------------------------------------------------------------------
# Step 3: in-place updates
# --------------------------------------------------------------------------------------------
async def test_in_place_updates_change_only_draft_and_association(w: Writer) -> None:
    await w.repository()
    written = await w.item(pull_request(1, 10, github_updated_at=T0))
    before = w.pr(1)
    async with open_writer(_url(w.database)) as writer:
        await apply_in_place_updates(
            writer,
            w.run_id,
            [InPlaceUpdate(11, "pull_request", written.row_id, 1, True, "CONTRIBUTOR")],
        )
    after = w.pr(1)
    assert (after["is_draft"], after["author_association"]) == (1, "CONTRIBUTOR")
    assert {k: v for k, v in after.items() if k not in ("is_draft", "author_association")} == {
        k: v for k, v in before.items() if k not in ("is_draft", "author_association")
    }


# --------------------------------------------------------------------------------------------
# Step 6: an item
# --------------------------------------------------------------------------------------------
def _minutes(n: int) -> Any:
    return T0 + timedelta(minutes=n)


async def test_a_first_write_stores_every_column(w: Writer) -> None:
    """Every column of the item, its child rows, and the accounts it sighted, from a record
    whose values differ column to column where they can (5.1, 5.3 step 6). `changed_files` is
    GitHub's figure, never the file rows' count, and each total is the record's."""
    repository_id = (await w.repository()).row_id
    record = pull_request(
        7,
        70,
        author=ALICE,
        merged_by=BOB,
        editor=CAROL,
        state="MERGED",
        is_draft=False,
        title="Add a bidder",
        body="The body",
        author_association="CONTRIBUTOR",
        created_at=T0 - timedelta(days=3),
        published_at=T0 - timedelta(days=2),
        merged_at=_minutes(1),
        closed_at=_minutes(2),
        last_edited_at=T0 - timedelta(days=1),
        includes_created_edit=True,
        maintainer_can_modify=False,
        merge_commit_oid="m" * 40,
        milestone=Milestone(number=3, title="v9"),
        review_decision="APPROVED",
        mergeable="CONFLICTING",
        merge_state_status="DIRTY",
        can_be_rebased=False,
        stack=Stack(node_id="S_1", number=4, size=8, base_ref_name="stack-base"),
        stack_entry=StackEntry(node_id="SE_1", position=5),
        additions=31,
        deletions=27,
        changed_files=1,
        total_comments_count=6,
        labels=[("LA_1", "bug"), ("LA_2", "feature")],
        files=[FileChange("a.js", "MODIFIED", 1, 2), FileChange("a.js", "RENAMED", 3, 4)],
        closing_issues=[
            LinkedClosingIssue(
                repo_github_id=777,
                repo_full_name="prebid/issues-a",
                number=5,
                github_id=90005,
                user_linked=True,
            ),
            LinkedClosingIssue(
                repo_github_id=778,
                repo_full_name="prebid/issues-b",
                number=6,
                github_id=90006,
                user_linked=False,
            ),
        ],
        reviews=[
            review(
                900,
                BOB,
                state="CHANGES_REQUESTED",
                created_at=_minutes(10),
                submitted_at=_minutes(11),
                published_at=_minutes(12),
                updated_at=_minutes(13),
                last_edited_at=_minutes(14),
                editor=ALICE,
                includes_created_edit=True,
                commit_oid="c" * 40,
                author_association="COLLABORATOR",
                author_can_push_to_repository=False,
                inline_comments_total=2,
            )
        ],
        comments=[
            comment(
                800,
                ALICE,
                created_at=_minutes(20),
                published_at=_minutes(21),
                updated_at=_minutes(22),
                last_edited_at=_minutes(23),
                editor=BOB,
                created_via_email=True,
                includes_created_edit=False,
                author_association="NONE",
            )
        ],
        commits=[
            commit(
                "s1",
                link(ALICE),
                author_name="Alice A",
                author_email="alice@example.com",
                authored_at=_minutes(30),
            ),
            commit(
                "s2",
                None,
                author_name="Someone",
                author_email="someone@example.com",
                authored_at=_minutes(31),
            ),
        ],
        review_requests=[request_for(DAVE)],
        assignees=[CAROL],
        github_updated_at=_minutes(40),
        totals=ConnectionTotals(
            labels=11,
            closing_issues=12,
            user_linked_closing_issues=13,
            reviews=14,
            comments=15,
            commits=16,
            files=17,
            review_requests=18,
            assignees=19,
        ),
    )
    written = await w.item(record)
    pr = written.row_id
    alice, bob, carol, dave = (
        w.account_row(login)["id"] for login in ("alice", "bob", "carol", "dave")
    )
    assert w.rows("SELECT * FROM pull_requests") == [
        {
            "id": pr,
            "repository_id": repository_id,
            "number": 7,
            "github_id": 70,
            "node_id": "PR_70",
            "state": "MERGED",
            "is_draft": 0,
            "title": "Add a bidder",
            "body": "The body",
            "author_id": alice,
            "author_reported": 1,
            "author_association": "CONTRIBUTOR",
            "created_at": ts(T0 - timedelta(days=3)),
            "merged_at": ts(_minutes(1)),
            "closed_at": ts(_minutes(2)),
            "published_at": ts(T0 - timedelta(days=2)),
            "last_edited_at": ts(T0 - timedelta(days=1)),
            "editor_id": carol,
            "includes_created_edit": 1,
            "maintainer_can_modify": 0,
            "merged_by_id": bob,
            "merged_by_reported": 1,
            "base_ref": "master",
            "base_ref_oid": "b" * 40,
            "head_ref_name": "feature-7",
            "head_ref_oid": "h" * 40,
            "head_repo_github_id": 55,
            "head_repo_full_name": "fork/Prebid.js",
            "head_repo_owner_node_id": "O_fork",
            "head_repo_owner_login": "fork",
            "merge_commit_oid": "m" * 40,
            "targets_default_branch": 1,
            "is_cross_repository": 1,
            "milestone_number": 3,
            "milestone_title": "v9",
            "review_decision": "APPROVED",
            "mergeable": "CONFLICTING",
            "merge_state_status": "DIRTY",
            "can_be_rebased": 0,
            "potential_merge_commit_oid": "p" * 40,
            "stack_node_id": "S_1",
            "stack_number": 4,
            "stack_size": 8,
            "stack_base_ref_name": "stack-base",
            "stack_entry_node_id": "SE_1",
            "stack_position": 5,
            "additions": 31,
            "deletions": 27,
            "changed_files": 1,
            "total_comments_count": 6,
            "commits_total": 16,
            "files_total": 17,
            "reviews_total": 14,
            "comments_total": 15,
            "labels_total": 11,
            "closing_issues_total": 12,
            "user_linked_closing_issues_total": 13,
            "review_requests_total": 18,
            "assignees_total": 19,
            "github_updated_at": ts(_minutes(40)),
            "hydrated_at": ts(HYDRATED),
            "hydration_version": 2,
        }
    ]
    assert w.rows("SELECT * FROM pr_labels ORDER BY label_node_id") == [
        {"pr_id": pr, "name": "bug", "label_node_id": "LA_1"},
        {"pr_id": pr, "name": "feature", "label_node_id": "LA_2"},
    ]
    assert w.rows("SELECT * FROM pr_files ORDER BY ordinal") == [
        {
            "pr_id": pr,
            "ordinal": 0,
            "path": "a.js",
            "change_type": "MODIFIED",
            "additions": 1,
            "deletions": 2,
        },
        {
            "pr_id": pr,
            "ordinal": 1,
            "path": "a.js",
            "change_type": "RENAMED",
            "additions": 3,
            "deletions": 4,
        },
    ]
    assert w.rows("SELECT * FROM pr_closing_issues ORDER BY issue_number") == [
        {
            "pr_id": pr,
            "issue_repo_github_id": 777,
            "issue_repo_full_name": "prebid/issues-a",
            "issue_number": 5,
            "issue_github_id": 90005,
            "user_linked": 1,
        },
        {
            "pr_id": pr,
            "issue_repo_github_id": 778,
            "issue_repo_full_name": "prebid/issues-b",
            "issue_number": 6,
            "issue_github_id": 90006,
            "user_linked": 0,
        },
    ]
    assert w.without_ids("pr_reviews") == [
        {
            "github_id": 900,
            "node_id": "PRR_900",
            "pr_id": pr,
            "reviewer_id": bob,
            "reviewer_reported": 1,
            "state": "CHANGES_REQUESTED",
            "created_at": ts(_minutes(10)),
            "submitted_at": ts(_minutes(11)),
            "published_at": ts(_minutes(12)),
            "updated_at": ts(_minutes(13)),
            "last_edited_at": ts(_minutes(14)),
            "editor_id": alice,
            "includes_created_edit": 1,
            "commit_oid": "c" * 40,
            "author_association": "COLLABORATOR",
            "author_can_push_to_repository": 0,
            "inline_comments_total": 2,
        }
    ]
    assert w.without_ids("pr_comments") == [
        {
            "github_id": 800,
            "node_id": "IC_800",
            "pr_id": pr,
            "author_id": alice,
            "author_reported": 1,
            "created_at": ts(_minutes(20)),
            "published_at": ts(_minutes(21)),
            "updated_at": ts(_minutes(22)),
            "last_edited_at": ts(_minutes(23)),
            "editor_id": bob,
            "created_via_email": 1,
            "includes_created_edit": 0,
            "author_association": "NONE",
        }
    ]
    assert w.rows("SELECT * FROM pr_commits ORDER BY sha") == [
        {
            "pr_id": pr,
            "sha": "s1",
            "author_id": alice,
            "linked_github_id": 101,
            "author_name": "Alice A",
            "author_email": "alice@example.com",
            "authored_at": ts(_minutes(30)),
        },
        {
            "pr_id": pr,
            "sha": "s2",
            "author_id": None,
            "linked_github_id": None,
            "author_name": "Someone",
            "author_email": "someone@example.com",
            "authored_at": ts(_minutes(31)),
        },
    ]
    assert w.rows("SELECT * FROM pr_review_requests") == [
        {
            "pr_id": pr,
            "ordinal": 0,
            "request_github_id": 501,
            "request_node_id": "RR_501",
            "requested_type": "User",
            "requested_github_id": 104,
            "requested_node_id": "N_dave",
            "account_id": dave,
            "team_slug": None,
            "as_code_owner": 0,
        }
    ]
    assert w.rows("SELECT * FROM pr_assignees") == [{"pr_id": pr, "account_id": carol}]
    assert w.without_ids("github_users") == [
        {
            "github_id": github_id,
            "node_id": f"N_{login}",
            "login": login,
            "type": "User",
            "name": None,
            "created_at": None,
            "updated_at": None,
            "linked_user_github_id": None,
            "claimant_github_id": None,
            "refreshed_at": ts(HYDRATED),
            "not_found_at": None,
        }
        for login, github_id in sorted(
            (("alice", 101), ("bob", 102), ("carol", 103), ("dave", 104)),
            key=lambda entry: w.account_row(entry[0])["id"],
        )
    ]
    assert set(written.inserted_accounts) == {"N_alice", "N_bob", "N_carol", "N_dave"}


async def test_each_pull_request_flag_is_written_to_its_own_column(w: Writer) -> None:
    """Three pull requests; flag i holds the bits of i + 1 across them, a pattern no other flag
    has, so a flag written to another flag's column fails."""
    await w.repository()
    flags = _flags(PullRequestRecord)
    assert len(flags) == 5
    for record in range(3):
        values: dict[str, Any] = {
            flag: bool(_bit(code, record)) for code, flag in enumerate(flags, start=1)
        }
        await w.item(pull_request(record + 1, (record + 1) * 10, **values))
    rows = w.rows("SELECT * FROM pull_requests ORDER BY number")
    assert {flag: tuple(row[flag] for row in rows) for flag in flags} == {
        flag: tuple(_bit(code, record) for record in range(3))
        for code, flag in enumerate(flags, start=1)
    }


async def test_a_closed_pull_request_and_its_reopening(w: Writer) -> None:
    """D3: closed without a merge, it is stored as CLOSED; reopened, its row is updated."""
    await w.repository()
    closed = await w.item(pull_request(3, 30, state="CLOSED", closed_at=T0))
    columns = ("id", "state", "closed_at", "merged_at", "merged_by_id", "hydrated_at")
    assert {key: w.pr(3)[key] for key in columns} == {
        "id": closed.row_id,
        "state": "CLOSED",
        "closed_at": ts(T0),
        "merged_at": None,
        "merged_by_id": None,
        "hydrated_at": ts(HYDRATED),
    }
    reopened = await w.item(
        pull_request(3, 30, state="OPEN", hydrated_at=HYDRATED + timedelta(days=1))
    )
    assert reopened.row_id == closed.row_id
    assert {key: w.pr(3)[key] for key in columns} == {
        "id": closed.row_id,
        "state": "OPEN",
        "closed_at": None,
        "merged_at": None,
        "merged_by_id": None,
        "hydrated_at": ts(HYDRATED + timedelta(days=1)),
    }


async def test_an_account_sighting_inserts_and_never_relabels(w: Writer) -> None:
    """Inserted with the sighting's login and type and the hydration's Date; a later sighting in
    another shape keeps both (5.2: 175728472 is Copilot and copilot-pull-request-reviewer)."""
    await w.repository()
    copilot = account("Copilot", 175728472, "Bot", node_id="BOT_kgDOCnlnWA")
    await w.item(pull_request(1, 10, reviews=[review(900, copilot)]))
    reviewer = account("copilot-pull-request-reviewer", 175728472, "User", node_id="BOT_kgDOCnlnWA")
    await w.item(
        pull_request(
            2, 20, reviews=[review(901, reviewer)], hydrated_at=HYDRATED + timedelta(days=3)
        )
    )
    row = w.account_row("Copilot")
    assert (row["github_id"], row["type"], row["refreshed_at"]) == (175728472, "Bot", ts(HYDRATED))
    assert w.rows("SELECT count(*) AS n FROM github_users") == [{"n": 1}]


async def test_a_sighting_clears_not_found_at(w: Writer) -> None:
    await w.repository()
    with connect(w.database) as connection:
        Seed(connection).user("alice", github_id=101, node_id="N_alice", not_found_at=T0)
    await w.item(pull_request(1, 10, author=ALICE))
    assert w.account_row("alice")["not_found_at"] is None


async def test_an_enterprise_account_is_keyed_by_its_node(w: Writer) -> None:
    await w.repository()
    enterprise = account(
        "ent", None, "EnterpriseUserAccount", node_id="EUA_1", linked_user_github_id=55
    )
    await w.item(pull_request(1, 10, author=enterprise))
    await w.item(pull_request(2, 20, author=enterprise))
    row = w.account_row("ent")
    assert (row["github_id"], row["node_id"], row["type"], row["linked_user_github_id"]) == (
        None,
        "EUA_1",
        "EnterpriseUserAccount",
        55,
    )
    assert w.pr(1)["author_id"] == w.pr(2)["author_id"] == row["id"]


async def test_a_second_node_id_for_a_stored_github_id_keeps_the_stored_row(w: Writer) -> None:
    await w.repository()
    await w.item(
        pull_request(1, 10, author=account("copilot", 175728472, "Bot", node_id="BOT_kgDOCnlnWA"))
    )
    other = account("copilot", 175728472, "Bot", node_id="MDM6Qm90MTc1NzI4NDcy")
    written = await w.item(pull_request(2, 20, author=other))
    assert written.second_node_ids == (
        SecondNodeId(175728472, "BOT_kgDOCnlnWA", "MDM6Qm90MTc1NzI4NDcy"),
    )
    assert w.rows("SELECT node_id FROM github_users") == [{"node_id": "BOT_kgDOCnlnWA"}]
    assert w.pr(2)["author_id"] == w.pr(1)["author_id"]


async def test_a_node_stored_under_another_github_id_fails_the_item(w: Writer) -> None:
    await w.repository()
    await w.item(pull_request(1, 10, author=account("alice", 101, node_id="N_X")))
    with pytest.raises(ItemConflict, match="node N_X is stored under github_id 101"):
        await w.item(pull_request(2, 20, author=account("mallory", 999, node_id="N_X")))
    assert w.rows("SELECT number FROM pull_requests") == [{"number": 1}]


async def test_a_second_node_id_another_account_holds_fails_the_item(w: Writer) -> None:
    """github_id 7 is stored under N_a; GitHub reports it under N_b, which 8 holds."""
    await w.repository()
    await w.item(
        pull_request(
            1,
            10,
            author=account("a", 7, node_id="N_a"),
            merged_by=account("b", 8, node_id="N_b"),
        )
    )
    before = w.rows("SELECT * FROM github_users ORDER BY id")
    with pytest.raises(
        ItemConflict, match="node N_b is stored under github_id 8, GitHub reports 7"
    ):
        await w.item(pull_request(2, 20, author=account("a", 7, node_id="N_b")))
    assert w.rows("SELECT number FROM pull_requests") == [{"number": 1}]
    assert w.rows("SELECT * FROM github_users ORDER BY id") == before


async def test_an_enterprise_sighting_of_a_node_stored_with_a_github_id_fails_the_item(
    w: Writer,
) -> None:
    await w.repository()
    await w.item(pull_request(1, 10, author=account("x", 7, node_id="EUA_1")))
    with pytest.raises(
        ItemConflict, match="node EUA_1 is stored as github_id 7, not as an EnterpriseUserAccount"
    ):
        await w.item(
            pull_request(
                2, 20, author=account("ent", None, "EnterpriseUserAccount", node_id="EUA_1")
            )
        )
    assert w.rows("SELECT number FROM pull_requests") == [{"number": 1}]
    assert w.rows("SELECT github_id, node_id, type FROM github_users") == [
        {"github_id": 7, "node_id": "EUA_1", "type": "User"}
    ]


async def test_d28_keeps_references_github_returns_as_null(w: Writer) -> None:
    """A null author, merger, reviewer, and comment author keep the stored reference with
    `*_reported` false; a null commit link keeps the stored author with linked_github_id null;
    an editor takes GitHub's value, null included."""
    await w.repository()
    first = pull_request(
        1,
        10,
        author=ALICE,
        merged_by=BOB,
        editor=BOB,
        state="MERGED",
        merged_at=T0,
        closed_at=T0,
        reviews=[review(900, BOB, editor=ALICE)],
        comments=[comment(800, ALICE, editor=BOB)],
        commits=[commit("s1", link(ALICE))],
    )
    await w.item(first)
    alice, bob = w.account_row("alice")["id"], w.account_row("bob")["id"]
    assert w.rows("SELECT editor_id FROM pr_reviews") == [{"editor_id": alice}]
    assert w.rows("SELECT editor_id FROM pr_comments") == [{"editor_id": bob}]
    again = pull_request(
        1,
        10,
        author=None,
        merged_by=None,
        editor=None,
        state="MERGED",
        merged_at=T0,
        closed_at=T0,
        reviews=[review(900, None, editor=None)],
        comments=[comment(800, None, editor=None)],
        commits=[commit("s1", None)],
        hydrated_at=HYDRATED + timedelta(days=1),
    )
    await w.item(again)
    row = w.pr(1)
    assert (
        row["author_id"],
        row["author_reported"],
        row["merged_by_id"],
        row["merged_by_reported"],
        row["editor_id"],
    ) == (
        alice,
        0,
        bob,
        0,
        None,
    )
    assert w.rows("SELECT reviewer_id, reviewer_reported, editor_id FROM pr_reviews") == [
        {"reviewer_id": bob, "reviewer_reported": 0, "editor_id": None}
    ]
    assert w.rows("SELECT author_id, author_reported, editor_id FROM pr_comments") == [
        {"author_id": alice, "author_reported": 0, "editor_id": None}
    ]
    assert w.rows("SELECT author_id, linked_github_id FROM pr_commits") == [
        {"author_id": alice, "linked_github_id": None}
    ]


async def test_d28_keeps_a_reference_only_from_the_same_review_comment_or_commit(
    w: Writer,
) -> None:
    """A null actor takes the stored one of the row with the same review or comment id, or a
    commit's of this PR's row with the same SHA, and no other (5.3 step 6)."""
    await w.repository()
    await w.item(
        pull_request(
            1,
            10,
            reviews=[review(900, BOB)],
            comments=[comment(800, ALICE)],
            commits=[commit("s1", link(ALICE))],
        )
    )
    other = await w.item(pull_request(2, 20, commits=[commit("s1", None)]))
    assert w.rows(
        "SELECT author_id, linked_github_id FROM pr_commits WHERE pr_id = ?", other.row_id
    ) == [{"author_id": None, "linked_github_id": None}]
    await w.item(
        pull_request(
            1,
            10,
            reviews=[review(901, None)],
            comments=[comment(801, None)],
            commits=[commit("s2", None)],
            hydrated_at=HYDRATED + timedelta(days=1),
        )
    )
    first = w.pr(1)["id"]
    assert w.rows("SELECT github_id, reviewer_id, reviewer_reported FROM pr_reviews") == [
        {"github_id": 901, "reviewer_id": None, "reviewer_reported": 0}
    ]
    assert w.rows("SELECT github_id, author_id, author_reported FROM pr_comments") == [
        {"github_id": 801, "author_id": None, "author_reported": 0}
    ]
    assert w.rows("SELECT sha, author_id FROM pr_commits WHERE pr_id = ?", first) == [
        {"sha": "s2", "author_id": None}
    ]


async def test_a_non_null_actor_replaces_the_stored_one(w: Writer) -> None:
    await w.repository()
    await w.item(
        pull_request(
            1, 10, author=ALICE, reviews=[review(900, ALICE)], commits=[commit("s1", link(ALICE))]
        )
    )
    await w.item(
        pull_request(
            1, 10, author=BOB, reviews=[review(900, BOB)], commits=[commit("s1", link(BOB))]
        )
    )
    bob = w.account_row("bob")["id"]
    assert (w.pr(1)["author_id"], w.pr(1)["author_reported"]) == (bob, 1)
    assert w.rows("SELECT reviewer_id, reviewer_reported FROM pr_reviews") == [
        {"reviewer_id": bob, "reviewer_reported": 1}
    ]
    assert w.rows("SELECT author_id, linked_github_id FROM pr_commits") == [
        {"author_id": bob, "linked_github_id": 102}
    ]


async def test_an_object_first_written_with_a_null_actor_has_no_reference(w: Writer) -> None:
    await w.repository()
    await w.item(
        pull_request(1, 10, author=None, reviews=[review(900, None)], comments=[comment(800, None)])
    )
    assert (w.pr(1)["author_id"], w.pr(1)["author_reported"]) == (None, 0)
    assert w.rows("SELECT reviewer_id, reviewer_reported FROM pr_reviews") == [
        {"reviewer_id": None, "reviewer_reported": 0}
    ]
    assert w.rows("SELECT author_id, author_reported FROM pr_comments") == [
        {"author_id": None, "author_reported": 0}
    ]


async def test_child_rows_are_replaced(w: Writer) -> None:
    await w.repository()
    await w.item(
        pull_request(
            1,
            10,
            labels=[("LA_1", "bug"), ("LA_2", "x")],
            files=[FileChange("a", "ADDED", 1, 0)],
            closing_issues=[closing_issue(5)],
            reviews=[review(900, ALICE), review(901, BOB)],
            comments=[comment(800, ALICE)],
            commits=[commit("s1", None), commit("s2", None)],
            review_requests=[request_for(ALICE), request_for_team("core")],
            assignees=[ALICE, BOB],
        )
    )
    await w.item(
        pull_request(
            1,
            10,
            labels=[("LA_2", "x")],
            reviews=[review(901, BOB)],
            commits=[commit("s2", None)],
            review_requests=[request_without_reviewer()],
            assignees=[DEPENDABOT],
        )
    )
    assert w.rows("SELECT name FROM pr_labels") == [{"name": "x"}]
    assert w.rows("SELECT count(*) AS n FROM pr_files") == [{"n": 0}]
    assert w.rows("SELECT count(*) AS n FROM pr_closing_issues") == [{"n": 0}]
    assert w.rows("SELECT github_id FROM pr_reviews") == [{"github_id": 901}]
    assert w.rows("SELECT count(*) AS n FROM pr_comments") == [{"n": 0}]
    assert w.rows("SELECT sha FROM pr_commits") == [{"sha": "s2"}]
    assert w.rows(
        "SELECT ordinal, requested_type, account_id, team_slug FROM pr_review_requests"
    ) == [{"ordinal": 0, "requested_type": None, "account_id": None, "team_slug": None}]
    assert w.rows("SELECT account_id FROM pr_assignees") == [
        {"account_id": w.account_row("dependabot")["id"]}
    ]
    assert w.pr(1)["reviews_total"] == 1


async def test_review_requests_and_assignees(w: Writer) -> None:
    await w.repository()
    await w.item(
        pull_request(
            1,
            10,
            review_requests=[
                request_for(ALICE, github_id=501, as_code_owner=True),
                request_for_team("core"),
                request_without_reviewer(),
            ],
            assignees=[ALICE, DEPENDABOT],
        )
    )
    alice, bot = w.account_row("alice")["id"], w.account_row("dependabot")["id"]
    assert w.rows(
        "SELECT ordinal, request_github_id, request_node_id, requested_type, requested_github_id, "
        "requested_node_id, account_id, team_slug, as_code_owner FROM pr_review_requests "
        "ORDER BY ordinal"
    ) == [
        {
            "ordinal": 0,
            "request_github_id": 501,
            "request_node_id": "RR_501",
            "requested_type": "User",
            "requested_github_id": 101,
            "requested_node_id": "N_alice",
            "account_id": alice,
            "team_slug": None,
            "as_code_owner": 1,
        },
        {
            "ordinal": 1,
            "request_github_id": 502,
            "request_node_id": "RR_502",
            "requested_type": "Team",
            "requested_github_id": 4001,
            "requested_node_id": "T_core",
            "account_id": None,
            "team_slug": "core",
            "as_code_owner": 1,
        },
        {
            "ordinal": 2,
            "request_github_id": 503,
            "request_node_id": "RR_503",
            "requested_type": None,
            "requested_github_id": None,
            "requested_node_id": None,
            "account_id": None,
            "team_slug": None,
            "as_code_owner": 0,
        },
    ]
    assert sorted(
        row["account_id"] for row in w.rows("SELECT account_id FROM pr_assignees")
    ) == sorted([alice, bot])
    assert w.account_row("dependabot")["type"] == "Bot"


async def test_a_pr_user_tags_row_survives_re_hydration_and_goes_with_its_pull_request(
    w: Writer,
) -> None:
    await w.repository()
    written = await w.item(pull_request(1, 10))
    with connect(w.database) as connection:
        connection.execute(
            text(
                "INSERT INTO user_tags (name, created_at) "
                "VALUES ('t', '2026-01-01 00:00:00.000000')"
            )
        )
        connection.execute(
            text("INSERT INTO pr_user_tags (pr_id, user_tag_id) VALUES (:pr, 1)"),
            {"pr": written.row_id},
        )
    again = await w.item(pull_request(1, 10, title="renamed"))
    assert again.row_id == written.row_id
    assert w.rows("SELECT pr_id FROM pr_user_tags") == [{"pr_id": written.row_id}]
    deletions = Deletions(
        repository_github_id=11,
        candidates=(Candidate("pull_request", written.row_id, 10, 1),),
        decision=GuardDecision(candidates=1, stored_rows=1, limit=25, approved_limit=None),
        failures_removed=(),
    )
    async with open_writer(_url(w.database)) as writer:
        assert await apply_item_deletions(writer, w.run_id, deletions) == 1
    assert w.rows("SELECT count(*) AS n FROM pr_user_tags") == [{"n": 0}]
    assert w.rows("SELECT count(*) AS n FROM user_tags") == [{"n": 1}]


async def test_a_successful_write_deletes_the_item_s_failure_row(w: Writer) -> None:
    await w.repository()
    with connect(w.database) as connection:
        repository_id = connection.execute(text("SELECT id FROM repositories")).scalar_one()
        seed = Seed(connection)
        seed.failure(repository_id, 1)
        seed.failure(repository_id, 2)
        seed.failure(repository_id, 1, kind="issue")
    await w.item(pull_request(1, 10))
    assert w.rows("SELECT item_kind, number FROM sync_failures ORDER BY number, item_kind") == [
        {"item_kind": "issue", "number": 1},
        {"item_kind": "pull_request", "number": 2},
    ]


# --------------------------------------------------------------------------------------------
# Refusals before anything is written
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["review", "comment"])
async def test_an_id_stored_under_another_pull_request_is_refused(w: Writer, kind: str) -> None:
    await w.repository()
    reviews = [review(900, ALICE)] if kind == "review" else []
    comments = [comment(900, ALICE)] if kind == "comment" else []
    await w.item(pull_request(1, 10, reviews=reviews, comments=comments))
    with pytest.raises(
        ItemConflict, match=rf"{kind} ids \[900\] are stored under another pull request"
    ):
        await w.item(pull_request(2, 20, author=BOB, reviews=reviews, comments=comments))
    assert w.rows("SELECT number FROM pull_requests") == [{"number": 1}]
    assert w.rows("SELECT count(*) AS n FROM github_users WHERE login = 'bob'") == [{"n": 0}]


async def test_a_number_stored_under_another_github_id_is_refused(w: Writer) -> None:
    await w.repository()
    await w.item(pull_request(1, 10))
    with pytest.raises(ItemConflict, match="the number is stored under github_id 10"):
        await w.item(pull_request(1, 11))


async def test_a_github_id_stored_as_another_pull_request_is_refused(w: Writer) -> None:
    await w.repository()
    await w.item(pull_request(1, 10))
    with pytest.raises(ItemConflict, match="github_id 10 is stored as another pull request"):
        await w.item(pull_request(2, 10))


# --------------------------------------------------------------------------------------------
# Commit links
# --------------------------------------------------------------------------------------------
async def test_a_link_neither_stored_nor_given_is_refused(w: Writer) -> None:
    await w.repository()
    with pytest.raises(AccountMissing) as refused:
        await w.item(pull_request(1, 10, author=BOB, commits=[commit("s1", link(ALICE))]))
    assert refused.value.github_ids == (101,)
    assert w.rows("SELECT count(*) AS n FROM pull_requests") == [{"n": 0}]
    assert w.rows("SELECT count(*) AS n FROM github_users") == [{"n": 0}]


async def test_a_link_sighted_in_another_record_of_the_batch(w: Writer) -> None:
    """The record that sighted the account is not written; the link's record inserts it from
    that sighting, with its Date (5.2)."""
    await w.repository()
    seen = HYDRATED - timedelta(minutes=1)
    await w.item(
        pull_request(1, 10, commits=[commit("s1", link(DEPENDABOT))]),
        {49699333: Sighted(DEPENDABOT, seen)},
    )
    row = w.account_row("dependabot")
    assert (row["type"], row["refreshed_at"]) == ("Bot", ts(seen))
    assert w.rows("SELECT author_id, linked_github_id FROM pr_commits") == [
        {"author_id": row["id"], "linked_github_id": 49699333}
    ]


async def test_a_link_given_with_a_lookup(w: Writer) -> None:
    await w.repository()
    looked_at = HYDRATED + timedelta(minutes=2)
    found = looked_up(
        ALICE,
        name="Alice",
        profile=UserProfile(company="Acme", location=None, website_url="https://a.example"),
    )
    await w.item(
        pull_request(1, 10, commits=[commit("s1", link(ALICE))]), {101: Found(found, looked_at)}
    )
    row = w.account_row("alice")
    assert (row["name"], row["refreshed_at"], row["created_at"]) == (
        "Alice",
        ts(looked_at),
        ts(found.created_at),
    )
    assert w.rows("SELECT company, location, website_url, refreshed_at FROM account_profiles") == [
        {
            "company": "Acme",
            "location": None,
            "website_url": "https://a.example",
            "refreshed_at": ts(looked_at),
        }
    ]


async def test_a_link_whose_lookup_answered_not_found(w: Writer) -> None:
    """Never stored: the commit has no account and keeps the id GitHub linked (D28)."""
    await w.repository()
    await w.item(
        pull_request(1, 10, commits=[commit("s1", link(ALICE))]), {101: NotFound(HYDRATED)}
    )
    assert w.rows("SELECT author_id, linked_github_id FROM pr_commits") == [
        {"author_id": None, "linked_github_id": 101}
    ]
    assert w.rows("SELECT count(*) AS n FROM github_users") == [{"n": 0}]


async def test_a_not_found_link_to_a_stored_account_keeps_it_and_marks_it(w: Writer) -> None:
    await w.repository()
    await w.item(pull_request(1, 10, author=ALICE))
    await w.item(
        pull_request(2, 20, commits=[commit("s1", link(ALICE))]),
        {101: NotFound(HYDRATED + timedelta(hours=1))},
    )
    alice = w.account_row("alice")
    assert alice["not_found_at"] == ts(HYDRATED + timedelta(hours=1))
    assert w.rows("SELECT author_id, linked_github_id FROM pr_commits") == [
        {"author_id": alice["id"], "linked_github_id": 101}
    ]
    await w.item(
        pull_request(3, 30, commits=[commit("s2", link(ALICE))]),
        {101: NotFound(HYDRATED + timedelta(hours=5))},
    )
    assert w.account_row("alice")["not_found_at"] == ts(HYDRATED + timedelta(hours=1))


@pytest.mark.parametrize("given", ["sighted", "found"])
async def test_a_link_given_for_another_account_is_refused(w: Writer, given: str) -> None:
    await w.repository()
    entry: LinkAccount = (
        Sighted(BOB, HYDRATED) if given == "sighted" else Found(looked_up(BOB), HYDRATED)
    )
    with pytest.raises(ItemConflict, match="commit link 101"):
        await w.item(pull_request(1, 10, commits=[commit("s1", link(ALICE))]), {101: entry})
    assert w.rows("SELECT count(*) AS n FROM pull_requests") == [{"n": 0}]


# --------------------------------------------------------------------------------------------
# Lookups and the refresh
# --------------------------------------------------------------------------------------------
async def _lookup(
    w: Writer, node_id: str, result: Found | NotFound, expected: int | None = None
) -> Any:
    async with open_writer(_url(w.database)) as writer:
        return await write_lookup(writer, w.run_id, node_id, result, expected_github_id=expected)


async def test_a_lookup_sets_login_type_and_the_profile(w: Writer) -> None:
    await w.repository()
    await w.item(pull_request(1, 10, author=account("old-login", 101, node_id="N_alice")))
    at = T0 + timedelta(days=2)
    profile = UserProfile(company="Acme", location="Paris", website_url=None)
    result = await _lookup(
        w, "N_alice", Found(looked_up(account("alice", 101), profile=profile), at)
    )
    assert w.rows("SELECT * FROM github_users") == [
        {
            "id": result.row_id,
            "github_id": 101,
            "node_id": "N_alice",
            "login": "alice",
            "type": "User",
            "name": "A Name",
            "created_at": ts(T0 - timedelta(days=2000)),
            "updated_at": ts(T0 - timedelta(days=10)),
            "linked_user_github_id": None,
            "claimant_github_id": None,
            "refreshed_at": ts(at),
            "not_found_at": None,
        }
    ]
    assert w.rows("SELECT * FROM account_profiles") == [
        {
            "account_id": result.row_id,
            "company": "Acme",
            "location": "Paris",
            "website_url": None,
            "description": None,
            "email": None,
            "twitter_username": None,
            "is_verified": None,
            "archived_at": None,
            "has_sponsors_listing": None,
            "pinned_items_remaining": None,
            "refreshed_at": ts(at),
        }
    ]
    changed = UserProfile(company=None, location="Paris", website_url=None)
    await _lookup(
        w,
        "N_alice",
        Found(looked_up(account("alice", 101), profile=changed), at + timedelta(days=2)),
    )
    assert w.rows("SELECT company, refreshed_at FROM account_profiles") == [
        {"company": None, "refreshed_at": ts(at + timedelta(days=2))}
    ]


async def test_an_organization_profile(w: Writer) -> None:
    org = account("prebid", 13242760, "Organization", node_id="ORG_1")
    profile = OrganizationProfile(
        description="d",
        email="e@example.com",
        location="Earth",
        website_url="https://prebid.org",
        twitter_username="prebidorg",
        is_verified=True,
        archived_at=T0 - timedelta(days=7),
        has_sponsors_listing=False,
        pinned_items_remaining=6,
    )
    result = await _lookup(w, "ORG_1", Found(looked_up(org, profile=profile), T0))
    assert w.rows("SELECT * FROM account_profiles") == [
        {
            "account_id": result.row_id,
            "company": None,
            "location": "Earth",
            "website_url": "https://prebid.org",
            "description": "d",
            "email": "e@example.com",
            "twitter_username": "prebidorg",
            "is_verified": 1,
            "archived_at": ts(T0 - timedelta(days=7)),
            "has_sponsors_listing": 0,
            "pinned_items_remaining": 6,
            "refreshed_at": ts(T0),
        }
    ]


@pytest.mark.parametrize(
    ("who", "claimant", "github_id", "linked"),
    [
        (account("mann", 6, "Mannequin", node_id="MAN_1"), 4242, 6, None),
        (
            account(
                "ent", None, "EnterpriseUserAccount", node_id="EUA_9", linked_user_github_id=55
            ),
            None,
            None,
            55,
        ),
    ],
    ids=["mannequin-claimant", "enterprise-linked-user"],
)
async def test_a_lookup_stores_the_claimant_and_the_linked_user(
    w: Writer, who: Any, claimant: int | None, github_id: int | None, linked: int | None
) -> None:
    result = await _lookup(
        w, who.node_id, Found(looked_up(who, name=None, claimant_github_id=claimant), T0)
    )
    assert w.rows("SELECT * FROM github_users") == [
        {
            "id": result.row_id,
            "github_id": github_id,
            "node_id": who.node_id,
            "login": who.login,
            "type": who.type,
            "name": None,
            "created_at": ts(T0 - timedelta(days=2000)),
            "updated_at": ts(T0 - timedelta(days=10)),
            "linked_user_github_id": linked,
            "claimant_github_id": claimant,
            "refreshed_at": ts(T0),
            "not_found_at": None,
        }
    ]


async def test_a_lookup_under_a_second_node_id_keeps_the_stored_row(w: Writer) -> None:
    """5.2: the stored row keeps its node id, the lookup's values apply, and the second node id
    is reported."""
    first = await _lookup(w, "N_old", Found(looked_up(account("x", 7, node_id="N_old")), T0))
    at = T0 + timedelta(days=1)
    second = await _lookup(
        w, "N_new", Found(looked_up(account("x-renamed", 7, node_id="N_new")), at)
    )
    assert (second.row_id, second.inserted) == (first.row_id, False)
    assert second.second_node_ids == (SecondNodeId(7, "N_old", "N_new"),)
    assert w.rows("SELECT id, node_id, login, refreshed_at FROM github_users") == [
        {"id": first.row_id, "node_id": "N_old", "login": "x-renamed", "refreshed_at": ts(at)}
    ]


@pytest.mark.parametrize(
    "who",
    [
        account("bot", 5, "Bot"),
        account("mann", 6, "Mannequin"),
        account("ent", None, "EnterpriseUserAccount", node_id="EUA_9"),
    ],
    ids=["bot", "mannequin", "enterprise"],
)
async def test_no_profile_row_for_other_types(w: Writer, who: Any) -> None:
    await _lookup(w, who.node_id, Found(looked_up(who, profile=None), T0))
    assert w.rows("SELECT count(*) AS n FROM account_profiles") == [{"n": 0}]
    assert w.rows("SELECT type FROM github_users") == [{"type": who.type}]


async def test_a_type_change_drops_the_profile(w: Writer) -> None:
    user = account("x", 7, node_id="N_x")
    await _lookup(w, "N_x", Found(looked_up(user, profile=UserProfile("c", None, None)), T0))
    await _lookup(
        w,
        "N_x",
        Found(
            looked_up(account("x", 7, "Bot", node_id="N_x"), profile=None), T0 + timedelta(days=1)
        ),
    )
    assert w.rows("SELECT count(*) AS n FROM account_profiles") == [{"n": 0}]
    assert w.account_row("x")["type"] == "Bot"


async def test_a_not_found_lookup_keeps_the_account_and_its_profile(w: Writer) -> None:
    user = account("x", 7, node_id="N_x")
    await _lookup(
        w, "N_x", Found(looked_up(user, name="X", profile=UserProfile("c", None, None)), T0)
    )
    before = w.account_row("x")
    await _lookup(w, "N_x", NotFound(T0 + timedelta(days=3)))
    after = w.account_row("x")
    assert after == {**before, "not_found_at": ts(T0 + timedelta(days=3))}
    await _lookup(w, "N_x", NotFound(T0 + timedelta(days=3, hours=12)))
    assert w.account_row("x") == after
    assert w.rows("SELECT company FROM account_profiles") == [{"company": "c"}]
    seen_again = await _lookup(
        w, "N_x", Found(looked_up(user, name="X", profile=None), T0 + timedelta(days=4))
    )
    assert seen_again.row_id == before["id"]
    assert w.account_row("x")["not_found_at"] is None


async def test_a_not_found_lookup_of_an_account_never_stored_writes_nothing(w: Writer) -> None:
    result = await _lookup(w, "N_nobody", NotFound(T0))
    assert (result.row_id, result.inserted) == (None, False)
    assert w.rows("SELECT count(*) AS n FROM github_users") == [{"n": 0}]


@pytest.mark.parametrize(
    ("node_id", "answer", "expected", "message"),
    [
        ("N_x", account("x", 8, node_id="N_x"), 7, "returned github_id 8, expected 7"),
        ("N_x", account("y", 7, node_id="N_y"), None, "answered for node N_y"),
        ("N_z", account("z", 9, node_id="N_z"), None, "node N_z is stored under github_id 7"),
    ],
    ids=["another-database-id", "another-node", "a-node-stored-under-another-id"],
)
async def test_a_lookup_that_disagrees_with_the_database_is_refused(
    w: Writer, node_id: str, answer: Any, expected: int | None, message: str
) -> None:
    await _lookup(w, "N_x", Found(looked_up(account("x", 7, node_id="N_x")), T0))
    if node_id == "N_z":
        with connect(w.database) as connection:
            connection.execute(text("UPDATE github_users SET node_id = 'N_z'"))
    before = w.rows("SELECT * FROM github_users")
    with pytest.raises(LookupConflict, match=message):
        await _lookup(w, node_id, Found(looked_up(answer), T0 + timedelta(days=1)), expected)
    assert w.rows("SELECT * FROM github_users") == before


async def test_accounts_due_for_the_refresh(w: Writer) -> None:
    now = T0 + timedelta(days=10)
    with connect(w.database) as connection:
        seed = Seed(connection)
        seed.user(
            "due",
            github_id=1,
            node_id="N1",
            refreshed_at=now - REFRESH_AFTER - timedelta(microseconds=1),
        )
        seed.user(
            "older", github_id=2, node_id="N2", refreshed_at=now - REFRESH_AFTER - timedelta(days=3)
        )
        seed.user("at-24h", github_id=3, node_id="N3", refreshed_at=now - REFRESH_AFTER)
        seed.user("fresh", github_id=4, node_id="N4", refreshed_at=now)
        seed.user(
            "missing",
            github_id=5,
            node_id="N5",
            refreshed_at=now - timedelta(days=5),
            not_found_at=now,
        )
    async with open_writer(_url(w.database)) as writer:
        due = await accounts_to_refresh(writer, now=now)
    assert [target.node_id for target in due] == ["N2", "N1"]


async def test_stored_accounts_by_github_id(w: Writer) -> None:
    with connect(w.database) as connection:
        seed = Seed(connection)
        seed.user("a", github_id=1, node_id="N1")
        seed.user("b", github_id=2, node_id="N2", not_found_at=T0)
    async with open_writer(_url(w.database)) as writer:
        stored = await stored_accounts(writer, [2, 1, 3])
    assert {github_id: (row.node_id, row.not_found_at) for github_id, row in stored.items()} == {
        1: ("N1", None),
        2: ("N2", T0),
    }


# --------------------------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------------------------
async def test_attempts_count_runs_and_the_first_failure_stays(w: Writer) -> None:
    await w.repository()
    run_started = T0

    async def fail(at: Any, started: Any, message: str) -> None:
        async with open_writer(_url(w.database)) as writer:
            await record_failure(
                writer,
                w.run_id,
                repository_github_id=11,
                number=5,
                error=ValueError(message),
                failed_at=at,
                run_started_at=started,
            )

    await fail(T0 + timedelta(minutes=1), run_started, "first")
    await fail(T0 + timedelta(minutes=2), run_started, "same run")
    assert w.rows(
        "SELECT attempts, last_error, first_failed_at, last_failed_at FROM sync_failures"
    ) == [
        {
            "attempts": 1,
            "last_error": "ValueError: same run",
            "first_failed_at": ts(T0 + timedelta(minutes=1)),
            "last_failed_at": ts(T0 + timedelta(minutes=2)),
        }
    ]
    next_run = T0 + timedelta(hours=6)
    await fail(next_run + timedelta(minutes=1), next_run, "next run")
    assert w.rows("SELECT attempts, last_error, first_failed_at FROM sync_failures") == [
        {
            "attempts": 2,
            "last_error": "ValueError: next run",
            "first_failed_at": ts(T0 + timedelta(minutes=1)),
        }
    ]


# --------------------------------------------------------------------------------------------
# Step 7 and the prune
# --------------------------------------------------------------------------------------------
async def _two_items_and_failures(w: Writer) -> tuple[int, int]:
    await w.repository()
    first = await w.item(pull_request(1, 10, reviews=[review(900, ALICE)]))
    second = await w.item(pull_request(2, 20))
    with connect(w.database) as connection:
        repository_id = connection.execute(text("SELECT id FROM repositories")).scalar_one()
        Seed(connection).failure(repository_id, 8)
        Seed(connection).failure(repository_id, 9)
    return first.row_id, second.row_id


def _deletions(
    rows: tuple[int, ...], github_ids: tuple[int, ...], *, runs: bool, approved: int | None = None
) -> Deletions:
    candidates = tuple(
        Candidate("pull_request", row, gid, row) for row, gid in zip(rows, github_ids, strict=True)
    )
    limit = len(candidates) if runs else len(candidates) - 1
    return Deletions(
        repository_github_id=11,
        candidates=candidates,
        decision=GuardDecision(
            candidates=len(candidates), stored_rows=100, limit=limit, approved_limit=approved
        ),
        failures_removed=(("pull_request", 8),),
        approval=None if approved is None else DeletionApproval(TRACKED_JS, approved),
    )


async def test_step_7_deletes_the_candidates_and_their_children(w: Writer) -> None:
    first, _ = await _two_items_and_failures(w)
    async with open_writer(_url(w.database)) as writer:
        deleted = await apply_item_deletions(
            writer, w.run_id, _deletions((first,), (10,), runs=True)
        )
    assert deleted == 1
    assert w.rows("SELECT number FROM pull_requests") == [{"number": 2}]
    assert w.rows("SELECT count(*) AS n FROM pr_reviews") == [{"n": 0}]
    assert w.rows("SELECT number FROM sync_failures") == [{"number": 9}]
    assert w.rows("SELECT items_deleted, deletions_approved FROM sync_runs") == [
        {"items_deleted": 1, "deletions_approved": None}
    ]
    assert w.rows("SELECT count(*) AS n FROM github_users") == [{"n": 1}]


async def test_a_halt_deletes_no_item_but_removes_the_failure_rows(w: Writer) -> None:
    first, _ = await _two_items_and_failures(w)
    async with open_writer(_url(w.database)) as writer:
        deleted = await apply_item_deletions(
            writer, w.run_id, _deletions((first,), (10,), runs=False)
        )
    assert deleted == 0
    assert w.rows("SELECT number FROM pull_requests ORDER BY number") == [
        {"number": 1},
        {"number": 2},
    ]
    assert w.rows("SELECT number FROM sync_failures") == [{"number": 9}]
    assert w.rows("SELECT items_deleted FROM sync_runs") == [{"items_deleted": 0}]


@pytest.mark.parametrize("runs", [True, False], ids=["deleted", "halted"])
async def test_an_approval_is_recorded_with_the_deletion(w: Writer, runs: bool) -> None:
    """Two candidates over the guard: N = 2 lets them through, N = 1 halts; either way the
    approval is recorded in the deletion's own transaction."""
    first, second = await _two_items_and_failures(w)
    limit = 2 if runs else 1
    deletions = _deletions((first, second), (10, 20), runs=False, approved=limit)
    assert deletions.decision.runs is runs
    async with open_writer(_url(w.database)) as writer:
        deleted = await apply_item_deletions(writer, w.run_id, deletions)
    assert deleted == (2 if runs else 0)
    (row,) = w.rows("SELECT deletions_approved FROM sync_runs")
    assert row["deletions_approved"] == (
        f'{{"allow_deletions":{{"approved":{limit},"github_id":11,"repository":"prebid/Prebid.js",'
        f'"sets":{{"items":{{"candidates":2,"deleted":{"true" if runs else "false"}}}}}}}}}'
    )


async def test_one_run_records_its_approval_and_every_prune(w: Writer) -> None:
    """deletions_approved gathers the approval and each prune in the order they ran, and
    items_deleted adds up across their transactions (5.1)."""
    first, second = await _two_items_and_failures(w)
    await w.repository(repository(github_id=12, full_name="prebid/gone-a", node_id="R_12"))
    await w.repository(repository(github_id=13, full_name="prebid/gone-b", node_id="R_13"))
    await w.item(pull_request(1, 110, repository="prebid/gone-a"), repo=12)
    await w.item(pull_request(1, 130, repository="prebid/gone-b"), repo=13)
    await w.item(pull_request(2, 131, repository="prebid/gone-b"), repo=13)
    async with open_writer(_url(w.database)) as writer:
        await mark_untracked(writer, w.run_id, [12, 13])
        assert await prune_repositories(writer, w.run_id, [13]) == (Pruned(13, "prebid/gone-b", 2),)
        deleted = await apply_item_deletions(
            writer,
            w.run_id,
            _deletions((first, second), (10, 20), runs=False, approved=2),
        )
        assert deleted == 2
        assert await prune_repositories(writer, w.run_id, [12]) == (Pruned(12, "prebid/gone-a", 1),)
    assert w.rows("SELECT items_deleted, deletions_approved FROM sync_runs") == [
        {
            "items_deleted": 5,
            "deletions_approved": (
                '{"allow_deletions":{"approved":2,"github_id":11,"repository":"prebid/Prebid.js",'
                '"sets":{"items":{"candidates":2,"deleted":true}}},'
                '"prune_untracked":[{"full_name":"prebid/gone-b","github_id":13,"items_deleted":2},'
                '{"full_name":"prebid/gone-a","github_id":12,"items_deleted":1}]}'
            ),
        }
    ]


async def test_the_prune_deletes_untracked_repositories_and_records_them(w: Writer) -> None:
    await w.repository(repository(github_id=11, full_name="prebid/Prebid.js"))
    await w.repository(repository(github_id=12, full_name="prebid/gone", node_id="R_12"))
    await w.item(pull_request(1, 10, author=ALICE))
    await w.item(pull_request(1, 110, repository="prebid/gone", author=ALICE), repo=12)
    await w.item(pull_request(2, 120, repository="prebid/gone"), repo=12)
    async with open_writer(_url(w.database)) as writer:
        await mark_untracked(writer, w.run_id, [12])
        pruned = await prune_repositories(writer, w.run_id, [12])
    assert pruned == (Pruned(12, "prebid/gone", 2),)
    assert w.rows("SELECT github_id FROM repositories") == [{"github_id": 11}]
    assert w.rows("SELECT github_id FROM pull_requests") == [{"github_id": 10}]
    assert w.rows("SELECT count(*) AS n FROM github_users") == [{"n": 1}]
    assert w.rows("SELECT items_deleted, deletions_approved FROM sync_runs") == [
        {
            "items_deleted": 2,
            "deletions_approved": (
                '{"prune_untracked":[{"full_name":"prebid/gone","github_id":12,"items_deleted":2}]}'
            ),
        }
    ]


@pytest.mark.parametrize("github_id", [11, 99], ids=["tracked", "not-stored"])
async def test_the_prune_refuses_a_repository_not_stored_untracked(
    w: Writer, github_id: int
) -> None:
    await w.repository()
    with pytest.raises(ValueError, match="prune refused"):
        async with open_writer(_url(w.database)) as writer:
            await prune_repositories(writer, w.run_id, [github_id])
    assert w.rows("SELECT count(*) AS n FROM repositories") == [{"n": 1}]


# --------------------------------------------------------------------------------------------
# A recorded hydration
# --------------------------------------------------------------------------------------------
async def test_a_recorded_pull_request_is_written_with_every_child_row(w: Writer) -> None:
    """Prebid.js#13481 through GitHubSource over its recordings: each child table holds the
    record's entries and each total equals its rows."""
    from github_activity_db.source import GitHubSource, Hydrated
    from tests.source.conftest import FakeClock
    from tests.source.replay import Replay

    replay = Replay.from_fixtures()
    async with GitHubSource(
        "test-token", http_transport=replay.transport(), clock=FakeClock()
    ) as source:
        resolved = await source.resolve("prebid", "Prebid.js")
        outcomes = [
            outcome
            async for batch in source.hydrate_pull_requests(resolved, [13481])
            for outcome in batch
        ]
    assert replay.problems == []
    (outcome,) = outcomes
    assert isinstance(outcome, Hydrated)
    record = outcome.record
    async with open_writer(_url(w.database)) as writer:
        await write_repository(writer, w.run_id, resolved, group="Client")
        sighted = {seen.github_id for seen in record.accounts()}
        links: dict[int, LinkAccount] = {
            commit_link.github_id: Sighted(
                account(commit_link.login, commit_link.github_id, node_id=commit_link.node_id),
                record.hydrated_at,
            )
            for commit_link in record.commit_links()
            if commit_link.github_id not in sighted
        }
        await write_pull_request(
            writer, w.run_id, record, repository_github_id=resolved.github_id, links=links
        )
    (row,) = w.rows("SELECT * FROM pull_requests")
    counts = {
        table: w.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]
        for table in (
            "pr_labels",
            "pr_reviews",
            "pr_comments",
            "pr_commits",
            "pr_files",
            "pr_closing_issues",
            "pr_review_requests",
            "pr_assignees",
        )
    }
    totals = record.totals
    assert counts == {
        "pr_labels": totals.labels,
        "pr_reviews": totals.reviews,
        "pr_comments": totals.comments,
        "pr_commits": totals.commits,
        "pr_files": totals.files,
        "pr_closing_issues": totals.closing_issues,
        "pr_review_requests": totals.review_requests,
        "pr_assignees": totals.assignees,
    }
    assert {column: row[column] for column in row if column.endswith("_total")} == {
        "labels_total": totals.labels,
        "closing_issues_total": totals.closing_issues,
        "user_linked_closing_issues_total": totals.user_linked_closing_issues,
        "reviews_total": totals.reviews,
        "comments_total": totals.comments,
        "commits_total": totals.commits,
        "files_total": totals.files,
        "review_requests_total": totals.review_requests,
        "assignees_total": totals.assignees,
    }
    assert w.rows("SELECT count(*) AS n FROM pr_closing_issues WHERE user_linked") == [
        {"n": totals.user_linked_closing_issues}
    ]
    assert sum(counts.values()) > 0
    assert w.rows("SELECT sha FROM pr_commits ORDER BY sha") == [
        {"sha": c.sha} for c in sorted(record.commits, key=lambda c: c.sha)
    ]


async def test_every_item_write_runs_on_the_writer_s_one_connection(
    w: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted at the writer engine's connect hook and at the one every application engine
    registers (db.engine), so a write that opened any other engine would show."""
    from github_activity_db.db import engine as engine_module
    from github_activity_db.sync import connection as connection_module

    await w.repository()
    opened: list[object] = []
    connected: list[object] = []
    original = connection_module._writer_connect
    original_pragmas = engine_module._apply_sqlite_pragmas

    def counting(dbapi_connection: object, record: object, busy_timeout_ms: int | None) -> None:
        opened.append(dbapi_connection)
        original(dbapi_connection, record, busy_timeout_ms)

    def counting_pragmas(dbapi_connection: object, record: object) -> None:
        connected.append(dbapi_connection)
        original_pragmas(dbapi_connection, record)

    monkeypatch.setattr(connection_module, "_writer_connect", counting)
    monkeypatch.setattr(engine_module, "_apply_sqlite_pragmas", counting_pragmas)
    async with open_writer(_url(w.database)) as writer:
        for number in range(1, 6):
            await write_pull_request(
                writer,
                w.run_id,
                pull_request(
                    number, number * 10, author=ALICE, reviews=[review(900 + number, BOB)]
                ),
                repository_github_id=11,
                links={},
            )
        await write_lookup(writer, w.run_id, "N_bob", Found(looked_up(BOB), HYDRATED))
        await record_failure(
            writer,
            w.run_id,
            repository_github_id=11,
            number=9,
            error=ValueError("x"),
            failed_at=T0,
            run_started_at=T0,
        )
    assert len(opened) == 1
    assert connected == opened
    assert w.rows("SELECT count(*) AS n FROM pull_requests") == [{"n": 5}]
