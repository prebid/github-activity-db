"""The planner of redesign plan 5.3 step 3.

``plan`` is a pure function over a snapshot of the stored rows, the attempted repositories as
steps 1 and 2 left them, and the configuration. It reads no database and sends no request, so
``--dry-run`` reuses it. It returns the hydration queue and its batches, the in-place updates,
the item failures it detects itself, which the engine records at once without the solo retry,
and each completely indexed repository's in-coverage set, which step 7 reads.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from github_activity_db.source.documents import PULL_REQUEST_HYDRATION_VERSION, PULL_REQUEST_KIND
from github_activity_db.source.limits import INDEX_CLOSING_LINKS, PR_BATCH_SIZE
from github_activity_db.source.records import (
    IndexedPullRequest,
    PullRequestIndex,
    RepositoryRecord,
    link_key,
)
from github_activity_db.sync.coverage import in_coverage, state_contradicts_dates
from github_activity_db.sync.snapshot import (
    LabelKey,
    Snapshot,
    StoredPullRequest,
    StoredRepository,
)
from github_activity_db.tracking import TrackingConfig

# Items per hydration batch, by kind (plan 5.3 step 4); issues join in Step 4.
BATCH_SIZES: Final = {PULL_REQUEST_KIND: PR_BATCH_SIZE}

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class Reason(StrEnum):
    """A hydration condition of plan 5.3 step 3 that holds for an item."""

    NOT_STORED = "not stored"
    UPDATED_AT = "updatedAt differs from github_updated_at"
    STATE = "state differs"
    HYDRATION_VERSION = "hydration_version differs from the code's"
    UNSETTLED = "unsettled"
    LABEL_GONE = "carries a label the repository no longer has"
    LINK_TOTAL = "closing-link totalCount differs from closing_issues_total"
    LINK_NOT_STORED = "an indexed closing link is not stored"
    LINK_NOT_INDEXED = "a stored closing link is not indexed"
    LINK_REPEATED = f"fewer distinct closing links than min(totalCount, {INDEX_CLOSING_LINKS})"
    FAILED = "has a sync_failures row"


class PlannerFailure(Exception):
    """An item failure the planner detects itself (plan 5.3 step 3)."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(f"#{number}: {message}")
        self.number = number


class StateContradictsDates(PlannerFailure):
    """The index lists a state its dates contradict (plan section 3); the stored row stays."""

    def __init__(self, entry: IndexedPullRequest) -> None:
        merged = entry.merged_at.isoformat() if entry.merged_at else None
        closed = entry.closed_at.isoformat() if entry.closed_at else None
        super().__init__(
            entry.number,
            f"the index lists state {entry.state} with mergedAt {merged} and closedAt {closed}",
        )


class NumberStoredUnderAnotherId(PlannerFailure):
    """An in-coverage index entry's number is stored under another ``github_id``; the stored
    row leaves through step 7."""

    def __init__(self, number: int, indexed_github_id: int, stored_github_id: int) -> None:
        super().__init__(
            number,
            f"the index lists github_id {indexed_github_id}, the database stores this number "
            f"under {stored_github_id}",
        )
        self.indexed_github_id = indexed_github_id
        self.stored_github_id = stored_github_id


@dataclass(frozen=True, slots=True)
class AttemptedRepository:
    """A repository the run attempts, after steps 1 and 2.

    ``labels`` are its labels as step 1's resolve read them, which a dry run does not write.
    ``index`` is None when the index did not complete (a fetch that still failed, a count
    mismatch after the rescan, or the deadline during it).
    """

    github_id: int
    full_name: str
    labels: frozenset[LabelKey]
    index: PullRequestIndex | None

    @classmethod
    def resolved(
        cls, record: RepositoryRecord, index: PullRequestIndex | None
    ) -> AttemptedRepository:
        return cls(
            github_id=record.github_id,
            full_name=record.full_name,
            labels=frozenset((label.node_id, label.name) for label in record.labels),
            index=index,
        )


@dataclass(frozen=True, slots=True)
class PlannedItem:
    """An item planned for hydration, with the index values the queue orders by.

    ``github_id`` is the index's: the hydrated item must carry the same one (plan 5.3 step 5).
    """

    repository_github_id: int
    repository: str
    kind: str
    number: int
    github_id: int
    is_open: bool
    updated_at: datetime
    pending: bool
    reasons: frozenset[Reason]


@dataclass(frozen=True, slots=True)
class Batch:
    """Items of one repository and kind, hydrated in one request."""

    repository_github_id: int
    repository: str
    kind: str
    items: tuple[PlannedItem, ...]

    @property
    def numbers(self) -> tuple[int, ...]:
        return tuple(item.number for item in self.items)


@dataclass(frozen=True, slots=True)
class InPlaceUpdate:
    """The index's ``is_draft`` and ``author_association`` for a stored item not planned for
    hydration; written as of the repository's ``last_index_started_at``."""

    repository_github_id: int
    kind: str
    row_id: int
    number: int
    is_draft: bool
    author_association: str


@dataclass(frozen=True, slots=True)
class ItemFailure:
    repository_github_id: int
    repository: str
    kind: str
    number: int
    error: PlannerFailure


@dataclass(frozen=True, slots=True)
class Coverage:
    """A completely indexed repository's in-coverage set, by ``github_id`` and by number."""

    repository_github_id: int
    kind: str
    github_ids: frozenset[int]
    numbers: frozenset[int]


@dataclass(frozen=True, slots=True)
class Plan:
    queue: tuple[PlannedItem, ...]
    batches: tuple[Batch, ...]
    in_place_updates: tuple[InPlaceUpdate, ...]
    failures: tuple[ItemFailure, ...]
    coverage: tuple[Coverage, ...]

    def coverage_of(self, repository_github_id: int, kind: str) -> Coverage | None:
        """The repository's in-coverage set, None when its index did not complete."""
        for coverage in self.coverage:
            if coverage.repository_github_id == repository_github_id and coverage.kind == kind:
                return coverage
        return None


def plan(
    snapshot: Snapshot, repositories: Sequence[AttemptedRepository], config: TrackingConfig
) -> Plan:
    """Plan the run's hydrations, in-place updates, and failures.

    A repository without a complete index gets none of them and no in-coverage set. The queue
    spans the repositories: ``sync_pending`` items first, then open items, then ``updatedAt``
    descending; ties keep the order of ``repositories``, then the number.

    Raises:
        ValueError: two attempted repositories have the same ``github_id``.
    """
    ids = [repository.github_id for repository in repositories]
    if len(set(ids)) != len(ids):
        raise ValueError(f"attempted repositories repeat a github_id: {ids}")
    queue: list[tuple[tuple[int, int, int, int, int], PlannedItem]] = []
    updates: list[InPlaceUpdate] = []
    failures: list[ItemFailure] = []
    coverage: list[Coverage] = []
    for position, repository in enumerate(repositories):
        if repository.index is None:
            continue
        stored = snapshot.repository(repository.github_id)
        result = _plan_repository(repository, repository.index, stored, config)
        coverage.append(result.coverage)
        updates.extend(result.updates)
        failures.extend(result.failures)
        for item in result.items:
            queue.append((_queue_key(item, position), item))
    ordered = tuple(item for _, item in sorted(queue, key=lambda pair: pair[0]))
    return Plan(
        queue=ordered,
        batches=batches(ordered),
        in_place_updates=tuple(updates),
        failures=tuple(failures),
        coverage=tuple(coverage),
    )


def batches(queue: Sequence[PlannedItem]) -> tuple[Batch, ...]:
    """Each batch: the first unbatched item and further unbatched items of its repository and
    kind, in queue order, up to the kind's batch size."""
    unbatched: defaultdict[tuple[int, str], deque[PlannedItem]] = defaultdict(deque)
    for item in queue:
        unbatched[(item.repository_github_id, item.kind)].append(item)
    batched: set[tuple[int, str, int]] = set()
    result: list[Batch] = []
    for item in queue:
        if (item.repository_github_id, item.kind, item.number) in batched:
            continue
        # Every earlier item of the queue is batched, so this item heads its group's remainder.
        group = unbatched[(item.repository_github_id, item.kind)]
        size = BATCH_SIZES[item.kind]
        members = tuple(group.popleft() for _ in range(min(size, len(group))))
        batched.update(
            (member.repository_github_id, member.kind, member.number) for member in members
        )
        result.append(
            Batch(
                repository_github_id=item.repository_github_id,
                repository=item.repository,
                kind=item.kind,
                items=members,
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class _RepositoryPlan:
    items: tuple[PlannedItem, ...]
    updates: tuple[InPlaceUpdate, ...]
    failures: tuple[ItemFailure, ...]
    coverage: Coverage


def _plan_repository(
    repository: AttemptedRepository,
    index: PullRequestIndex,
    stored: StoredRepository | None,
    config: TrackingConfig,
) -> _RepositoryPlan:
    kind = PULL_REQUEST_KIND
    floor = config.coverage.floor
    unsettled = timedelta(minutes=config.sync.unsettled_minutes)
    stored_rows = stored.pull_requests if stored is not None else ()
    by_github_id = {row.github_id: row for row in stored_rows}
    by_number = {row.number: row for row in stored_rows}
    failed_before = stored.failures if stored is not None else frozenset()
    pending = stored.pending if stored is not None else frozenset()

    items: list[PlannedItem] = []
    updates: list[InPlaceUpdate] = []
    failures: list[ItemFailure] = []
    covered_ids: set[int] = set()
    covered_numbers: set[int] = set()
    for entry in index.entries:
        row = by_github_id.get(entry.github_id)
        covered = in_coverage(entry, floor)
        if covered:
            covered_ids.add(entry.github_id)
            covered_numbers.add(entry.number)
            error = _planner_failure(entry, by_number.get(entry.number))
            if error is not None:
                # An item failure writes nothing for the item (plan 5.3 step 4).
                failures.append(
                    ItemFailure(
                        repository.github_id, repository.full_name, kind, entry.number, error
                    )
                )
                continue
            reasons = _reasons(
                entry, row, repository.labels, (kind, entry.number) in failed_before, unsettled
            )
            if reasons:
                items.append(
                    PlannedItem(
                        repository_github_id=repository.github_id,
                        repository=repository.full_name,
                        kind=kind,
                        number=entry.number,
                        github_id=entry.github_id,
                        is_open=entry.state == "OPEN",
                        updated_at=entry.updated_at,
                        pending=(kind, entry.number) in pending,
                        reasons=reasons,
                    )
                )
                continue
        if row is not None and (
            row.is_draft != entry.is_draft or row.author_association != entry.author_association
        ):
            updates.append(
                InPlaceUpdate(
                    repository_github_id=repository.github_id,
                    kind=kind,
                    row_id=row.row_id,
                    number=row.number,
                    is_draft=entry.is_draft,
                    author_association=entry.author_association,
                )
            )
    return _RepositoryPlan(
        items=tuple(items),
        updates=tuple(updates),
        failures=tuple(failures),
        coverage=Coverage(
            repository_github_id=repository.github_id,
            kind=kind,
            github_ids=frozenset(covered_ids),
            numbers=frozenset(covered_numbers),
        ),
    )


def _planner_failure(
    entry: IndexedPullRequest, stored_under_number: StoredPullRequest | None
) -> PlannerFailure | None:
    if state_contradicts_dates(entry):
        return StateContradictsDates(entry)
    if stored_under_number is not None and stored_under_number.github_id != entry.github_id:
        return NumberStoredUnderAnotherId(
            entry.number, entry.github_id, stored_under_number.github_id
        )
    return None


def _reasons(
    entry: IndexedPullRequest,
    row: StoredPullRequest | None,
    repository_labels: frozenset[LabelKey],
    failed_before: bool,
    unsettled: timedelta,
) -> frozenset[Reason]:
    """Every hydration condition of plan 5.3 step 3 that holds for an in-coverage entry."""
    reasons: set[Reason] = set()
    indexed_links = {link_key(link) for link in entry.closing_issues}
    if len(indexed_links) < min(entry.closing_issues_total, INDEX_CLOSING_LINKS):
        reasons.add(Reason.LINK_REPEATED)
    if failed_before:
        reasons.add(Reason.FAILED)
    if row is None:
        reasons.add(Reason.NOT_STORED)
        return frozenset(reasons)
    if entry.updated_at != row.github_updated_at:
        reasons.add(Reason.UPDATED_AT)
    if entry.state != row.state:
        reasons.add(Reason.STATE)
    if row.hydration_version != PULL_REQUEST_HYDRATION_VERSION:
        reasons.add(Reason.HYDRATION_VERSION)
    if row.hydrated_at - row.github_updated_at < unsettled:
        reasons.add(Reason.UNSETTLED)
    if not row.labels <= repository_labels:
        reasons.add(Reason.LABEL_GONE)
    if entry.closing_issues_total != row.closing_issues_total:
        reasons.add(Reason.LINK_TOTAL)
    if not indexed_links <= row.closing_links:
        reasons.add(Reason.LINK_NOT_STORED)
    if len(indexed_links) == entry.closing_issues_total and not row.closing_links <= indexed_links:
        reasons.add(Reason.LINK_NOT_INDEXED)
    return frozenset(reasons)


def _queue_key(item: PlannedItem, position: int) -> tuple[int, int, int, int, int]:
    """Pending first, then open, then ``updatedAt`` descending; then repository, then number."""
    updated = (item.updated_at - _EPOCH) // timedelta(microseconds=1)
    return (0 if item.pending else 1, 0 if item.is_open else 1, -updated, position, item.number)
