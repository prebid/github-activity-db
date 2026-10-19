"""Step 7's candidates, delete guard, and ``--allow-deletions`` (plan 5.3 step 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from github_activity_db.source.records import IndexedPullRequest
from github_activity_db.sync.deletions import (
    AllowDeletionsError,
    Candidate,
    DeletionApproval,
    Deletions,
    GuardDecision,
    guard,
    guard_limit,
    item_deletions,
    parse_allow_deletions,
)
from github_activity_db.sync.plan import AttemptedRepository, Coverage, plan
from github_activity_db.sync.snapshot import StoredPullRequest, StoredRepository
from github_activity_db.tracking import SyncConfig, TrackedRepository, TrackingConfig
from tests.db.seed import Seed, connect
from tests.sync.support import HYDRATED, UPDATED, config, entry, index, snapshot_of, stored_pr

DEFAULT = SyncConfig()
JS_ID = 11


def _rows(count: int, start: int = 1) -> tuple[StoredPullRequest, ...]:
    return tuple(
        StoredPullRequest(
            row_id=n,
            github_id=1000 + n,
            number=n,
            state="OPEN",
            is_draft=False,
            author_association="MEMBER",
            github_updated_at=UPDATED,
            hydrated_at=HYDRATED,
            hydration_version=2,
            closing_issues_total=0,
            labels=frozenset(),
            closing_links=frozenset(),
        )
        for n in range(start, start + count)
    )


def _stored(
    rows: tuple[StoredPullRequest, ...], failures: frozenset[tuple[str, int]] = frozenset()
) -> StoredRepository:
    return StoredRepository(
        row_id=1,
        github_id=JS_ID,
        full_name="prebid/Prebid.js",
        is_tracked=True,
        pull_requests=rows,
        failures=failures,
        pending=frozenset(),
    )


def _coverage(rows: tuple[StoredPullRequest, ...]) -> Coverage:
    return Coverage(
        repository_github_id=JS_ID,
        kind="pull_request",
        github_ids=frozenset(row.github_id for row in rows),
        numbers=frozenset(row.number for row in rows),
    )


# --------------------------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("stored_rows", "limit"),
    [(0, 25), (100, 25), (1250, 25), (1300, 26), (1310, 26), (1349, 26), (1350, 27), (2000, 40)],
)
def test_the_guard_limit_is_max_25_and_2_percent(stored_rows: int, limit: int) -> None:
    assert guard_limit(stored_rows, DEFAULT) == limit


@pytest.mark.parametrize(
    ("min_rows", "percent", "stored_rows", "limit"),
    [
        (0, 2.0, 100, 2),
        (0, 2.0, 149, 2),
        (0, 2.0, 150, 3),
        (10, 50.0, 10, 10),
        (10, 50.0, 30, 15),
        (0, 100.0, 7, 7),
        # Exact decimal arithmetic: 0.57 * 10000 / 100 in floating point is 56.99... (56).
        (0, 0.57, 10000, 57),
    ],
)
def test_the_guard_limit_reads_the_configuration(
    min_rows: int, percent: float, stored_rows: int, limit: int
) -> None:
    settings = SyncConfig(delete_guard_min_rows=min_rows, delete_guard_percent=percent)
    assert guard_limit(stored_rows, settings) == limit


@pytest.mark.parametrize(
    ("candidates", "stored_rows", "runs"),
    [
        (0, 0, True),
        (25, 100, True),
        (26, 100, False),
        (26, 1310, True),
        (27, 1310, False),
        (40, 2000, True),
        (41, 2000, False),
    ],
)
def test_the_guard_on_each_side_of_its_limit(candidates: int, stored_rows: int, runs: bool) -> None:
    decision = guard(candidates, stored_rows, DEFAULT, None)
    assert decision.runs is runs
    assert (decision.halt_reason is None) is runs


@pytest.mark.parametrize(
    ("approved", "runs"),
    [(None, False), (29, False), (30, True), (31, True)],
    ids=["no-approval", "under", "at", "over"],
)
def test_allow_deletions_under_at_and_over_the_candidates(approved: int | None, runs: bool) -> None:
    """30 candidates exceed the guard's 25 for 100 stored rows."""
    decision = guard(30, 100, DEFAULT, approved)
    assert decision == GuardDecision(
        candidates=30, stored_rows=100, limit=25, approved_limit=approved
    )
    assert decision.runs is runs


def test_allow_deletions_below_the_guard_changes_nothing() -> None:
    """N under the candidates while the guard lets them through: they run as without the flag."""
    assert guard(20, 100, DEFAULT, 5).runs


@pytest.mark.parametrize(
    ("approved", "reason"),
    [
        (
            None,
            "30 deletion candidates exceed the delete guard's 25 (100 stored rows before the run)",
        ),
        (
            29,
            "30 deletion candidates exceed the delete guard's 25 (100 stored rows before the run) "
            "and --allow-deletions 29",
        ),
    ],
)
def test_the_halt_reason(approved: int | None, reason: str) -> None:
    assert guard(30, 100, DEFAULT, approved).halt_reason == reason


# --------------------------------------------------------------------------------------------
# Candidates
# --------------------------------------------------------------------------------------------
def test_candidates_are_the_stored_items_absent_from_coverage() -> None:
    rows = _rows(5)
    result = item_deletions(
        _stored(rows),
        _coverage(rows[:3]),
        failed_during_hydration=False,
        stored_rows_before_run=5,
        config=DEFAULT,
        approved_limit=None,
    )
    assert result == Deletions(
        repository_github_id=JS_ID,
        candidates=(Candidate("pull_request", 4, 1004, 4), Candidate("pull_request", 5, 1005, 5)),
        decision=GuardDecision(candidates=2, stored_rows=5, limit=25, approved_limit=None),
        failures_removed=(),
    )


def test_no_candidates_when_every_stored_item_is_covered() -> None:
    rows = _rows(3)
    result = item_deletions(
        _stored(rows),
        _coverage(rows),
        failed_during_hydration=False,
        stored_rows_before_run=3,
        config=DEFAULT,
        approved_limit=None,
    )
    assert result is not None
    assert (result.candidates, result.decision.runs) == ((), True)


def test_a_repository_with_nothing_stored() -> None:
    result = item_deletions(
        None,
        _coverage(()),
        failed_during_hydration=False,
        stored_rows_before_run=0,
        config=DEFAULT,
        approved_limit=None,
    )
    assert result == Deletions(
        repository_github_id=JS_ID,
        candidates=(),
        decision=GuardDecision(candidates=0, stored_rows=0, limit=25, approved_limit=None),
        failures_removed=(),
    )


def test_the_guard_counts_the_rows_before_the_run() -> None:
    """2,000 rows after the writes would let 40 through; the 100 before the run allow 25."""
    rows = _rows(2000)
    result = item_deletions(
        _stored(rows),
        _coverage(rows[26:]),
        failed_during_hydration=False,
        stored_rows_before_run=100,
        config=DEFAULT,
        approved_limit=None,
    )
    assert result is not None
    assert len(result.candidates) == 26
    assert result.decision.runs is False


@pytest.mark.parametrize("approved", [None, 30])
def test_failure_rows_absent_from_coverage_are_removed_under_a_halt_too(
    approved: int | None,
) -> None:
    rows = _rows(130)
    failures = frozenset(
        {("pull_request", 101), ("pull_request", 7), ("pull_request", 500), ("issue", 500)}
    )
    result = item_deletions(
        _stored(rows, failures),
        _coverage(rows[:100]),
        failed_during_hydration=False,
        stored_rows_before_run=130,
        config=DEFAULT,
        approved_limit=approved,
    )
    assert result is not None
    assert len(result.candidates) == 30
    assert result.decision.runs is (approved is not None)
    assert result.failures_removed == (("pull_request", 101), ("pull_request", 500))


@pytest.mark.parametrize(
    ("coverage", "failed"),
    [(None, False), (None, True), ("complete", True)],
    ids=["incomplete-index", "incomplete-and-failed", "failed-during-hydration"],
)
def test_step_7_skips_a_repository(coverage: str | None, failed: bool) -> None:
    rows = _rows(3)
    result = item_deletions(
        _stored(rows, frozenset({("pull_request", 9)})),
        _coverage(rows[:1]) if coverage else None,
        failed_during_hydration=failed,
        stored_rows_before_run=3,
        config=DEFAULT,
        approved_limit=None,
    )
    assert result is None


def test_only_pull_request_coverage_is_read() -> None:
    issues = Coverage(JS_ID, "issue", frozenset(), frozenset())
    with pytest.raises(ValueError, match="no stored issue rows are read before Step 4"):
        item_deletions(
            _stored(_rows(1)),
            issues,
            failed_during_hydration=False,
            stored_rows_before_run=1,
            config=DEFAULT,
            approved_limit=None,
        )


def test_the_control_for_skipping_lists_candidates() -> None:
    """The same inputs as the skipped cases, completely indexed and not failed."""
    rows = _rows(3)
    result = item_deletions(
        _stored(rows, frozenset({("pull_request", 9)})),
        _coverage(rows[:1]),
        failed_during_hydration=False,
        stored_rows_before_run=3,
        config=DEFAULT,
        approved_limit=None,
    )
    assert result is not None
    assert [c.number for c in result.candidates] == [2, 3]
    assert result.failures_removed == (("pull_request", 9),)


async def test_candidates_from_the_planner_and_a_snapshot(migrated_database: Path) -> None:
    """Over a seeded database: a contradicted entry's stored row stays, a number the index lists
    under another github_id leaves, an item no longer indexed leaves, and the failure row of an
    item outside coverage is removed while a covered one stays."""
    ids: dict[str, int] = {}
    with connect(migrated_database) as connection:
        seed = Seed(connection)
        repo = seed.repository("prebid/Prebid.js", github_id=JS_ID)
        ids["kept"] = stored_pr(seed, repo, 1, 10)
        ids["contradicted"] = stored_pr(seed, repo, 2, 20)
        ids["renumbered"] = stored_pr(seed, repo, 3, 30)
        ids["gone"] = stored_pr(seed, repo, 4, 40)
        seed.failure(repo, 2)
        seed.failure(repo, 4)
        seed.failure(repo, 8)
    snapshot = await snapshot_of(migrated_database)
    entries = (entry(1, 10), entry(2, 20, state="CLOSED"), entry(3, 31))
    result = plan(snapshot, [_attempted(entries)], config())
    deletions = item_deletions(
        snapshot.repository(JS_ID),
        result.coverage_of(JS_ID, "pull_request"),
        failed_during_hydration=False,
        stored_rows_before_run=4,
        config=DEFAULT,
        approved_limit=None,
    )
    assert deletions is not None
    assert deletions.candidates == (
        Candidate("pull_request", ids["renumbered"], 30, 3),
        Candidate("pull_request", ids["gone"], 40, 4),
    )
    assert deletions.failures_removed == (("pull_request", 4), ("pull_request", 8))
    assert deletions.decision.runs


def _attempted(entries: tuple[IndexedPullRequest, ...]) -> AttemptedRepository:
    return AttemptedRepository(
        github_id=JS_ID,
        full_name="prebid/Prebid.js",
        labels=frozenset(),
        index=index("prebid/Prebid.js", *entries),
    )


# --------------------------------------------------------------------------------------------
# --allow-deletions owner/name=N
# --------------------------------------------------------------------------------------------
TWO_REPOSITORIES = TrackingConfig.model_validate(
    {
        "coverage": {"floor": UPDATED},
        "repositories": [
            {"owner": "prebid", "name": "Prebid.js", "group": "Client"},
            {"owner": "prebid", "name": "prebid-server", "group": "Server"},
        ],
    }
)
JS, SERVER = TWO_REPOSITORIES.repositories


@pytest.mark.parametrize(
    ("value", "repository", "limit"),
    [
        ("prebid/Prebid.js=3", JS, 3),
        ("PREBID/prebid.JS=3", JS, 3),
        ("prebid/prebid-server=1", SERVER, 1),
        ("prebid/Prebid.js=007", JS, 7),
        ("prebid/Prebid.js=120000", JS, 120000),
    ],
)
def test_parse_allow_deletions(value: str, repository: TrackedRepository, limit: int) -> None:
    assert parse_allow_deletions(value, TWO_REPOSITORIES) == DeletionApproval(repository, limit)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("prebid/Prebid.js=0", "N of at least 1, not 0"),
        ("prebid/Prebid.js=000", "N of at least 1, not 0"),
        ("prebid/Prebid.js=-1", "takes owner/name=N"),
        ("prebid/Prebid.js=+3", "takes owner/name=N"),
        ("prebid/Prebid.js= 3", "takes owner/name=N"),
        ("prebid/Prebid.js=3 ", "takes owner/name=N"),
        ("prebid/Prebid.js=3\n", "takes owner/name=N"),
        ("prebid/Prebid.js=1.5", "takes owner/name=N"),
        ("prebid/Prebid.js=٣", "takes owner/name=N"),
        ("prebid/Prebid.js", "takes owner/name=N"),
        ("prebid/Prebid.js=", "takes owner/name=N"),
        ("=3", "takes owner/name=N"),
        ("", "takes owner/name=N"),
        ("prebid/Prebid.js=3=4", "takes owner/name=N"),
        (" prebid/Prebid.js=3", "which tracking.toml does not list"),
        ("prebid/go-gdpr=3", "which tracking.toml does not list"),
        ("Prebid.js=3", "which tracking.toml does not list"),
    ],
)
def test_parse_allow_deletions_refuses(value: str, message: str) -> None:
    with pytest.raises(AllowDeletionsError, match=message.replace("+", r"\+")):
        parse_allow_deletions(value, TWO_REPOSITORIES)


def test_an_approval_applies_to_its_repository_only() -> None:
    approval = parse_allow_deletions("prebid/prebid.js=4", TWO_REPOSITORIES)
    assert approval.limit_for(JS) == 4
    assert approval.limit_for(SERVER) is None
