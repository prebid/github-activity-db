"""The sync engine over the scenario model and a migrated database (plan 5.2, 5.3).

Each case runs the engine and checks the stored rows, the ``sync_runs`` row, the
``repo_sync_state`` rows the run wrote, and the exit code. The planner's and the writer's own
rules have their own tests; these show the engine joins them.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from github_activity_db.source import PULL_REQUEST_HYDRATION_VERSION
from github_activity_db.sync import writer
from github_activity_db.sync.deletions import DeletionApproval, parse_allow_deletions
from github_activity_db.sync.plan import Reason
from github_activity_db.sync.runs import ClaimRefused, HeadCheckError
from tests.source.replay import synthetic
from tests.source.scenario import Scenario
from tests.sync.engine_support import World, ended, outcomes, planned, project


@pytest.fixture
def world(scenario: Scenario, migrated_database: Path) -> Iterator[World]:
    """The engine never leaves an ``AccountMissing`` failure: it gives the writer every account a
    commit links (plan 5.2)."""
    world = World(scenario, migrated_database)
    yield world
    if not world.expects_account_missing:
        assert world.rows("SELECT number FROM sync_failures WHERE last_error LIKE 'Account%'") == []


def batches(scenario: Scenario) -> list[list[int]]:
    """The numbers of each batch request, in the order sent."""
    return [seen.numbers for seen in scenario.replay.seen if seen.operation == "PullRequestBatch"]


def operations(scenario: Scenario) -> list[str]:
    return [seen.operation for seen in scenario.replay.seen]


# --------------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------------
async def test_a_first_sync_stores_every_pull_request_and_its_relations(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=2)
    reviewer = scenario.add_account()
    pull = repository.pulls[1]
    pull.add("reviews", scenario.node("reviews", by=reviewer))
    pull.add("comments", scenario.node("comments", by=author))
    pull.add("commits", scenario.node("commits", by=author))

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.numbers(repository) == [1, 2]
    assert planned(report) == [1, 2]
    assert {item.reasons for item in report.planned} == {frozenset({Reason.NOT_STORED})}
    run = world.run(report.run_id)
    assert (run["items_planned"], run["items_hydrated"], run["failures"]) == (2, 2, 0)
    row = world.rows(
        "SELECT state, hydrated_at, hydration_version, reviews_total FROM pull_requests "
        "WHERE number = 1"
    )
    assert row == [
        {
            "state": "OPEN",
            "hydrated_at": "2026-10-17 12:11:00.000000",
            "hydration_version": PULL_REQUEST_HYDRATION_VERSION,
            "reviews_total": 1,
        }
    ]
    assert world.rows("SELECT pr_number, reviewer_login, is_pr_author FROM v_pr_reviews") == [
        {
            "pr_number": 1,
            "reviewer_login": scenario.account(reviewer)["login"],
            "is_pr_author": 0,
        }
    ]
    assert world.rows("SELECT pr_number, author_login, is_pr_author FROM v_pr_comments") == [
        {"pr_number": 1, "author_login": scenario.account(author)["login"], "is_pr_author": 1}
    ]
    assert world.rows("SELECT pr_number, account_login FROM v_commit_authors") == [
        {"pr_number": 1, "account_login": scenario.account(author)["login"]}
    ]
    state = world.state(repository)
    assert state is not None
    assert state["last_success_at"] == run["finished_at"]
    assert state["index_pr_total"] == 2


async def test_an_update_hydrates_only_the_changed_pull_request(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=2)
    await world.sync()
    repository.pulls[2].add("comments", scenario.node("comments", by=author))

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert [(item.number, item.reasons) for item in report.planned] == [
        (2, frozenset({Reason.UPDATED_AT}))
    ]
    assert world.rows(
        "SELECT p.number FROM pr_comments c JOIN pull_requests p ON p.id = c.pr_id"
    ) == [{"number": 2}]


@pytest.mark.parametrize("change", ["merge", "close", "reopen"])
async def test_a_state_change_is_stored(world: World, change: str) -> None:
    repository, author = project(world)
    pull = repository.pulls[1]
    if change == "reopen":
        pull.close()
    await world.sync()
    if change == "merge":
        pull.merge(by=author)
    elif change == "close":
        pull.close()
    else:
        pull.reopen()

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert planned(report) == [1]
    expected = {"merge": "MERGED", "close": "CLOSED", "reopen": "OPEN"}[change]
    (row,) = world.rows(
        "SELECT state, merged_at IS NOT NULL AS merged, closed_at FROM pull_requests"
    )
    assert (row["state"], row["merged"], row["closed_at"] is None) == (
        expected,
        change == "merge",
        change == "reopen",
    )


async def test_a_pull_request_closed_after_the_index_is_stored_closed(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    scenario.after("PullRequestIndex", repository.pulls[1].close)

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.rows("SELECT state FROM pull_requests") == [{"state": "CLOSED"}]


async def test_a_renamed_repository_is_followed_under_its_configured_name(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    await world.sync()
    row_id = world.value("SELECT id FROM repositories")
    scenario.rename_repository(repository, "Prebid2.js")

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid2.js": "complete"})
    assert world.rows("SELECT id, full_name, name, is_tracked FROM repositories") == [
        {"id": row_id, "full_name": "prebid/Prebid2.js", "name": "Prebid2.js", "is_tracked": 1}
    ]
    assert report.repositories[0].stored_full_name == "prebid/Prebid.js"
    assert report.repositories[0].full_name == "prebid/Prebid2.js"
    assert world.numbers(repository) == [1]


async def test_a_renamed_label_keeps_its_id_and_rehydrates_its_items(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    label = scenario.add_label(repository, "bug")
    repository.pulls[1].add("labels", label)
    await world.sync()
    scenario.rename_label(repository, label["id"], "defect")

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert [(item.number, item.reasons) for item in report.planned] == [
        (1, frozenset({Reason.LABEL_GONE}))
    ]
    assert world.rows("SELECT label_node_id, name FROM pr_labels") == [
        {"label_node_id": label["id"], "name": "defect"}
    ]
    assert world.rows("SELECT node_id, name FROM repo_labels") == [
        {"node_id": label["id"], "name": "defect"}
    ]


async def test_a_hydration_version_bump_rehydrates_every_item(world: World) -> None:
    project(world, pulls=2)
    await world.sync()
    world.execute("UPDATE pull_requests SET hydration_version = 1")

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert {item.reasons for item in report.planned} == {frozenset({Reason.HYDRATION_VERSION})}
    assert planned(report) == [1, 2]
    assert world.rows("SELECT DISTINCT hydration_version FROM pull_requests") == [
        {"hydration_version": PULL_REQUEST_HYDRATION_VERSION}
    ]


async def test_an_unsettled_item_is_hydrated_again_until_it_settles(world: World) -> None:
    repository, _ = project(world)
    await world.sync(settle=False)

    again = await world.sync(settle=False)
    settled = await world.sync()
    after = await world.sync()

    assert [(item.number, item.reasons) for item in again.planned] == [
        (1, frozenset({Reason.UNSETTLED}))
    ]
    # Hydrated within the window again (the clock did not move), so planned again.
    assert planned(settled) == [1]
    assert planned(after) == []
    assert ended(world, after) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.numbers(repository) == [1]


async def test_the_queue_puts_pending_then_open_then_newest_first(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=5)
    pulls = repository.pulls
    pulls[1].merge(by=author)
    pulls[4].close()
    pulls[5].merge(by=author)
    await world.sync()
    world.execute(
        "INSERT INTO sync_pending (repository_id, item_kind, number, run_id) "
        "SELECT id, 'pull_request', 5, 1 FROM repositories"
    )
    now = scenario.clock.now()
    for number, minutes in ((1, 50), (2, 40), (3, 30), (4, 20), (5, 10)):
        pulls[number].touch(at=now - timedelta(minutes=minutes))

    report = await world.sync()

    # #5 pending; then the open #3 (newer) and #2; then the closed #4 and the merged #1.
    assert planned(report) == [5, 3, 2, 4, 1]
    assert batches(scenario)[-1] == [5, 3, 2, 4, 1]
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.pending(repository) == []


async def test_ready_for_review_and_association_come_from_the_index(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=2)
    repository.pulls[1].set(isDraft=True)
    repository.pulls[2].set(isDraft=True)
    await world.sync()
    updated = world.rows("SELECT number, github_updated_at FROM pull_requests ORDER BY number")
    # #1 only changes in place; #2 also gets a comment, so it is hydrated, and between the index
    # and its hydration its association changes again: the hydrated value is written.
    for pull in repository.pulls.values():
        pull.set(isDraft=False, authorAssociation="MEMBER")
    repository.pulls[2].add("comments", scenario.node("comments", by=author))
    scenario.after("PullRequestIndex", lambda: repository.pulls[2].set(authorAssociation="OWNER"))

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert [(u.number, u.is_draft, u.author_association) for u in report.in_place_updates] == [
        (1, False, "MEMBER")
    ]
    assert planned(report) == [2]
    assert world.rows(
        "SELECT number, is_draft, author_association FROM pull_requests ORDER BY number"
    ) == [
        {"number": 1, "is_draft": 0, "author_association": "MEMBER"},
        {"number": 2, "is_draft": 0, "author_association": "OWNER"},
    ]
    after = world.rows("SELECT number, github_updated_at FROM pull_requests ORDER BY number")
    assert after[0] == updated[0]


async def test_a_closing_link_added_after_merge_is_hydrated(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world)
    issues = scenario.add_repository("prebid", "issues")
    pull = repository.pulls[1]
    pull.merge(by=author)
    await world.sync()
    pull.link(5, issues)

    report = await world.sync()

    assert [(item.number, item.reasons) for item in report.planned] == [
        (1, frozenset({Reason.LINK_TOTAL, Reason.LINK_NOT_STORED}))
    ]
    assert world.rows(
        "SELECT issue_number, issue_repo_full_name, closing_issues_total FROM pr_closing_issues "
        "JOIN pull_requests ON pull_requests.id = pr_closing_issues.pr_id"
    ) == [{"issue_number": 5, "issue_repo_full_name": "prebid/issues", "closing_issues_total": 1}]
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_closing_link_listed_twice_in_the_index_is_hydrated(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    issues = scenario.add_repository("prebid", "issues")
    pull = repository.pulls[1]
    pull.link(5, issues)
    pull.link(6, issues)
    await world.sync()

    def listed_twice(body: dict[str, Any]) -> None:
        links = body["data"]["repository"]["pullRequests"]["nodes"][0]["closingIssuesReferences"]
        links["nodes"][1] = copy.deepcopy(links["nodes"][0])

    scenario.replay.modify(listed_twice, when=lambda seen: seen.operation == "PullRequestIndex")

    report = await world.sync()

    assert [(item.number, item.reasons) for item in report.planned] == [
        (1, frozenset({Reason.LINK_REPEATED}))
    ]
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.value("SELECT count(*) FROM pr_closing_issues") == 2


async def test_more_than_ten_closing_links_are_stored_and_not_planned_again(
    world: World,
) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    issues = scenario.add_repository("prebid", "issues")
    for number in range(1, 12):
        repository.pulls[1].link(number, issues)

    first = await world.sync()
    second = await world.sync()

    assert planned(first) == [1]
    assert world.value("SELECT count(*) FROM pr_closing_issues") == 11
    assert world.value("SELECT closing_issues_total FROM pull_requests") == 11
    assert planned(second) == []
    assert ended(world, second) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_an_item_leaving_coverage_after_the_index_is_skipped(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    committer = scenario.add_account()
    pull = repository.pulls[1]
    pull.add("commits", scenario.node("commits", by=committer))
    scenario.after("PullRequestIndex", lambda: pull.close(at=world.floor - timedelta(days=1)))

    report = await world.sync()

    assert planned(report) == [1]
    assert world.numbers(repository) == []
    assert report.failures == ()
    # Skipped before any account lookup for it.
    assert "UserLookup" not in operations(scenario)
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.run(report.run_id)["items_hydrated"] == 0


async def test_the_run_counters(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=3)
    await world.sync()
    repository.pulls[1].gone = True
    repository.pulls[2].add("comments", scenario.node("comments", by=author))
    scenario.replay.modify(
        lambda body: body["data"]["repository"]["p0"].update(number=99),
        when=lambda seen: seen.operation == "PullRequestBatch",
        times=2,
    )
    before = len(scenario.replay.seen)

    report = await world.sync()

    graphql = [seen for seen in scenario.replay.seen[before:] if seen.operation != "rest"]
    run = world.run(report.run_id)
    assert (
        run["graphql_points"],
        run["rest_calls"],
        run["items_planned"],
        run["items_hydrated"],
        run["items_deleted"],
        run["failures"],
    ) == (len(graphql), 0, 1, 0, 1, 1)
    assert world.failures() == [("prebid/Prebid.js", 2, "IdentityMismatch")]
    assert world.numbers(repository) == [2, 3]
    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "complete"})


# --------------------------------------------------------------------------------------------
# Tracking
# --------------------------------------------------------------------------------------------
async def test_an_unresolvable_repository_fails_while_the_others_sync(world: World) -> None:
    repository, _ = project(world)
    world.track("prebid/missing")

    report = await world.sync()

    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "complete"})
    assert world.numbers(repository) == [1]
    missing = report.repositories[1]
    assert (missing.github_id, missing.outcome) == (None, None)
    assert missing.reason == "RepositoryNotFound: prebid/missing: repository not found"
    assert world.value("SELECT count(*) FROM repositories") == 1


async def test_no_repository_resolving_is_fatal(world: World) -> None:
    world.track("prebid/missing")

    report = await world.sync()

    assert ended(world, report) == ("failed", 1, {})
    assert report.error == "NoRepositoryResolved: no configured repository resolved"


async def test_a_stored_repository_whose_resolve_failed_keeps_its_items(world: World) -> None:
    repository, _ = project(world, pulls=2)
    await world.sync()
    resolved_at = world.value("SELECT resolved_at FROM repositories")
    other, _ = project(world, name="other")
    repository.gone = True

    report = await world.sync()

    assert ended(world, report) == (
        "complete",
        2,
        {"prebid/Prebid.js": "failed", "prebid/other": "complete"},
    )
    assert world.numbers(repository) == [1, 2]
    assert (
        world.value("SELECT resolved_at FROM repositories WHERE full_name = 'prebid/Prebid.js'")
        == resolved_at
    )
    assert world.numbers(other) == [1]


async def test_resolved_at_is_the_resolve_date(world: World) -> None:
    project(world)
    await world.sync()

    assert world.value("SELECT resolved_at FROM repositories") == "2026-10-17 12:11:00.000000"


async def test_a_repository_the_deadline_kept_from_resolving_is_incomplete(
    world: World,
) -> None:
    scenario = world.scenario
    first, _ = project(world)
    second, _ = project(world, name="second")
    await world.sync()
    scenario.after(
        "Resolve",
        lambda: scenario.clock.advance(120),
        when=lambda seen: seen.variables["name"] == "Prebid.js",
    )

    report = await world.sync(max_minutes=1)

    assert ended(world, report) == (
        "time_limited",
        2,
        {"prebid/Prebid.js": "incomplete_index", "prebid/second": "incomplete_index"},
    )
    assert [r.reason for r in report.repositories] == [
        "the deadline came before its index finished",
        "the deadline came before its resolve",
    ]
    assert world.numbers(first) == [1]
    assert world.numbers(second) == [1]


async def test_two_entries_resolving_to_one_repository_fail_the_later(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    await world.sync()
    scenario.rename_repository(repository, "Prebid2.js")
    world.track("prebid/Prebid2.js")

    report = await world.sync()

    assert ended(world, report) == ("complete", 2, {"prebid/Prebid2.js": "complete"})
    later = report.repositories[1]
    assert (later.outcome, later.reason) == (
        None,
        f"prebid/Prebid2.js resolves to github_id {repository.data['databaseId']}, as "
        "prebid/Prebid.js does",
    )
    assert world.value("SELECT count(*) FROM repositories") == 1


async def test_a_repo_run_writes_no_state_for_another_repository(world: World) -> None:
    first, _ = project(world)
    project(world, name="second")
    initial = await world.sync()
    tracked = world.config().config.repositories

    report = await world.sync(repositories=(tracked[0],))

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.rows(
        "SELECT r.full_name, s.last_run_id FROM repo_sync_state s "
        "JOIN repositories r ON r.id = s.repository_id ORDER BY r.full_name"
    ) == [
        {"full_name": "prebid/Prebid.js", "last_run_id": report.run_id},
        {"full_name": "prebid/second", "last_run_id": initial.run_id},
    ]
    assert [r.configured.full_name for r in report.repositories] == ["prebid/Prebid.js"]
    assert world.numbers(first) == [1]


async def test_a_dropped_repository_is_untracked_then_pruned(world: World) -> None:
    project(world)
    dropped, _ = project(world, pulls=2, name="dropped")
    await world.sync()
    world.untrack("prebid/dropped")
    tracked = world.config().config.repositories

    # --repo neither marks nor reports, and refuses the prune.
    under_repo = await world.sync(repositories=tracked, prune_untracked=True)
    assert ended(world, under_repo) == ("complete", 2, {"prebid/Prebid.js": "complete"})
    assert under_repo.untracked == ()
    assert under_repo.prune_refused == "--prune-untracked is refused with --repo"
    assert world.value("SELECT is_tracked FROM repositories WHERE name = 'dropped'") == 1

    # The run that marks it untracked exits 2 and does not prune it.
    marked = await world.sync(prune_untracked=True)
    assert ended(world, marked) == (
        "complete",
        2,
        {"prebid/Prebid.js": "complete", "prebid/dropped": "untracked"},
    )
    assert marked.untracked == ("prebid/dropped",)
    assert marked.pruned == ()
    assert world.rows("SELECT full_name, is_tracked FROM repositories ORDER BY full_name") == [
        {"full_name": "prebid/Prebid.js", "is_tracked": 1},
        {"full_name": "prebid/dropped", "is_tracked": 0},
    ]

    # The next run prunes it; a pruned repository is no longer stored, so it makes no exit 2.
    pruned = await world.sync(prune_untracked=True)
    assert ended(world, pruned) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.rows("SELECT full_name FROM repositories") == [{"full_name": "prebid/Prebid.js"}]
    assert world.numbers(dropped) == []
    run = world.run(pruned.run_id)
    assert run["items_deleted"] == 2
    assert json.loads(run["deletions_approved"]) == {
        "prune_untracked": [
            {
                "github_id": dropped.data["databaseId"],
                "full_name": "prebid/dropped",
                "items_deleted": 2,
            }
        ]
    }


async def test_a_readded_repository_is_tracked_again_and_not_pruned(world: World) -> None:
    project(world)
    readded, _ = project(world, name="readded")
    await world.sync()
    world.untrack("prebid/readded")
    await world.sync()
    world.track(readded)

    report = await world.sync(prune_untracked=True)

    assert ended(world, report) == (
        "complete",
        0,
        {"prebid/Prebid.js": "complete", "prebid/readded": "complete"},
    )
    assert report.pruned == ()
    assert world.rows("SELECT full_name, is_tracked FROM repositories ORDER BY full_name") == [
        {"full_name": "prebid/Prebid.js", "is_tracked": 1},
        {"full_name": "prebid/readded", "is_tracked": 1},
    ]


async def test_a_refused_prune_deletes_nothing(world: World) -> None:
    project(world)
    dropped, _ = project(world, name="dropped")
    await world.sync()
    world.untrack("prebid/dropped")
    await world.sync()
    world.track("prebid/missing")

    report = await world.sync(prune_untracked=True)

    assert ended(world, report) == (
        "complete",
        2,
        {"prebid/Prebid.js": "complete", "prebid/dropped": "untracked"},
    )
    assert report.prune_refused == (
        "--prune-untracked is refused: configured repositories did not resolve: prebid/missing"
    )
    assert world.numbers(dropped) == [1]
    assert world.run(report.run_id)["deletions_approved"] is None


# --------------------------------------------------------------------------------------------
# Deletions
# --------------------------------------------------------------------------------------------
def approve(world: World, value: str) -> DeletionApproval:
    return parse_allow_deletions(value, world.config().config)


def halt_reasons(world: World) -> dict[str, str | None]:
    return {
        row["full_name"]: row["halt_reason"]
        for row in world.rows(
            "SELECT r.full_name, s.halt_reason FROM repo_sync_state s "
            "JOIN repositories r ON r.id = s.repository_id ORDER BY r.full_name"
        )
    }


async def test_a_mass_delete_halts_until_allowed(world: World) -> None:
    repository, _ = project(world, pulls=30)
    await world.sync()
    for number in range(1, 28):
        repository.pulls[number].gone = True

    halted = await world.sync()
    assert ended(world, halted) == ("complete", 2, {"prebid/Prebid.js": "halted"})
    assert halt_reasons(world) == {
        "prebid/Prebid.js": "27 deletion candidates exceed the delete guard's 25 (30 stored "
        "rows before the run)"
    }
    assert len(world.numbers(repository)) == 30
    assert world.run(halted.run_id)["deletions_approved"] is None

    allowed = await world.sync(allow_deletions=approve(world, "prebid/Prebid.js=27"))
    assert ended(world, allowed) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.numbers(repository) == [28, 29, 30]
    assert halt_reasons(world) == {"prebid/Prebid.js": None}
    run = world.run(allowed.run_id)
    assert run["items_deleted"] == 27
    assert json.loads(run["deletions_approved"]) == {
        "allow_deletions": {
            "repository": "prebid/Prebid.js",
            "github_id": repository.data["databaseId"],
            "approved": 27,
            "sets": {"items": {"candidates": 27, "deleted": True}},
        }
    }


async def test_an_approval_names_the_configured_repository_after_a_rename(
    world: World,
) -> None:
    scenario = world.scenario
    renamed, _ = project(world, pulls=26)
    other, _ = project(world, pulls=26, name="other")
    await world.sync()
    scenario.rename_repository(renamed, "Prebid2.js")
    for pull in [*renamed.pulls.values(), *other.pulls.values()]:
        pull.gone = True

    report = await world.sync(allow_deletions=approve(world, "prebid/Prebid.js=26"))

    assert ended(world, report) == (
        "complete",
        2,
        {"prebid/Prebid2.js": "complete", "prebid/other": "halted"},
    )
    assert world.numbers(renamed) == []
    assert len(world.numbers(other)) == 26
    assert json.loads(world.run(report.run_id)["deletions_approved"]) == {
        "allow_deletions": {
            "repository": "prebid/Prebid.js",
            "github_id": renamed.data["databaseId"],
            "approved": 26,
            "sets": {"items": {"candidates": 26, "deleted": True}},
        }
    }
    assert halt_reasons(world) == {
        "prebid/Prebid2.js": None,
        "prebid/other": "26 deletion candidates exceed the delete guard's 25 (26 stored rows "
        "before the run)",
    }


async def test_an_incomplete_index_deletes_nothing(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world, pulls=2)
    await world.sync()
    repository.pulls[1].gone = True

    def miscounted(body: dict[str, Any]) -> None:
        body["data"]["repository"]["pullRequests"]["totalCount"] += 1

    scenario.replay.modify(
        miscounted, when=lambda seen: seen.operation == "PullRequestIndex", times=2
    )

    report = await world.sync()

    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "incomplete_index"})
    assert report.repositories[0].reason == (
        "IndexCountMismatch: prebid/Prebid.js: pullRequests index enumerated 1 items, totalCount 2"
    )
    assert world.numbers(repository) == [1, 2]
    assert report.deletions == ()


async def test_the_guard_counts_the_rows_stored_before_the_run(world: World) -> None:
    scenario = world.scenario
    world.sync_settings = {"delete_guard_min_rows": 0, "delete_guard_percent": 50.0}
    repository, author = project(world, pulls=4)
    await world.sync()
    for number in (1, 2, 3):
        repository.pulls[number].gone = True
    for _ in range(4):
        scenario.open_pull_request(repository, author)

    report = await world.sync()

    # 3 candidates exceed 50% of the 4 rows stored before the run, not of the 5 after it.
    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "halted"})
    assert halt_reasons(world) == {
        "prebid/Prebid.js": "3 deletion candidates exceed the delete guard's 2 (4 stored rows "
        "before the run)"
    }
    assert world.numbers(repository) == [1, 2, 3, 4, 5, 6, 7, 8]


@pytest.mark.parametrize("halt", [False, True])
async def test_failure_rows_leave_with_their_items(world: World, halt: bool) -> None:
    repository, _ = project(world, pulls=30 if halt else 2)
    await world.sync()
    world.execute(
        "INSERT INTO sync_failures (repository_id, item_kind, number, attempts, last_error, "
        "first_failed_at, last_failed_at, last_run_id) SELECT id, 'pull_request', 1, 1, "
        "'ItemError: #1', '2026-10-17 12:00:00', '2026-10-17 12:00:00', 1 FROM repositories"
    )
    gone = range(1, 28) if halt else (1,)
    for number in gone:
        repository.pulls[number].gone = True

    report = await world.sync()

    assert world.failures() == []
    expected = "halted" if halt else "complete"
    assert ended(world, report) == ("complete", 2 if halt else 0, {"prebid/Prebid.js": expected})
    assert len(world.numbers(repository)) == (30 if halt else 1)


async def test_a_closed_pull_request_on_each_side_of_the_floor(world: World) -> None:
    repository, _ = project(world, pulls=2)
    now = world.scenario.clock.now()
    repository.pulls[1].close(at=now - timedelta(days=2))
    repository.pulls[2].close(at=now)
    initial = await world.sync()
    assert planned(initial) == [2, 1]
    world.floor = now - timedelta(days=1)

    report = await world.sync()

    # #1 closed before the floor: out of coverage, so not planned and deleted; #2 stays.
    assert planned(report) == []
    assert world.numbers(repository) == [2]
    assert world.run(report.run_id)["items_deleted"] == 1
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    world.floor = now + timedelta(days=1)
    world.scenario.open_pull_request(repository, world.scenario.add_account())
    fresh = await world.sync()
    assert planned(fresh) == [3]


# --------------------------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------------------------
def requests(scenario: Scenario, operation: str) -> int:
    return operations(scenario).count(operation)


async def test_an_index_state_its_dates_contradict_fails_without_hydration(
    world: World,
) -> None:
    scenario = world.scenario
    repository, author = project(world)
    await world.sync()
    repository.pulls[1].add("comments", scenario.node("comments", by=author))

    def contradicted(body: dict[str, Any]) -> None:
        body["data"]["repository"]["pullRequests"]["nodes"][0]["state"] = "MERGED"

    scenario.replay.modify(contradicted, when=lambda seen: seen.operation == "PullRequestIndex")
    before = requests(scenario, "PullRequestBatch")

    failed = await world.sync()
    assert requests(scenario, "PullRequestBatch") == before
    assert world.failures() == [("prebid/Prebid.js", 1, "StateContradictsDates")]
    assert ended(world, failed) == ("complete", 2, {"prebid/Prebid.js": "complete"})
    assert world.numbers(repository) == [1]
    assert world.value("SELECT comments_total FROM pull_requests") == 0

    cleared = await world.sync()
    assert [(item.number, item.reasons) for item in cleared.planned] == [
        (1, frozenset({Reason.UPDATED_AT, Reason.FAILED}))
    ]
    assert world.failures() == []
    assert ended(world, cleared) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_number_stored_under_another_id_fails_and_leaves_through_step_7(
    world: World,
) -> None:
    scenario = world.scenario
    repository, author = project(world)
    await world.sync()
    old_id = world.value("SELECT github_id FROM pull_requests")
    scenario.open_pull_request(repository, author, number=1)

    failed = await world.sync()
    assert world.failures() == [("prebid/Prebid.js", 1, "NumberStoredUnderAnotherId")]
    assert requests(scenario, "PullRequestBatch") == 1
    assert world.rows("SELECT github_id FROM pull_requests") == []
    assert world.run(failed.run_id)["items_deleted"] == 1
    assert ended(world, failed) == ("complete", 2, {"prebid/Prebid.js": "complete"})

    cleared = await world.sync()
    assert world.value("SELECT github_id FROM pull_requests") != old_id
    assert world.failures() == []
    assert ended(world, cleared) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_hydrated_id_other_than_the_index_fails_at_once(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    scenario.replay.modify(
        lambda body: body["data"]["repository"]["p0"].update(fullDatabaseId="123"),
        when=lambda seen: seen.operation == "PullRequestBatch",
    )

    failed = await world.sync()
    assert requests(scenario, "PullRequestBatch") == 1
    assert world.failures() == [("prebid/Prebid.js", 1, "HydratedIdMismatch")]
    assert world.numbers(repository) == []
    assert ended(world, failed) == ("complete", 2, {"prebid/Prebid.js": "complete"})

    cleared = await world.sync()
    assert world.numbers(repository) == [1]
    assert world.failures() == []
    assert ended(world, cleared) == ("complete", 0, {"prebid/Prebid.js": "complete"})


@pytest.mark.parametrize("connection", ["reviews", "comments"])
async def test_a_review_or_comment_id_stored_under_another_pull_request_fails_at_once(
    world: World, connection: str
) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=2)
    review = scenario.node(connection, by=author)
    repository.pulls[1].add(connection, review)
    await world.sync()
    repository.pulls[2].add(connection, copy.deepcopy(review))
    before = requests(scenario, "PullRequestBatch")

    failed = await world.sync()
    assert requests(scenario, "PullRequestBatch") == before + 1
    assert world.failures() == [("prebid/Prebid.js", 2, "ItemConflict")]
    assert ended(world, failed) == ("complete", 2, {"prebid/Prebid.js": "complete"})

    repository.pulls[2].remove(connection, lambda node: node["id"] == review["id"])
    cleared = await world.sync()
    assert world.failures() == []
    assert ended(world, cleared) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_lookup_of_another_id_than_the_commit_link_fails_at_once(
    world: World,
) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    committer = scenario.add_account()
    repository.pulls[1].add("commits", scenario.node("commits", by=committer))
    scenario.replay.modify(
        lambda body: body["data"]["nodes"][0].update(databaseId=42),
        when=lambda seen: seen.operation == "UserLookup",
    )

    failed = await world.sync()
    assert requests(scenario, "UserLookup") == 1
    assert world.failures() == [("prebid/Prebid.js", 1, "ItemConflict")]
    assert "the lookup for commit link" in world.value("SELECT last_error FROM sync_failures")
    assert world.rows("SELECT github_id FROM github_users WHERE github_id = 42") == []
    assert ended(world, failed) == ("complete", 2, {"prebid/Prebid.js": "complete"})

    cleared = await world.sync()
    assert world.failures() == []
    assert (
        world.value(
            "SELECT login FROM github_users WHERE github_id = ?",
            scenario.account(committer)["databaseId"],
        )
        == scenario.account(committer)["login"]
    )
    assert ended(world, cleared) == ("complete", 0, {"prebid/Prebid.js": "complete"})


def failing_lookup(scenario: Scenario, ids: int) -> None:
    """Every attempt of the next lookup request holding ``ids`` ids answers 502."""
    scenario.replay.fault(
        lambda request: synthetic("bad_gateway"),
        when=lambda seen: seen.operation == "UserLookup" and len(seen.variables["ids"]) == ids,
        times=4,
    )


async def test_a_failed_lookup_is_retried_with_each_items_own_ids(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world, pulls=2)
    first, second = scenario.add_account(), scenario.add_account()
    repository.pulls[1].add("commits", scenario.node("commits", by=first))
    repository.pulls[2].add("commits", scenario.node("commits", by=second))
    failing_lookup(scenario, ids=2)
    # #2's own lookup fails too; #1's succeeds.
    scenario.replay.fault(
        lambda request: synthetic("bad_gateway"),
        when=lambda seen: seen.operation == "UserLookup" and seen.variables["ids"] == [second],
        times=4,
    )

    report = await world.sync()

    lookups = [
        seen.variables["ids"] for seen in scenario.replay.seen if seen.operation == "UserLookup"
    ]
    assert lookups == [[first, second]] * 4 + [[first]] + [[second]] * 4
    assert world.numbers(repository) == [1]
    assert world.failures() == [("prebid/Prebid.js", 2, "UserLookupFailed")]
    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "complete"})


async def test_a_failed_lookup_of_only_the_items_own_ids_is_not_retried(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    committer = scenario.add_account()
    repository.pulls[1].add("commits", scenario.node("commits", by=committer))
    failing_lookup(scenario, ids=1)

    report = await world.sync()

    assert requests(scenario, "UserLookup") == 4
    assert world.failures() == [("prebid/Prebid.js", 1, "UserLookupFailed")]
    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "complete"})


async def test_a_duplicate_repository_label_id_fails_the_repository(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    project(world, name="other")
    scenario.add_label(repository, "bug")
    await world.sync()

    def doubled(body: dict[str, Any]) -> None:
        labels = body["data"]["repository"]["labels"]
        labels["nodes"].append(copy.deepcopy(labels["nodes"][0]))
        labels["totalCount"] += 1

    scenario.replay.modify(
        doubled,
        when=lambda seen: seen.operation == "Resolve" and seen.variables["name"] == "Prebid.js",
    )

    report = await world.sync()

    assert report.repositories[0].reason == (
        "RepositoryShapeError: prebid/Prebid.js: label id "
        f"{repository.data['labels']['nodes'][0]['id']} is listed twice"
    )
    assert world.value("SELECT count(*) FROM repo_labels") == 1
    assert ended(world, report) == (
        "complete",
        2,
        {"prebid/Prebid.js": "failed", "prebid/other": "complete"},
    )


async def test_a_repository_gone_during_hydration_fails_for_the_rest_of_the_run(
    world: World,
) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=27)
    await world.sync()
    world.execute(
        "INSERT INTO sync_pending (repository_id, item_kind, number, run_id) "
        "SELECT id, 'pull_request', 2, 1 FROM repositories"
    )
    repository.pulls[1].gone = True
    for pull in repository.pulls.values():
        pull.add("comments", scenario.node("comments", by=author))
    scenario.after("PullRequestBatch", lambda: setattr(repository, "gone", True))
    index_started = world.value("SELECT last_index_started_at FROM repo_sync_state")

    report = await world.sync()

    assert batches(scenario)[-2:] == [list(range(2, 27)), [27]]
    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "failed"})
    assert report.repositories[0].reason == (
        "RepositoryNotFound: prebid/Prebid.js: repository not found"
    )
    # Its written items stay, step 7 skips it (#1 is not deleted), and sync_pending is unchanged.
    assert world.value("SELECT count(*) FROM pr_comments") == 25
    assert world.numbers(repository) == list(range(1, 28))
    assert world.pending(repository) == [2]
    assert report.deletions == ()
    assert world.value("SELECT last_index_started_at FROM repo_sync_state") == index_started


async def test_a_database_error_mid_batch_is_fatal_and_the_next_run_resumes(
    world: World,
) -> None:
    repository, _ = project(world, pulls=2)
    world.execute(
        "CREATE TRIGGER refuse_second BEFORE INSERT ON pull_requests WHEN NEW.number = 2 "
        "BEGIN SELECT RAISE(ABORT, 'refused by the test'); END"
    )

    failed = await world.sync()
    assert failed.error is not None
    assert failed.error.startswith("LocalDatabaseError: IntegrityError: refused by the test")
    assert ended(world, failed) == ("failed", 1, {"prebid/Prebid.js": "failed"})
    assert world.numbers(repository) == [1]
    run = world.run(failed.run_id)
    assert (run["items_planned"], run["items_hydrated"]) == (2, 1)

    world.execute("DROP TRIGGER refuse_second")
    resumed = await world.sync()
    assert planned(resumed) == [2]
    assert world.numbers(repository) == [1, 2]
    assert ended(world, resumed) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_fatal_source_error_closes_the_run_failed(world: World) -> None:
    scenario = world.scenario
    project(world)
    scenario.replay.fault(
        lambda request: synthetic("unauthorized"),
        when=lambda seen: seen.operation == "PullRequestIndex",
    )

    report = await world.sync()

    assert report.error is not None and report.error.startswith("Unauthorized")
    assert ended(world, report) == ("failed", 1, {"prebid/Prebid.js": "failed"})
    state = world.rows("SELECT last_index_started_at, index_pr_total FROM repo_sync_state")
    assert state == [{"last_index_started_at": None, "index_pr_total": None}]


async def test_any_other_exception_after_the_claim_closes_the_run_failed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    project(world)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("broken in-place update")

    monkeypatch.setattr(writer, "apply_in_place_updates", broken)

    report = await world.sync()

    assert report.error == "RuntimeError: broken in-place update"
    assert ended(world, report) == ("failed", 1, {"prebid/Prebid.js": "failed"})


async def test_an_account_missing_refusal_is_an_item_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.expects_account_missing = True
    repository, _ = project(world, pulls=2)
    write = writer.write_pull_request

    async def refusing(connection: Any, run_id: int, record: Any, **kwargs: Any) -> Any:
        if record.number == 1:
            raise writer.AccountMissing(record.number, [7])
        return await write(connection, run_id, record, **kwargs)

    monkeypatch.setattr(writer, "write_pull_request", refusing)

    report = await world.sync()

    assert world.failures() == [("prebid/Prebid.js", 1, "AccountMissing")]
    assert world.numbers(repository) == [2]
    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "complete"})


# --------------------------------------------------------------------------------------------
# Accounts (D28)
# --------------------------------------------------------------------------------------------
def account_row(world: World, node_id: str) -> dict[str, Any]:
    (row,) = world.rows(
        "SELECT github_id, login, type, refreshed_at, not_found_at FROM github_users "
        "WHERE node_id = ?",
        node_id,
    )
    return row


async def test_a_bot_seen_first_through_a_commit_is_looked_up(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    bot = scenario.add_account("Bot", login="helper")
    repository.pulls[1].add("commits", scenario.node("commits", by=bot))

    report = await world.sync()

    assert requests(scenario, "UserLookup") == 1
    data = scenario.account(bot)
    assert account_row(world, bot) == {
        "github_id": data["databaseId"],
        "login": "helper",
        "type": "Bot",
        "refreshed_at": "2026-10-17 12:11:00.000000",
        "not_found_at": None,
    }
    assert world.rows("SELECT linked_github_id, author_email FROM pr_commits") == [
        {
            "linked_github_id": data["databaseId"],
            "author_email": f"{data['databaseId']}+helper[bot]@users.noreply.github.com",
        }
    ]
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_deleted_account_keeps_its_references_until_seen_again(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world)
    pull = repository.pulls[1]
    pull.add("reviews", scenario.node("reviews", by=author))
    pull.add("comments", scenario.node("comments", by=author))
    pull.add("commits", scenario.node("commits", by=author))
    await world.sync()
    account_id = world.value("SELECT id FROM github_users WHERE node_id = ?", author)
    scenario.delete_account(author)
    pull.touch()
    scenario.clock.advance(24 * 3600)

    deleted = await world.sync()
    assert ended(world, deleted) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.rows("SELECT author_id, author_reported FROM pull_requests") == [
        {"author_id": account_id, "author_reported": 0}
    ]
    assert world.rows("SELECT reviewer_id, reviewer_reported FROM pr_reviews") == [
        {"reviewer_id": account_id, "reviewer_reported": 0}
    ]
    assert world.rows("SELECT author_id, author_reported FROM pr_comments") == [
        {"author_id": account_id, "author_reported": 0}
    ]
    assert world.rows("SELECT author_id, linked_github_id FROM pr_commits") == [
        {"author_id": account_id, "linked_github_id": None}
    ]
    # The refresh found it gone: not_found_at from the lookup's Date, refreshed_at kept.
    row = account_row(world, author)
    assert (row["not_found_at"], row["refreshed_at"]) == (
        "2026-10-18 12:22:00.000000",
        "2026-10-17 12:11:00.000000",
    )

    scenario.restore_account(author)
    pull.add("comments", scenario.node("comments", by=author))
    seen = await world.sync()
    assert account_row(world, author)["not_found_at"] is None
    assert world.rows("SELECT author_reported FROM pr_comments ORDER BY id") == [
        {"author_reported": 0},
        {"author_reported": 1},
    ]
    assert ended(world, seen) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_an_account_deleted_before_it_was_stored(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    gone = scenario.add_account()
    gone_id = scenario.account(gone)["databaseId"]
    scenario.delete_account(gone)
    # Only a commit made afterwards still links the account (gap 90).
    repository.pulls[1].add("commits", scenario.node("commits", by=gone))

    report = await world.sync()

    assert requests(scenario, "UserLookup") == 1
    assert world.rows("SELECT author_id, linked_github_id FROM pr_commits") == [
        {"author_id": None, "linked_github_id": gone_id}
    ]
    assert world.rows("SELECT id FROM github_users WHERE github_id = ?", gone_id) == []
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_an_object_first_written_after_its_authors_deletion(world: World) -> None:
    scenario = world.scenario
    _, author = project(world)
    scenario.delete_account(author)

    report = await world.sync()

    assert world.rows("SELECT author_id, author_reported FROM pull_requests") == [
        {"author_id": None, "author_reported": 0}
    ]
    assert world.rows("SELECT id FROM github_users WHERE node_id = ?", author) == []
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_replaced_commit_link_is_written(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world)
    other = scenario.add_account()
    pull = repository.pulls[1]
    pull.add("commits", scenario.node("commits", by=author))
    await world.sync()
    pull.node["commits"]["nodes"][0]["commit"]["author"] = scenario.node("commits", by=other)[
        "commit"
    ]["author"]
    pull.touch()

    report = await world.sync()

    other_id = scenario.account(other)["databaseId"]
    assert world.rows(
        "SELECT u.github_id, c.linked_github_id FROM pr_commits c "
        "JOIN github_users u ON u.id = c.author_id"
    ) == [{"github_id": other_id, "linked_github_id": other_id}]
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


@pytest.mark.parametrize("past", [False, True])
async def test_the_refresh_on_each_side_of_24_hours(world: World, past: bool) -> None:
    scenario = world.scenario
    project(world)
    gone = scenario.add_account()
    repository = scenario.repositories["prebid/prebid.js"]
    repository.pulls[1].add("comments", scenario.node("comments", by=gone))
    await world.sync()
    author = repository.pulls[1].node["author"]["id"]
    stored = account_row(world, author)
    # An account with not_found_at set is skipped by the refresh.
    scenario.delete_account(gone)
    world.execute("UPDATE github_users SET not_found_at = refreshed_at WHERE node_id = ?", gone)
    scenario.account(author)["login"] = "renamed"
    scenario.clock.advance(24 * 3600 + (1 if past else 0))
    before = requests(scenario, "UserLookup")

    report = await world.sync(settle=False)

    lookups = [s.variables["ids"] for s in scenario.replay.seen[-2:] if s.operation == "UserLookup"]
    row = account_row(world, author)
    if past:
        assert lookups == [[author]]
        assert (row["login"], row["refreshed_at"]) == ("renamed", "2026-10-18 12:11:01.000000")
    else:
        assert requests(scenario, "UserLookup") == before
        assert (row["login"], row["refreshed_at"]) == (stored["login"], stored["refreshed_at"])
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_commit_link_to_a_not_found_account_looks_it_up(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    linked = scenario.add_account()
    repository.pulls[1].add("comments", scenario.node("comments", by=linked))
    await world.sync()
    scenario.delete_account(linked)
    scenario.clock.advance(24 * 3600)
    await world.sync()
    assert account_row(world, linked)["not_found_at"] is not None
    scenario.restore_account(linked)
    repository.pulls[1].add("commits", scenario.node("commits", by=linked))

    report = await world.sync()

    # Looked up within 24 hours of its last refresh because a hydrated commit links it.
    assert scenario.replay.seen[-1].operation == "UserLookup"
    assert scenario.replay.seen[-1].variables["ids"] == [linked]
    assert account_row(world, linked)["not_found_at"] is None
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


# --------------------------------------------------------------------------------------------
# Stopping
# --------------------------------------------------------------------------------------------
def deadline_after(scenario: Scenario, operation: str, when: Any = None) -> None:
    """Move the clock past a one-minute deadline right after the next ``operation`` request."""
    scenario.after(operation, lambda: scenario.clock.advance(120), when=when)


def index_started(world: World) -> dict[str, str | None]:
    return {
        row["full_name"]: row["last_index_started_at"]
        for row in world.rows(
            "SELECT r.full_name, s.last_index_started_at FROM repo_sync_state s "
            "JOIN repositories r ON r.id = s.repository_id ORDER BY r.full_name"
        )
    }


async def test_a_deadline_before_any_resolve_is_time_limited(world: World) -> None:
    repository, _ = project(world)
    await world.sync()

    report = await world.sync(max_minutes=0)

    assert operations(world.scenario)[-1] == "PullRequestBatch"
    assert ended(world, report) == ("time_limited", 2, {"prebid/Prebid.js": "incomplete_index"})
    assert world.numbers(repository) == [1]


async def test_a_deadline_during_a_middle_repositorys_index(world: World) -> None:
    scenario = world.scenario
    first, _ = project(world, name="a")
    project(world, pulls=101, name="b")
    project(world, name="c")
    first.pulls[1].set(isDraft=True)
    await world.sync(repositories=world.config().config.repositories[:1])
    first.pulls[1].set(isDraft=False)
    scenario.open_pull_request(first, first.pulls[1].node["author"]["id"])
    deadline_after(
        scenario,
        "PullRequestIndex",
        when=lambda seen: seen.variables["name"] == "b" and seen.variables["after"] is None,
    )

    report = await world.sync(max_minutes=1)

    assert ended(world, report) == (
        "time_limited",
        2,
        {
            "prebid/a": "time_limited",
            "prebid/b": "incomplete_index",
            "prebid/c": "incomplete_index",
        },
    )
    # a's index completed: its in-place update is applied and its new item waits in sync_pending.
    assert world.value("SELECT is_draft FROM pull_requests WHERE number = 1") == 0
    assert world.pending(first) == [2]
    started = index_started(world)
    assert started["prebid/a"] is not None
    assert (started["prebid/b"], started["prebid/c"]) == (None, None)
    assert [r.reason for r in report.repositories[1:]] == [
        "the deadline came before its index finished"
    ] * 2


async def test_a_deadline_during_hydration_then_a_run_that_finishes(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world, pulls=30)
    deadline_after(scenario, "PullRequestBatch")

    limited = await world.sync(max_minutes=1)
    assert ended(world, limited) == ("time_limited", 0, {"prebid/Prebid.js": "time_limited"})
    assert world.numbers(repository) == list(range(1, 26))
    assert world.pending(repository) == list(range(26, 31))
    assert index_started(world)["prebid/Prebid.js"] is not None
    assert limited.failures == ()

    finished = await world.sync()
    assert planned(finished)[:5] == list(range(26, 31))
    assert all(item.pending for item in finished.planned[:5])
    assert world.pending(repository) == []
    assert ended(world, finished) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_deadline_during_the_refresh_is_time_limited(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world)
    await world.sync()
    refreshed = account_row(world, author)["refreshed_at"]
    scenario.clock.advance(24 * 3600)
    repository.pulls[1].touch()
    deadline_after(scenario, "PullRequestBatch")

    report = await world.sync(max_minutes=1)

    assert ended(world, report) == ("time_limited", 0, {"prebid/Prebid.js": "complete"})
    assert "UserLookup" not in operations(scenario)
    assert account_row(world, author)["refreshed_at"] == refreshed


async def test_a_pending_item_whose_cause_cleared_is_not_hydrated(world: World) -> None:
    repository, _ = project(world)
    await world.sync()
    world.execute(
        "INSERT INTO sync_pending (repository_id, item_kind, number, run_id) "
        "SELECT id, 'pull_request', 1, 1 FROM repositories"
    )

    report = await world.sync()

    assert planned(report) == []
    assert world.pending(repository) == []
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


def interrupting(request: Any) -> Any:
    raise KeyboardInterrupt


async def test_an_interrupt_aborts_the_run_and_a_resume_keeps_its_writes(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world, pulls=30)
    other, _ = project(world, name="other")
    batch = 0

    def second_batch(seen: Any) -> bool:
        nonlocal batch
        batch += seen.operation == "PullRequestBatch"
        return seen.operation == "PullRequestBatch" and batch == 2

    scenario.replay.fault(interrupting, when=second_batch)

    with pytest.raises(KeyboardInterrupt):
        await world.sync()

    (run,) = world.rows("SELECT id, status, exit_code FROM sync_runs")
    assert (run["status"], run["exit_code"]) == ("aborted", 130)
    assert outcomes(world, run["id"]) == {"prebid/Prebid.js": "failed", "prebid/other": "failed"}
    assert world.numbers(repository) == list(range(1, 26))
    assert (
        world.rows("SELECT last_index_started_at FROM repo_sync_state")
        == [{"last_index_started_at": None}] * 2
    )

    resumed = await world.sync()
    assert planned(resumed) == [*range(26, 31), 1]
    assert world.numbers(repository) == list(range(1, 31))
    assert world.numbers(other) == [1]
    assert ended(world, resumed) == (
        "complete",
        0,
        {"prebid/Prebid.js": "complete", "prebid/other": "complete"},
    )


async def test_a_run_another_process_aborted_exits_1_and_stays_aborted(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    scenario.after(
        "PullRequestIndex",
        lambda: world.execute("UPDATE sync_runs SET status = 'aborted' WHERE status = 'running'"),
    )

    report = await world.sync()

    run = world.run(report.run_id)
    assert (run["status"], run["exit_code"], run["finished_at"]) == ("aborted", None, None)
    assert (report.status, report.exit_code) == ("aborted", 1)
    assert report.error == (
        f"RunAborted: run {report.run_id} is aborted, not running; run {report.run_id} is "
        "aborted: another process ended it"
    )
    assert world.numbers(repository) == []
    assert outcomes(world, report.run_id) == {}


@pytest.mark.parametrize("ending", ["fatal", "interrupt"])
async def test_a_run_ending_after_step_7_keeps_its_halt_reason(
    world: World, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    repository, _ = project(world, pulls=26)
    await world.sync()
    for pull in repository.pulls.values():
        pull.gone = True

    async def failing_prune(*args: Any) -> Any:
        if ending == "fatal":
            raise RuntimeError("prune failed")
        raise KeyboardInterrupt

    monkeypatch.setattr(writer, "prune_repositories", failing_prune)

    if ending == "fatal":
        report = await world.sync(prune_untracked=True)
        assert ended(world, report) == ("failed", 1, {"prebid/Prebid.js": "failed"})
    else:
        with pytest.raises(KeyboardInterrupt):
            await world.sync(prune_untracked=True)
        assert world.value("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1") == "aborted"
    assert halt_reasons(world) == {
        "prebid/Prebid.js": "26 deletion candidates exceed the delete guard's 25 (26 stored "
        "rows before the run)"
    }
    assert len(world.numbers(repository)) == 26


async def test_the_head_check_comes_before_the_claim_and_any_request(
    world: World, tmp_path: Path
) -> None:
    project(world)
    missing = tmp_path / "missing.db"
    world.url = f"sqlite+aiosqlite:///{missing}"

    for dry_run in (False, True):
        with pytest.raises(HeadCheckError, match="database file not found"):
            await world.sync(dry_run=dry_run)

    assert not missing.exists()
    assert world.scenario.replay.seen == []


async def test_a_database_not_at_head_starts_no_run(world: World) -> None:
    project(world)
    world.execute("UPDATE alembic_version SET version_num = 'older'")

    with pytest.raises(HeadCheckError, match="not the code's head"):
        await world.sync()

    assert world.rows("SELECT id FROM sync_runs") == []
    assert world.scenario.replay.seen == []


# --------------------------------------------------------------------------------------------
# Exit codes and accepted failures
# --------------------------------------------------------------------------------------------
def accept(world: World, *entries: tuple[str, int]) -> None:
    world.sync_settings["accepted_failures"] = [
        {"repository": repository, "number": number, "reason": f"known #{number}"}
        for repository, number in entries
    ]


async def test_an_accepted_failure_leaves_exit_0(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world, pulls=2)
    accept(world, ("prebid/Prebid.js", 1))
    scenario.replay.modify(
        lambda body: body["data"]["repository"]["p0"].update(fullDatabaseId="123"),
        when=lambda seen: seen.operation == "PullRequestBatch",
    )

    report = await world.sync()

    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    assert world.failures() == [("prebid/Prebid.js", 1, "HydratedIdMismatch")]
    (failure,) = report.failures
    assert failure.accepted is not None and failure.accepted.reason == "known #1"
    assert report.accepted_not_failed == ()
    state = world.state(repository)
    assert state is not None
    assert state["last_success_at"] == world.run(report.run_id)["finished_at"]


async def test_an_unaccepted_failure_leaves_last_success_at(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world)
    initial = await world.sync()
    repository.pulls[1].touch()
    scenario.replay.modify(
        lambda body: body["data"]["repository"]["p0"].update(fullDatabaseId="123"),
        when=lambda seen: seen.operation == "PullRequestBatch",
    )

    report = await world.sync()

    assert ended(world, report) == ("complete", 2, {"prebid/Prebid.js": "complete"})
    state = world.state(repository)
    assert state is not None
    assert state["last_success_at"] == world.run(initial.run_id)["finished_at"]


async def test_an_accepted_entry_whose_item_did_not_fail_is_reported(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world, pulls=30)
    world.track("prebid/missing")
    accept(world, ("prebid/Prebid.js", 1), ("prebid/Prebid.js", 30), ("prebid/missing", 1))

    complete = await world.sync()
    # A repository with no outcome (it never resolved) reports none.
    assert [(e.repository, e.number) for e in complete.accepted_not_failed] == [
        ("prebid/Prebid.js", 1),
        ("prebid/Prebid.js", 30),
    ]

    for pull in repository.pulls.values():
        pull.touch()
    deadline_after(scenario, "PullRequestBatch")
    limited = await world.sync(max_minutes=1)
    assert outcomes(world, limited.run_id) == {"prebid/Prebid.js": "time_limited"}
    # #30 is pending, so it is not reported.
    assert [(e.repository, e.number) for e in limited.accepted_not_failed] == [
        ("prebid/Prebid.js", 1)
    ]

    scenario.replay.modify(
        lambda body: body["data"]["repository"]["pullRequests"].update(totalCount=99),
        when=lambda seen: seen.operation == "PullRequestIndex",
        times=2,
    )
    incomplete = await world.sync()
    assert outcomes(world, incomplete.run_id) == {"prebid/Prebid.js": "incomplete_index"}
    assert incomplete.accepted_not_failed == ()


# --------------------------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------------------------
def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_a_dry_run_writes_nothing_and_reports_the_plan(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=3)
    repository.pulls[1].set(isDraft=True)
    await world.sync()
    scenario.rename_repository(repository, "Prebid2.js")
    repository.pulls[1].set(isDraft=False)
    repository.pulls[2].add("comments", scenario.node("comments", by=author))
    repository.pulls[3].gone = True
    scenario.open_pull_request(repository, author)
    before = digest(world.database)

    dry = await world.sync(dry_run=True)

    assert digest(world.database) == before
    assert (dry.dry_run, dry.run_id, dry.status, dry.exit_code) == (True, None, None, 0)
    (entry,) = dry.repositories
    assert (entry.stored_full_name, entry.full_name, entry.outcome) == (
        "prebid/Prebid.js",
        "prebid/Prebid2.js",
        "complete",
    )
    assert [(item.number, item.reasons) for item in dry.planned] == [
        (2, frozenset({Reason.UPDATED_AT})),
        (4, frozenset({Reason.NOT_STORED})),
    ]
    assert [(u.number, u.is_draft) for u in dry.in_place_updates] == [(1, False)]
    (deletions,) = dry.deletions
    assert [c.number for c in deletions.candidates] == [3]

    run = await world.sync()
    assert planned(run) == planned(dry)
    assert [c.number for c in run.deletions[0].candidates] == [3]
    assert world.numbers(repository) == [1, 2, 4]
    assert ended(world, run) == ("complete", 0, {"prebid/Prebid2.js": "complete"})


async def failed_resolve(world: World) -> None:
    project(world)
    world.track("prebid/missing")


async def incomplete_index(world: World) -> None:
    project(world)
    world.scenario.replay.modify(
        lambda body: body["data"]["repository"]["pullRequests"].update(totalCount=99),
        when=lambda seen: seen.operation == "PullRequestIndex",
        times=4,
    )


async def untracked(world: World) -> None:
    project(world)
    project(world, name="dropped")
    await world.sync()
    world.untrack("prebid/dropped")


async def halted(world: World) -> None:
    repository, _ = project(world, pulls=30)
    await world.sync()
    for number in range(1, 28):
        repository.pulls[number].gone = True


async def refused_prune(world: World) -> None:
    project(world)
    world.track("prebid/missing")
    world.options = {"prune_untracked": True}


async def planner_failure(world: World) -> None:
    project(world)
    world.scenario.replay.modify(
        lambda body: body["data"]["repository"]["pullRequests"]["nodes"][0].update(state="MERGED"),
        when=lambda seen: seen.operation == "PullRequestIndex",
        times=2,
    )


@pytest.mark.parametrize(
    "condition",
    [failed_resolve, incomplete_index, untracked, halted, refused_prune, planner_failure],
)
async def test_a_dry_run_exits_as_the_run_would(world: World, condition: Any) -> None:
    await condition(world)

    dry = await world.sync(dry_run=True, **world.options)
    run = await world.sync(**world.options)

    assert (dry.exit_code, run.exit_code) == (2, 2)
    assert [r.outcome for r in dry.repositories] == [r.outcome for r in run.repositories]
    assert dry.untracked == run.untracked
    assert dry.prune_refused == run.prune_refused
    assert [(f.number, f.error) for f in dry.failures] == [
        (f.number, f.error) for f in run.failures
    ]


async def test_a_dry_run_takes_no_lock_and_never_blocks_a_commit(world: World) -> None:
    scenario = world.scenario
    project(world)
    await world.sync()
    world.execute(
        "INSERT INTO sync_runs (started_at, status, trigger, code_version, config_source, "
        "config_sha256, graphql_points, rest_calls, items_planned, items_hydrated, "
        "items_deleted, failures) VALUES ('2026-10-17', 'running', 'manual', 'x', 'test', "
        "'0', 0, 0, 0, 0, 0, 0)"
    )
    committed: list[str] = []

    def commit_without_waiting() -> None:
        connection = sqlite3.connect(world.database, timeout=0)
        try:
            with connection:
                connection.execute(
                    "UPDATE sync_runs SET code_version = 'y' WHERE status = 'running'"
                )
            committed.append("committed")
        finally:
            connection.close()

    scenario.after("PullRequestIndex", commit_without_waiting)

    dry = await world.sync(dry_run=True)

    assert committed == ["committed"]
    assert dry.exit_code == 0
    with pytest.raises(ClaimRefused):
        await world.sync()


async def test_a_dry_run_stops_at_the_deadline_during_its_index(world: World) -> None:
    scenario = world.scenario
    project(world)
    project(world, name="later")
    deadline_after(scenario, "PullRequestIndex")

    dry = await world.sync(dry_run=True, max_minutes=1)

    assert dry.exit_code == 2
    assert [(r.outcome, r.reason) for r in dry.repositories] == [
        ("complete", None),
        ("incomplete_index", "the deadline came before its index finished"),
    ]
    assert world.rows("SELECT id FROM sync_runs") == []


# --------------------------------------------------------------------------------------------
# What the run reports
# --------------------------------------------------------------------------------------------
async def test_a_second_node_id_is_reported_and_the_stored_one_kept(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world)
    await world.sync()
    repository.pulls[1].node["author"]["id"] = "U_second"
    repository.pulls[1].touch()

    report = await world.sync()

    database_id = scenario.account(author)["databaseId"]
    assert [
        (n.github_id, n.stored_node_id, n.reported_node_id) for n in report.second_node_ids
    ] == [(database_id, author, "U_second")]
    assert world.rows("SELECT node_id FROM github_users") == [{"node_id": author}]
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


@pytest.mark.parametrize("failure", ["server error", "another databaseId"])
async def test_a_failed_refresh_keeps_the_stored_values_and_the_exit_code(
    world: World, failure: str
) -> None:
    scenario = world.scenario
    _, author = project(world)
    await world.sync()
    stored = account_row(world, author)
    scenario.clock.advance(24 * 3600)
    scenario.account(author)["login"] = "renamed"
    if failure == "server error":
        failing_lookup(scenario, ids=1)
    else:
        scenario.replay.modify(
            lambda body: body["data"]["nodes"][0].update(databaseId=42),
            when=lambda seen: seen.operation == "UserLookup",
        )

    report = await world.sync()

    (message,) = report.refresh_failures
    assert message.startswith("UserLookupFailed" if failure == "server error" else "LookupConflict")
    assert account_row(world, author) == stored
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})
    retried = await world.sync()
    assert account_row(world, author)["login"] == "renamed"
    assert retried.refresh_failures == ()


async def test_break_lock_aborts_a_blocking_run_and_proceeds(world: World) -> None:
    project(world)
    world.execute(
        "INSERT INTO sync_runs (started_at, status, trigger, code_version, config_source, "
        "config_sha256, graphql_points, rest_calls, items_planned, items_hydrated, "
        "items_deleted, failures) VALUES ('2026-10-17', 'running', 'manual', 'x', 'test', "
        "'0', 0, 0, 0, 0, 0, 0)"
    )
    blocking = world.value("SELECT id FROM sync_runs")

    with pytest.raises(ClaimRefused):
        await world.sync()
    report = await world.sync(break_lock=True)

    assert report.broken == (blocking,)
    assert world.run(blocking)["status"] == "aborted"
    assert ended(world, report) == ("complete", 0, {"prebid/Prebid.js": "complete"})


async def test_a_cancelled_run_closes_aborted_and_re_raises(world: World) -> None:
    scenario = world.scenario
    project(world)
    run = asyncio.create_task(world.sync())

    def cancel(request: Any) -> Any:
        run.cancel()
        return synthetic("bad_gateway")

    scenario.replay.fault(cancel, when=lambda seen: seen.operation == "PullRequestIndex")

    with pytest.raises(asyncio.CancelledError):
        await run

    (row,) = world.rows("SELECT id, status, exit_code FROM sync_runs")
    assert (row["status"], row["exit_code"]) == ("aborted", 130)
    assert outcomes(world, row["id"]) == {"prebid/Prebid.js": "failed"}
