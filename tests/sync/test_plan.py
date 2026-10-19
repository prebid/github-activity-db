"""The planner of plan 5.3 step 3, over snapshots read from migrated, seeded databases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from github_activity_db.source import GitHubSource
from github_activity_db.source.documents import PULL_REQUEST_HYDRATION_VERSION
from github_activity_db.source.records import IndexedPullRequest, PullRequestIndex
from github_activity_db.sync.plan import (
    AttemptedRepository,
    Batch,
    Coverage,
    InPlaceUpdate,
    NumberStoredUnderAnotherId,
    Plan,
    PlannedItem,
    Reason,
    StateContradictsDates,
    batches,
    plan,
)
from github_activity_db.tracking import TrackingConfig
from tests.db.seed import Seed, connect
from tests.source.conftest import FakeClock
from tests.source.replay import Replay
from tests.sync.support import (
    FLOOR,
    UPDATED,
    closed,
    config,
    entry,
    index,
    link,
    merged,
    snapshot_of,
    stored_pr,
)

JS, JS_ID = "prebid/Prebid.js", 11
SERVER, SERVER_ID = "prebid/prebid-server", 12
BUG, FEATURE = ("LA_1", "bug"), ("LA_2", "feature")
LABELS = frozenset({BUG, FEATURE})
V = PULL_REQUEST_HYDRATION_VERSION
SECOND = timedelta(seconds=1)


def attempted(
    *entries: IndexedPullRequest,
    github_id: int = JS_ID,
    full_name: str = JS,
    labels: frozenset[tuple[str, str]] = LABELS,
    complete: bool = True,
) -> AttemptedRepository:
    return AttemptedRepository(
        github_id=github_id,
        full_name=full_name,
        labels=labels,
        index=index(full_name, *entries) if complete else None,
    )


async def plan_of(
    database: Path, *repositories: AttemptedRepository, settings: TrackingConfig | None = None
) -> Plan:
    return plan(await snapshot_of(database), repositories, settings or config())


def item(
    number: int,
    github_id: int,
    reasons: set[Reason],
    *,
    repository: tuple[int, str] = (JS_ID, JS),
    is_open: bool = True,
    updated_at: datetime = UPDATED,
    pending: bool = False,
) -> PlannedItem:
    return PlannedItem(
        repository_github_id=repository[0],
        repository=repository[1],
        kind="pull_request",
        number=number,
        github_id=github_id,
        is_open=is_open,
        updated_at=updated_at,
        pending=pending,
        reasons=frozenset(reasons),
    )


def _js(database: Path, build: Callable[[Seed, int], object] | None = None) -> int:
    """Seed Prebid.js with the baseline label set in ``repo_labels``, then ``build``."""
    with connect(database) as connection:
        seed = Seed(connection)
        repository = seed.repository(JS, github_id=JS_ID)
        seed.repo_label(repository, *BUG)
        seed.repo_label(repository, *FEATURE)
        if build is not None:
            build(seed, repository)
    return repository


# --------------------------------------------------------------------------------------------
# Each hydration condition alone, and its absence
# --------------------------------------------------------------------------------------------
# The baseline: #5, stored settled at the code's version with label "bug" and one closing link,
# and an index entry equal to it. The stored link's repository name differs from the index's
# ("owner/repo-777" against "owner/repo"): a link is keyed by repository id and number.
BASE_STORED: dict[str, Any] = {"labels": [BUG], "links": [1]}
BASE_ENTRY: dict[str, Any] = {"links": [link(1)]}
TEN = [link(n) for n in range(1, 11)]

CONDITIONS: list[tuple[str, dict[str, Any], dict[str, Any], frozenset[Any], set[Reason]]] = [
    ("unchanged", {}, {}, LABELS, set()),
    ("updated-later", {}, {"updated_at": UPDATED + SECOND}, LABELS, {Reason.UPDATED_AT}),
    ("updated-earlier", {}, {"updated_at": UPDATED - SECOND}, LABELS, {Reason.UPDATED_AT}),
    ("open-to-closed", {}, {"state": "CLOSED", "closed_at": UPDATED}, LABELS, {Reason.STATE}),
    (
        "open-to-merged",
        {},
        {"state": "MERGED", "merged_at": UPDATED, "closed_at": UPDATED},
        LABELS,
        {Reason.STATE},
    ),
    ("reopened", {"state": "CLOSED", "closed_at": UPDATED}, {}, LABELS, {Reason.STATE}),
    ("version-below", {"hydration_version": V - 1}, {}, LABELS, {Reason.HYDRATION_VERSION}),
    ("version-above", {"hydration_version": V + 1}, {}, LABELS, {Reason.HYDRATION_VERSION}),
    (
        "unsettled-inside-the-window",
        {"hydrated_at": UPDATED + timedelta(minutes=10, microseconds=-1)},
        {},
        LABELS,
        {Reason.UNSETTLED},
    ),
    ("settled-at-the-window", {"hydrated_at": UPDATED + timedelta(minutes=10)}, {}, LABELS, set()),
    (
        "hydrated-before-updated",
        {"hydrated_at": UPDATED - SECOND},
        {},
        LABELS,
        {Reason.UNSETTLED},
    ),
    ("label-deleted", {}, {}, frozenset({FEATURE}), {Reason.LABEL_GONE}),
    ("label-renamed", {}, {}, frozenset({("LA_1", "defect"), FEATURE}), {Reason.LABEL_GONE}),
    (
        "label-recreated-under-its-name",
        {},
        {},
        frozenset({("LA_9", "bug"), FEATURE}),
        {Reason.LABEL_GONE},
    ),
    ("no-labels", {"labels": []}, {}, frozenset(), set()),
    (
        "an-eleventh-link",
        {"links": range(1, 11)},
        {"links": TEN, "links_total": 11},
        LABELS,
        {Reason.LINK_TOTAL},
    ),
    (
        "an-indexed-link-not-stored",
        {"links": [*range(1, 10), 11, 12, 13]},
        {"links": TEN, "links_total": 12},
        LABELS,
        {Reason.LINK_NOT_STORED},
    ),
    (
        "a-stored-link-not-indexed",
        {"links": [1, 2], "closing_issues_total": 1},
        {},
        LABELS,
        {Reason.LINK_NOT_INDEXED},
    ),
    (
        "a-link-replaced",
        {},
        {"links": [link(2)]},
        LABELS,
        {Reason.LINK_NOT_STORED, Reason.LINK_NOT_INDEXED},
    ),
    (
        "a-link-to-another-repository-with-the-same-number",
        {},
        {"links": [link(1, repo_github_id=778)]},
        LABELS,
        {Reason.LINK_NOT_STORED, Reason.LINK_NOT_INDEXED},
    ),
    (
        "a-stored-link-past-the-ten-indexed",
        {"links": range(1, 12)},
        {"links": TEN, "links_total": 11},
        LABELS,
        set(),
    ),
    (
        "a-link-listed-twice",
        {"links": [1, 2]},
        {"links": [link(1), link(1)], "links_total": 2},
        LABELS,
        {Reason.LINK_REPEATED},
    ),
    (
        "a-link-listed-twice-among-ten-of-fifteen",
        {"links": range(1, 16)},
        {"links": [*TEN[:9], link(1)], "links_total": 15},
        LABELS,
        {Reason.LINK_REPEATED},
    ),
    (
        "ten-distinct-links-of-fifteen",
        {"links": range(1, 16)},
        {"links": TEN, "links_total": 15},
        LABELS,
        set(),
    ),
]


@pytest.mark.parametrize(
    ("stored", "indexed", "labels", "reasons"),
    [case[1:] for case in CONDITIONS],
    ids=[case[0] for case in CONDITIONS],
)
async def test_each_condition_alone(
    migrated_database: Path,
    stored: dict[str, Any],
    indexed: dict[str, Any],
    labels: frozenset[tuple[str, str]],
    reasons: set[Reason],
) -> None:
    _js(
        migrated_database,
        lambda seed, repo: stored_pr(seed, repo, 5, 50, **{**BASE_STORED, **stored}),
    )
    the_entry = entry(5, 50, **{**BASE_ENTRY, **indexed})
    result = await plan_of(migrated_database, attempted(the_entry, labels=labels))
    expected = (
        (item(5, 50, reasons, is_open=the_entry.state == "OPEN", updated_at=the_entry.updated_at),)
        if reasons
        else ()
    )
    assert result.queue == expected
    assert result.in_place_updates == ()
    assert result.failures == ()


@pytest.mark.parametrize(
    ("minutes", "hydrated_after", "reasons"),
    [
        (1, timedelta(seconds=59), {Reason.UNSETTLED}),
        (1, timedelta(seconds=60), set()),
        (30, timedelta(minutes=29, seconds=59), {Reason.UNSETTLED}),
        (30, timedelta(minutes=30), set()),
    ],
)
async def test_the_unsettled_window_comes_from_the_configuration(
    migrated_database: Path, minutes: int, hydrated_after: timedelta, reasons: set[Reason]
) -> None:
    _js(
        migrated_database,
        lambda seed, repo: stored_pr(seed, repo, 5, 50, hydrated_at=UPDATED + hydrated_after),
    )
    result = await plan_of(
        migrated_database, attempted(entry(5, 50)), settings=config(unsettled_minutes=minutes)
    )
    assert result.queue == ((item(5, 50, reasons),) if reasons else ())


@pytest.mark.parametrize(
    ("kind", "number", "reasons"),
    [
        ("pull_request", 5, {Reason.FAILED}),
        ("issue", 5, set()),
        ("pull_request", 6, set()),
    ],
    ids=["this-item", "an-issue-with-the-number", "another-number"],
)
async def test_a_sync_failures_row(
    migrated_database: Path, kind: str, number: int, reasons: set[Reason]
) -> None:
    def build(seed: Seed, repo: int) -> None:
        stored_pr(seed, repo, 5, 50)
        seed.failure(repo, number, kind=kind)

    _js(migrated_database, build)
    result = await plan_of(migrated_database, attempted(entry(5, 50)))
    assert result.queue == ((item(5, 50, reasons),) if reasons else ())


async def test_a_label_is_read_from_the_resolve_not_from_repo_labels(
    migrated_database: Path,
) -> None:
    """The stored ``repo_labels`` lack "bug"; step 1's resolve still has it."""
    with connect(migrated_database) as connection:
        seed = Seed(connection)
        repo = seed.repository(JS, github_id=JS_ID)
        seed.repo_label(repo, *FEATURE)
        stored_pr(seed, repo, 5, 50, labels=[BUG])
    result = await plan_of(migrated_database, attempted(entry(5, 50), labels=LABELS))
    assert result.queue == ()


@pytest.mark.parametrize(
    ("build", "the_entry", "reasons"),
    [
        (None, entry(5, 50), {Reason.NOT_STORED}),
        (
            lambda seed, repo: seed.failure(repo, 5),
            entry(5, 50),
            {Reason.NOT_STORED, Reason.FAILED},
        ),
        (
            None,
            entry(5, 50, links=[link(1), link(1)], links_total=2),
            {Reason.NOT_STORED, Reason.LINK_REPEATED},
        ),
    ],
    ids=["alone", "with-a-failure-row", "with-a-repeated-link"],
)
async def test_an_item_not_stored(
    migrated_database: Path,
    build: Callable[[Seed, int], object] | None,
    the_entry: IndexedPullRequest,
    reasons: set[Reason],
) -> None:
    _js(migrated_database, build)
    result = await plan_of(migrated_database, attempted(the_entry))
    assert result.queue == (item(5, 50, reasons),)


async def test_an_item_of_a_repository_not_stored(migrated_database: Path) -> None:
    result = await plan_of(migrated_database, attempted(entry(5, 50)))
    assert result.queue == (item(5, 50, {Reason.NOT_STORED}),)
    assert result.coverage == (Coverage(JS_ID, "pull_request", frozenset({50}), frozenset({5})),)


# --------------------------------------------------------------------------------------------
# The coverage rule
# --------------------------------------------------------------------------------------------
OLD = datetime(2016, 4, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("the_entry", "covered"),
    [
        (entry(1, 10, created_at=OLD, updated_at=OLD), True),
        (merged(1, 10, FLOOR, created_at=OLD), True),
        (merged(1, 10, FLOOR - SECOND, created_at=OLD, updated_at=FLOOR - SECOND), False),
        (merged(1, 10, FLOOR - SECOND, created_at=OLD, updated_at=UPDATED), False),
        (closed(1, 10, FLOOR, created_at=OLD), True),
        (closed(1, 10, FLOOR - SECOND, created_at=OLD), False),
        (closed(1, 10, UPDATED, created_at=OLD), True),
    ],
    ids=[
        "open-created-before-the-floor",
        "merged-at-the-floor",
        "merged-before-the-floor",
        "merged-before-the-floor-and-updated-after",
        "closed-at-the-floor",
        "closed-before-the-floor",
        "created-before-the-floor-and-closed-after",
    ],
)
async def test_the_coverage_rule(
    migrated_database: Path, the_entry: IndexedPullRequest, covered: bool
) -> None:
    result = await plan_of(migrated_database, attempted(the_entry))
    assert [planned.number for planned in result.queue] == ([1] if covered else [])
    expected = frozenset({1}) if covered else frozenset()
    assert result.coverage == (
        Coverage(JS_ID, "pull_request", frozenset({10}) if covered else frozenset(), expected),
    )


@pytest.mark.parametrize(
    ("merged_at", "covered"),
    [
        (datetime(2024, 6, 1, tzinfo=UTC), True),
        (datetime(2024, 5, 31, 23, 59, 59, tzinfo=UTC), False),
    ],
)
async def test_the_floor_comes_from_the_configuration(
    migrated_database: Path, merged_at: datetime, covered: bool
) -> None:
    settings = config(floor=datetime(2024, 6, 1, tzinfo=UTC))
    result = await plan_of(
        migrated_database, attempted(merged(1, 10, merged_at)), settings=settings
    )
    assert [planned.number for planned in result.queue] == ([1] if covered else [])


async def test_a_stored_item_outside_coverage_is_not_hydrated(migrated_database: Path) -> None:
    """It is not planned whatever differs; it is updated in place like any stored item the run
    does not hydrate, and it is outside the in-coverage set, so step 7 lists it."""
    old = FLOOR - timedelta(days=1)
    ids: dict[str, int] = {}
    _js(
        migrated_database,
        lambda seed, repo: ids.setdefault(
            "pr",
            stored_pr(
                seed,
                repo,
                9,
                90,
                state="MERGED",
                merged_at=old,
                closed_at=old,
                hydration_version=V - 1,
            ),
        ),
    )
    the_entry = merged(9, 90, old, updated_at=UPDATED + SECOND, is_draft=True)
    result = await plan_of(migrated_database, attempted(the_entry))
    assert result.queue == ()
    assert result.in_place_updates == (
        InPlaceUpdate(JS_ID, "pull_request", ids["pr"], 9, True, "MEMBER"),
    )
    assert result.coverage == (Coverage(JS_ID, "pull_request", frozenset(), frozenset()),)


# --------------------------------------------------------------------------------------------
# Failures the planner detects
# --------------------------------------------------------------------------------------------
CONTRADICTIONS = [
    entry(5, 50, state="CLOSED"),
    entry(5, 50, state="MERGED", closed_at=UPDATED),
    entry(5, 50, merged_at=UPDATED),
    entry(5, 50, state="CLOSED", merged_at=UPDATED, closed_at=UPDATED),
    entry(5, 50, merged_at=OLD, created_at=OLD, updated_at=OLD),
]
CONTRADICTION_IDS = [
    "closed-without-closed-at",
    "merged-without-merged-at",
    "open-with-merged-at",
    "closed-with-merged-at",
    "open-with-merged-at-before-the-floor",
]


@pytest.mark.parametrize("the_entry", CONTRADICTIONS, ids=CONTRADICTION_IDS)
async def test_an_entry_whose_state_its_dates_contradict(
    migrated_database: Path, the_entry: IndexedPullRequest
) -> None:
    """A failure, never planned although the item is not stored and has a failure row, and in
    coverage."""
    _js(migrated_database, lambda seed, repo: seed.failure(repo, 5))
    result = await plan_of(migrated_database, attempted(the_entry))
    assert result.queue == ()
    (failure,) = result.failures
    assert (failure.repository_github_id, failure.repository, failure.kind, failure.number) == (
        JS_ID,
        JS,
        "pull_request",
        5,
    )
    assert type(failure.error) is StateContradictsDates
    assert result.coverage == (Coverage(JS_ID, "pull_request", frozenset({50}), frozenset({5})),)


@pytest.mark.parametrize("the_entry", CONTRADICTIONS, ids=CONTRADICTION_IDS)
async def test_a_stored_item_whose_index_state_its_dates_contradict(
    migrated_database: Path, the_entry: IndexedPullRequest
) -> None:
    """Every condition selects it and its draft flag differs; it is a failure, neither planned
    nor updated in place."""
    _js(
        migrated_database,
        lambda seed, repo: stored_pr(seed, repo, 5, 50, hydration_version=V - 1, is_draft=True),
    )
    contradicted = replace(the_entry, updated_at=UPDATED + SECOND)
    result = await plan_of(migrated_database, attempted(contradicted))
    assert (result.queue, result.in_place_updates) == ((), ())
    assert [type(failure.error) for failure in result.failures] == [StateContradictsDates]


def test_the_contradiction_message() -> None:
    error = StateContradictsDates(entry(5, 50, state="CLOSED"))
    assert str(error) == "#5: the index lists state CLOSED with mergedAt None and closedAt None"
    assert error.number == 5


async def test_a_number_stored_under_another_github_id(migrated_database: Path) -> None:
    _js(migrated_database, lambda seed, repo: stored_pr(seed, repo, 5, 50, is_draft=True))
    result = await plan_of(migrated_database, attempted(entry(5, 51)))
    assert (result.queue, result.in_place_updates) == ((), ())
    (failure,) = result.failures
    assert isinstance(failure.error, NumberStoredUnderAnotherId)
    assert (failure.number, failure.error.indexed_github_id, failure.error.stored_github_id) == (
        5,
        51,
        50,
    )
    assert str(failure.error) == (
        "#5: the index lists github_id 51, the database stores this number under 50"
    )
    assert result.coverage == (Coverage(JS_ID, "pull_request", frozenset({51}), frozenset({5})),)


async def test_a_number_stored_under_another_github_id_outside_coverage(
    migrated_database: Path,
) -> None:
    """Out of coverage the entry is not hydrated, so it is no failure; step 7 lists the row."""
    _js(migrated_database, lambda seed, repo: stored_pr(seed, repo, 5, 50))
    result = await plan_of(migrated_database, attempted(merged(5, 51, FLOOR - SECOND)))
    assert (result.queue, result.failures) == ((), ())
    assert result.coverage == (Coverage(JS_ID, "pull_request", frozenset(), frozenset()),)


async def test_a_contradiction_is_reported_before_a_stored_number(migrated_database: Path) -> None:
    _js(migrated_database, lambda seed, repo: stored_pr(seed, repo, 5, 50))
    result = await plan_of(migrated_database, attempted(entry(5, 51, state="CLOSED")))
    assert [type(failure.error) for failure in result.failures] == [StateContradictsDates]


# --------------------------------------------------------------------------------------------
# Repositories without a complete index
# --------------------------------------------------------------------------------------------
async def test_a_repository_without_a_complete_index_gets_nothing(
    migrated_database: Path,
) -> None:
    def build(seed: Seed, js: int) -> None:
        stored_pr(seed, js, 1, 10)
        stored_pr(seed, js, 2, 20)
        server = seed.repository(SERVER, github_id=SERVER_ID)
        stored_pr(seed, server, 1, 110)
        stored_pr(seed, server, 2, 120)

    _js(migrated_database, build)
    js_entries = (entry(1, 10, updated_at=UPDATED + SECOND), entry(2, 20, is_draft=True))
    server_entries = (entry(1, 110, updated_at=UPDATED + SECOND), entry(2, 120, is_draft=True))

    def server(complete: bool) -> AttemptedRepository:
        return attempted(
            *server_entries,
            github_id=SERVER_ID,
            full_name=SERVER,
            complete=complete,
        )

    both = await plan_of(migrated_database, attempted(*js_entries), server(True))
    assert [(p.repository, p.number) for p in both.queue] == [(JS, 1), (SERVER, 1)]
    assert [(u.repository_github_id, u.number) for u in both.in_place_updates] == [
        (JS_ID, 2),
        (SERVER_ID, 2),
    ]
    assert both.coverage_of(SERVER_ID, "pull_request") is not None

    incomplete = await plan_of(migrated_database, attempted(*js_entries), server(False))
    assert [(p.repository, p.number) for p in incomplete.queue] == [(JS, 1)]
    assert [(u.repository_github_id, u.number) for u in incomplete.in_place_updates] == [(JS_ID, 2)]
    assert incomplete.coverage_of(SERVER_ID, "pull_request") is None
    assert incomplete.coverage_of(JS_ID, "pull_request") == both.coverage_of(JS_ID, "pull_request")


# --------------------------------------------------------------------------------------------
# In-place updates and pending items
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("indexed", "update"),
    [
        ({"is_draft": True}, (True, "MEMBER")),
        ({"author_association": "COLLABORATOR"}, (False, "COLLABORATOR")),
        ({"is_draft": True, "author_association": "NONE"}, (True, "NONE")),
        ({}, None),
    ],
    ids=["draft", "association", "both", "neither"],
)
async def test_an_in_place_update(
    migrated_database: Path, indexed: dict[str, Any], update: tuple[bool, str] | None
) -> None:
    ids: dict[str, int] = {}
    _js(migrated_database, lambda seed, repo: ids.setdefault("pr", stored_pr(seed, repo, 5, 50)))
    result = await plan_of(migrated_database, attempted(entry(5, 50, **indexed)))
    assert result.queue == ()
    assert result.in_place_updates == (
        (InPlaceUpdate(JS_ID, "pull_request", ids["pr"], 5, *update),) if update else ()
    )


async def test_no_in_place_update_for_a_planned_item(migrated_database: Path) -> None:
    _js(migrated_database, lambda seed, repo: stored_pr(seed, repo, 5, 50))
    the_entry = entry(5, 50, is_draft=True, author_association="NONE", updated_at=UPDATED + SECOND)
    result = await plan_of(migrated_database, attempted(the_entry))
    assert result.queue == (item(5, 50, {Reason.UPDATED_AT}, updated_at=UPDATED + SECOND),)
    assert result.in_place_updates == ()


async def test_a_pending_item_no_condition_selects_is_not_planned(migrated_database: Path) -> None:
    def build(seed: Seed, repo: int) -> None:
        stored_pr(seed, repo, 5, 50)
        stored_pr(seed, repo, 6, 60, hydration_version=V - 1)
        seed.pending(repo, 5)
        seed.pending(repo, 6)

    _js(migrated_database, build)
    result = await plan_of(migrated_database, attempted(entry(5, 50), entry(6, 60)))
    assert result.queue == (item(6, 60, {Reason.HYDRATION_VERSION}, pending=True),)


# --------------------------------------------------------------------------------------------
# The queue and its batches
# --------------------------------------------------------------------------------------------
def _hours(n: int) -> datetime:
    return UPDATED + timedelta(hours=n)


async def test_the_queue_order_across_repositories(migrated_database: Path) -> None:
    """Pending first, then open, then updatedAt descending; ties by repository, then number."""

    def build(seed: Seed, js: int) -> None:
        server = seed.repository(SERVER, github_id=SERVER_ID)
        seed.pending(js, 1)
        seed.pending(server, 1)

    _js(migrated_database, build)
    js_entries = [
        merged(1, 101, _hours(-5), updated_at=_hours(-5)),
        entry(2, 102, updated_at=_hours(-3)),
        merged(3, 103, _hours(0)),
        entry(6, 106, updated_at=_hours(-1)),
        entry(4, 104, updated_at=_hours(-1)),
    ]
    server_entries = [
        entry(1, 201, updated_at=_hours(-2)),
        entry(2, 202, updated_at=_hours(-1)),
        closed(3, 203, _hours(1), updated_at=_hours(1)),
    ]
    result = await plan_of(
        migrated_database,
        attempted(*js_entries),
        attempted(*server_entries, github_id=SERVER_ID, full_name=SERVER),
    )
    assert [(p.repository, p.number) for p in result.queue] == [
        (SERVER, 1),
        (JS, 1),
        (JS, 4),
        (JS, 6),
        (SERVER, 2),
        (JS, 2),
        (SERVER, 3),
        (JS, 3),
    ]
    assert [p.pending for p in result.queue] == [True, True, *[False] * 6]


async def test_batches_follow_the_queue_within_a_repository(migrated_database: Path) -> None:
    """Thirty Prebid.js and five prebid-server items alternate in the queue: the first batch takes
    Prebid.js's first 25, the next prebid-server's five, the last Prebid.js's remaining five."""
    js_entries = [
        merged(n, 1000 + n, FLOOR, updated_at=UPDATED - timedelta(minutes=2 * n)) for n in range(30)
    ]
    server_entries = [
        merged(n, 2000 + n, FLOOR, updated_at=UPDATED - timedelta(minutes=2 * n + 1))
        for n in range(5)
    ]
    result = await plan_of(
        migrated_database,
        attempted(*js_entries),
        attempted(*server_entries, github_id=SERVER_ID, full_name=SERVER),
    )
    assert [p.repository for p in result.queue[:10]] == [JS, SERVER] * 5
    assert [(b.repository, b.kind, b.numbers) for b in result.batches] == [
        (JS, "pull_request", tuple(range(25))),
        (SERVER, "pull_request", tuple(range(5))),
        (JS, "pull_request", tuple(range(25, 30))),
    ]
    for batch in result.batches:
        assert list(batch.items) == [p for p in result.queue if p in batch.items]
    batched = [(p.repository, p.number) for b in result.batches for p in b.items]
    assert sorted(batched) == sorted((p.repository, p.number) for p in result.queue)


@pytest.mark.parametrize(
    ("count", "sizes"), [(1, [1]), (25, [25]), (26, [25, 1]), (51, [25, 25, 1])]
)
def test_batch_sizes(count: int, sizes: list[int]) -> None:
    queue = [item(n, 100 + n, {Reason.NOT_STORED}) for n in range(count)]
    result = batches(queue)
    assert [len(batch.items) for batch in result] == sizes
    assert [planned for batch in result for planned in batch.items] == queue
    assert all(isinstance(batch, Batch) for batch in result)


def test_an_empty_queue_has_no_batches() -> None:
    assert batches([]) == ()


async def test_attempted_repositories_must_be_distinct(migrated_database: Path) -> None:
    snapshot = await snapshot_of(migrated_database)
    with pytest.raises(ValueError, match="repeat a github_id"):
        plan(snapshot, [attempted(), attempted(full_name="prebid/renamed")], config())


# --------------------------------------------------------------------------------------------
# A recorded index
# --------------------------------------------------------------------------------------------
async def _recorded_go_gdpr() -> tuple[AttemptedRepository, PullRequestIndex]:
    replay = Replay.from_fixtures()
    source = GitHubSource("test-token", http_transport=replay.transport(), clock=FakeClock())
    async with source:
        record = await source.resolve("prebid", "go-gdpr")
        recorded_index = await source.index_pull_requests(record)
    assert replay.problems == []
    return AttemptedRepository.resolved(record, recorded_index), recorded_index


GO_GDPR_ID = 132915235
BUG_GO = ("MDU6TGFiZWw5Mjg5ODIzMDE=", "bug")


async def test_a_recorded_index(migrated_database: Path) -> None:
    """go-gdpr's recorded resolve and index (37 PRs, 11 in coverage) over stored rows: #48 stored
    as indexed, #50 carrying a label the repository does not have, and #13 (merged in 2020) and
    #99 (not indexed) outside coverage, #13 stored as a draft."""
    repository, recorded_index = await _recorded_go_gdpr()
    assert len(repository.labels) == 10 and BUG_GO in repository.labels
    assert repository.github_id == GO_GDPR_ID
    with connect(migrated_database) as connection:
        seed = Seed(connection)
        repo = seed.repository("prebid/go-gdpr", github_id=GO_GDPR_ID)
        july = datetime(2026, 7, 17, 8, 56, 41, tzinfo=UTC)
        stored_pr(
            seed,
            repo,
            48,
            3250042830,
            labels=[BUG_GO],
            github_updated_at=july,
            hydrated_at=july + timedelta(hours=1),
        )
        october = datetime(2026, 10, 2, 14, 18, 33, tzinfo=UTC)
        stored_pr(
            seed,
            repo,
            50,
            4523847083,
            labels=[("LA_old", "obsolete")],
            github_updated_at=october,
            hydrated_at=october + timedelta(hours=1),
            author_association="NONE",
        )
        merged_2020 = datetime(2020, 3, 24, 19, 44, tzinfo=UTC)
        stored_pr(
            seed,
            repo,
            13,
            384516348,
            state="MERGED",
            merged_at=merged_2020,
            closed_at=merged_2020,
            is_draft=True,
        )
        stored_pr(seed, repo, 99, 999999)
    result = await plan_of(migrated_database, repository)
    assert [p.number for p in result.queue] == [50, 49, 44, 43, 42, 47, 39, 37, 36, 35]
    assert result.queue[0].reasons == frozenset({Reason.LABEL_GONE})
    assert all(p.reasons == {Reason.NOT_STORED} for p in result.queue[1:])
    assert [b.numbers for b in result.batches] == [(50, 49, 44, 43, 42, 47, 39, 37, 36, 35)]
    (coverage,) = result.coverage
    assert coverage.numbers == {35, 36, 37, 39, 42, 43, 44, 47, 48, 49, 50}
    assert len(coverage.github_ids) == 11 and 3250042830 in coverage.github_ids
    assert result.failures == ()
    assert [(u.number, u.is_draft, u.author_association) for u in result.in_place_updates] == [
        (13, False, "MEMBER")
    ]
    assert recorded_index.total_count == 37
