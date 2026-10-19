"""The coverage rule (plan section 3), on each side of the floor for each state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from github_activity_db.source.records import IndexedPullRequest
from github_activity_db.sync.coverage import in_coverage, state_contradicts_dates
from tests.sync.support import FLOOR, closed, entry, merged

BEFORE = FLOOR - timedelta(microseconds=1)
OLD = datetime(2016, 4, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("the_entry", "covered"),
    [
        (entry(1, 1, created_at=OLD, updated_at=OLD), True),
        (merged(1, 1, FLOOR, created_at=OLD), True),
        (merged(1, 1, BEFORE, created_at=OLD), False),
        (entry(1, 1, state="MERGED", merged_at=BEFORE, closed_at=FLOOR, created_at=OLD), False),
        (closed(1, 1, FLOOR, created_at=OLD), True),
        (closed(1, 1, BEFORE, created_at=OLD), False),
        (closed(1, 1, BEFORE, created_at=FLOOR - timedelta(days=1), updated_at=FLOOR), False),
    ],
    ids=[
        "open",
        "merged-at-the-floor",
        "merged-before-the-floor",
        "merged-before-the-floor-closed-at-it",
        "closed-at-the-floor",
        "closed-before-the-floor",
        "closed-before-the-floor-updated-at-it",
    ],
)
def test_in_coverage(the_entry: IndexedPullRequest, covered: bool) -> None:
    assert state_contradicts_dates(the_entry) is False
    assert in_coverage(the_entry, FLOOR) is covered


@pytest.mark.parametrize(
    "the_entry",
    [
        entry(1, 1, state="CLOSED"),
        entry(1, 1, state="MERGED", closed_at=OLD),
        entry(1, 1, state="MERGED"),
        entry(1, 1, merged_at=OLD),
        entry(1, 1, merged_at=OLD, closed_at=OLD),
        entry(1, 1, state="CLOSED", merged_at=OLD, closed_at=OLD),
    ],
    ids=[
        "closed-without-closed-at",
        "merged-without-merged-at",
        "merged-without-either",
        "open-with-merged-at",
        "open-with-both",
        "closed-with-merged-at",
    ],
)
def test_a_contradicted_state_counts_as_in_coverage(the_entry: IndexedPullRequest) -> None:
    """Every date here is before the floor, which would leave a consistent entry out."""
    assert state_contradicts_dates(the_entry) is True
    assert in_coverage(the_entry, FLOOR) is True


@pytest.mark.parametrize(
    "the_entry",
    [
        entry(1, 1),
        entry(1, 1, closed_at=OLD),
        merged(1, 1, OLD),
        entry(1, 1, state="MERGED", merged_at=OLD),
        closed(1, 1, OLD),
    ],
    ids=["open", "open-with-closed-at", "merged", "merged-without-closed-at", "closed"],
)
def test_states_their_dates_do_not_contradict(the_entry: IndexedPullRequest) -> None:
    assert state_contradicts_dates(the_entry) is False
