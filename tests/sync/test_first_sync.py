"""A first sync of go-gdpr from the recordings made for it, without the scenario model (redesign
plan Step 3b-2a-2c).

``scripts/record_fixtures.py --only first-sync`` ran the engine's first sync of the repository
against live GitHub and kept every unit it asked for under ``tests/fixtures/source/first_sync``.
Every connection of every recorded pull request fits its first page, so each stored item's child
rows can be checked against the recorded node lists themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from github_activity_db.sync.engine import SyncOptions, sync
from github_activity_db.tracking import TrackingConfigFile
from tests.source.conftest import FakeClock, opened_over
from tests.source.recorder import FIRST_SYNC_REPOSITORY, first_sync_config
from tests.source.recordings import FIRST_SYNC
from tests.source.replay import Replay, unanswered
from tests.sync.records import fetch

# Each stored total and the recorded connection it counts.
TOTALS = {
    "reviews_total": "reviews",
    "comments_total": "comments",
    "commits_total": "commits",
    "files_total": "files",
    "labels_total": "labels",
    "closing_issues_total": "closingIssuesReferences",
    "user_linked_closing_issues_total": "userLinkedClosingIssues",
    "review_requests_total": "reviewRequests",
    "assignees_total": "assignedActors",
}


def recorded_pull_requests(replay: Replay) -> dict[int, dict[str, Any]]:
    units = [unit for unit in replay.units.values() if unit.kind == "pull_request"]
    return {unit.data["number"]: unit.data for unit in units}


def children(database: Path, pr_id: int) -> dict[str, Any]:
    """The stored child rows of one pull request, by the keys GitHub gives them."""

    def column(sql: str) -> list[Any]:
        return [next(iter(row.values())) for row in fetch(database, sql, pr_id)]

    return {
        "reviews": sorted(column("SELECT github_id FROM pr_reviews WHERE pr_id = ?")),
        "comments": sorted(column("SELECT github_id FROM pr_comments WHERE pr_id = ?")),
        "commits": column("SELECT sha FROM pr_commits WHERE pr_id = ? ORDER BY rowid"),
        "files": column("SELECT path FROM pr_files WHERE pr_id = ? ORDER BY ordinal"),
        "labels": sorted(column("SELECT label_node_id FROM pr_labels WHERE pr_id = ?")),
        "closing": sorted(
            (row["issue_repo_github_id"], row["issue_number"], row["user_linked"])
            for row in fetch(
                database,
                "SELECT issue_repo_github_id, issue_number, user_linked FROM pr_closing_issues "
                "WHERE pr_id = ?",
                pr_id,
            )
        ),
        "requests": column(
            "SELECT request_node_id FROM pr_review_requests WHERE pr_id = ? ORDER BY ordinal"
        ),
        "assignees": sorted(
            column(
                "SELECT u.node_id FROM pr_assignees a JOIN github_users u ON u.id = a.account_id "
                "WHERE a.pr_id = ?"
            )
        ),
    }


def recorded_children(data: dict[str, Any]) -> dict[str, Any]:
    """The same keys, read from the recorded response."""

    def nodes(connection: str) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = data[connection]["nodes"]
        return listed

    user_linked = {
        (node["repository"]["databaseId"], node["number"])
        for node in nodes("userLinkedClosingIssues")
    }
    return {
        "reviews": sorted(int(node["fullDatabaseId"]) for node in nodes("reviews")),
        "comments": sorted(int(node["fullDatabaseId"]) for node in nodes("comments")),
        "commits": [node["commit"]["oid"] for node in nodes("commits")],
        "files": [node["path"] for node in nodes("files")],
        "labels": sorted(node["id"] for node in nodes("labels")),
        "closing": sorted(
            (
                node["repository"]["databaseId"],
                node["number"],
                int((node["repository"]["databaseId"], node["number"]) in user_linked),
            )
            for node in nodes("closingIssuesReferences")
        ),
        "requests": [node["id"] for node in nodes("reviewRequests")],
        "assignees": sorted(node["id"] for node in nodes("assignedActors")),
    }


async def test_a_first_sync_of_go_gdpr_from_its_recordings(migrated_database: Path) -> None:
    replay = Replay.from_directory(FIRST_SYNC)
    recorded = recorded_pull_requests(replay)
    for data in recorded.values():
        for connection in TOTALS.values():
            # The premise: each connection fits its first page.
            assert len(data[connection]["nodes"]) == data[connection]["totalCount"]
    clock = FakeClock()
    owner, name = FIRST_SYNC_REPOSITORY

    report = await sync(
        SyncOptions(
            database_url=f"sqlite+aiosqlite:///{migrated_database}",
            config=TrackingConfigFile(first_sync_config(owner, name), "test", "0" * 64),
            trigger="manual",
            code_version="test",
        ),
        open_source=opened_over(replay, clock),
        clock=clock,
    )

    assert (report.status, report.exit_code, report.error, report.failures) == (
        "complete",
        0,
        None,
        (),
    )
    assert unanswered(replay) == []
    (run,) = fetch(migrated_database, "SELECT status, exit_code FROM sync_runs")
    assert run == {"status": "complete", "exit_code": 0}
    rows = fetch(
        migrated_database,
        f"SELECT id, number, github_id, state, {', '.join(TOTALS)} FROM pull_requests "
        "ORDER BY number",
    )
    assert [row["number"] for row in rows] == sorted(recorded)
    for row in rows:
        data = recorded[row["number"]]
        assert (row["github_id"], row["state"]) == (int(data["fullDatabaseId"]), data["state"])
        assert {column: row[column] for column in TOTALS} == {
            column: data[connection]["totalCount"] for column, connection in TOTALS.items()
        }, row["number"]
        assert children(migrated_database, row["id"]) == recorded_children(data), row["number"]
