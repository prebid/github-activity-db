"""Step 7's deletion candidates, delete guard, and ``--allow-deletions`` (redesign plan 5.3).

The engine calls these after its writes, with the stored rows read again and the in-coverage set
the planner returned; ``--dry-run`` calls them with the planner's snapshot. They write nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from github_activity_db.source.documents import PULL_REQUEST_KIND
from github_activity_db.sync.plan import Coverage
from github_activity_db.sync.snapshot import ItemKey, StoredRepository
from github_activity_db.tracking import SyncConfig, TrackedRepository, TrackingConfig

_APPROVAL = re.compile(r"(?P<repository>[^=]+)=(?P<limit>[0-9]+)")


class AllowDeletionsError(ValueError):
    """An ``--allow-deletions`` value that is not ``owner/name=N`` with N at least 1 and the name
    of a configured repository."""


@dataclass(frozen=True, slots=True)
class DeletionApproval:
    """``--allow-deletions owner/name=N``: the configured repository and N."""

    repository: TrackedRepository
    limit: int

    def limit_for(self, repository: TrackedRepository) -> int | None:
        """N for the repository the approval names, None for any other."""
        return (
            self.limit
            if repository.full_name.lower() == self.repository.full_name.lower()
            else None
        )


def parse_allow_deletions(value: str, config: TrackingConfig) -> DeletionApproval:
    """Read ``owner/name=N``; the name matches a configured repository ignoring ASCII case.

    GitHub names are ASCII, and ``str.lower()`` folds some other characters into ASCII letters
    (KELVIN SIGN into "k"), so a value with any other character is refused.

    Raises:
        AllowDeletionsError: the value is not ASCII or has another form, N is below 1 or has more
            digits than ``int()`` converts, or no configured repository has the name.
    """
    if not value.isascii():
        raise AllowDeletionsError(f"--allow-deletions takes an ASCII owner/name=N, not {value!r}")
    match = _APPROVAL.fullmatch(value)
    if match is None:
        raise AllowDeletionsError(f"--allow-deletions takes owner/name=N, not {value!r}")
    try:
        limit = int(match["limit"])
    except ValueError as e:  # past Python's limit on the digits int() converts
        raise AllowDeletionsError(
            f"--allow-deletions N has too many digits ({len(match['limit'])})"
        ) from e
    if limit < 1:
        raise AllowDeletionsError(f"--allow-deletions needs N of at least 1, not {limit}")
    name = match["repository"].lower()
    for repository in config.repositories:
        if repository.full_name.lower() == name:
            return DeletionApproval(repository=repository, limit=limit)
    raise AllowDeletionsError(
        f"--allow-deletions names {match['repository']!r}, which tracking.toml does not list"
    )


def guard_limit(stored_rows: int, config: SyncConfig) -> int:
    """The most deletions the guard lets through: max(min rows, percent of the stored rows).

    The percentage is read as the shortest decimal that gives back the configured float
    (``str()`` of it). For a value written with at most 15 significant digits and not below
    2.2250738585072014e-308 (the smallest normal float) that is the decimal ``tracking.toml``
    writes, so 2.0% of 1,310 rows is exactly 26.2 and lets 26 through.
    """
    share = Fraction(str(config.delete_guard_percent)) * stored_rows / 100
    return int(max(Fraction(config.delete_guard_min_rows), share))


@dataclass(frozen=True, slots=True)
class GuardDecision:
    """Whether one guarded set's deletions run.

    They run when the candidates are at most ``guard_limit`` of the stored rows counted before
    the run's writes, or at most the ``--allow-deletions`` N for the repository; otherwise they
    halt.
    """

    candidates: int
    stored_rows: int
    limit: int
    approved_limit: int | None

    @property
    def runs(self) -> bool:
        if self.candidates <= self.limit:
            return True
        return self.approved_limit is not None and self.candidates <= self.approved_limit

    @property
    def halt_reason(self) -> str | None:
        """What ``repo_sync_state.halt_reason`` records for a halt: the candidate count and the
        limits it exceeds; None when the deletions run."""
        if self.runs:
            return None
        reason = (
            f"{self.candidates} deletion candidates exceed the delete guard's {self.limit} "
            f"({self.stored_rows} stored rows before the run)"
        )
        if self.approved_limit is not None:
            reason += f" and --allow-deletions {self.approved_limit}"
        return reason


def guard(
    candidates: int, stored_rows: int, config: SyncConfig, approved_limit: int | None
) -> GuardDecision:
    """Decide one guarded set; ``stored_rows`` is counted in the snapshot before the run's writes,
    and ``approved_limit`` is the ``--allow-deletions`` N for this repository, if any."""
    return GuardDecision(
        candidates=candidates,
        stored_rows=stored_rows,
        limit=guard_limit(stored_rows, config),
        approved_limit=approved_limit,
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """A stored item whose ``github_id`` is absent from its repository's in-coverage set."""

    kind: str
    row_id: int
    github_id: int
    number: int


@dataclass(frozen=True, slots=True)
class Deletions:
    """Step 7 for one repository's items.

    ``candidates`` are deleted when ``decision.runs``; ``failures_removed``, the
    ``sync_failures`` rows of items absent from the in-coverage set, are removed even under a
    halt. ``approval`` is the ``--allow-deletions`` approval naming this repository, whose N the
    guard decided with, which the writer records; None without one.

    Raises:
        ValueError: ``approval`` and ``decision.approved_limit`` name different Ns.
    """

    repository_github_id: int
    candidates: tuple[Candidate, ...]
    decision: GuardDecision
    failures_removed: tuple[ItemKey, ...]
    approval: DeletionApproval | None = None

    def __post_init__(self) -> None:
        approved = None if self.approval is None else self.approval.limit
        if self.decision.approved_limit != approved:
            raise ValueError(
                f"repository {self.repository_github_id}: the guard decided with N "
                f"{self.decision.approved_limit}, the approval carries {approved}"
            )


def item_deletions(
    stored: StoredRepository | None,
    coverage: Coverage | None,
    *,
    repository: TrackedRepository,
    failed_during_hydration: bool,
    stored_rows_before_run: int,
    config: SyncConfig,
    approval: DeletionApproval | None,
) -> Deletions | None:
    """Step 7 for a repository's items, or None when step 7 skips the repository.

    It skips a repository whose index did not complete (no in-coverage set) and one that failed
    during hydration. ``stored`` is the repository's stored rows as they are now, after the run's
    writes; None when nothing is stored. The guard counts ``stored_rows_before_run``, taken from
    the planner's snapshot. ``repository`` is the configured repository the run resolved to
    ``coverage``'s; ``approval`` is the run's ``--allow-deletions``, which applies only when it
    names that repository, and then travels with the result to the writer.

    Raises:
        ValueError: ``coverage`` is not of pull requests, the only items stored before Step 4.
    """
    if coverage is None or failed_during_hydration:
        return None
    if coverage.kind != PULL_REQUEST_KIND:
        raise ValueError(f"no stored {coverage.kind} rows are read before Step 4")
    rows = stored.pull_requests if stored is not None else ()
    candidates = tuple(
        Candidate(kind=coverage.kind, row_id=row.row_id, github_id=row.github_id, number=row.number)
        for row in rows
        if row.github_id not in coverage.github_ids
    )
    failures = stored.failures if stored is not None else frozenset()
    removed = tuple(
        sorted(
            (kind, number)
            for kind, number in failures
            if kind == coverage.kind and number not in coverage.numbers
        )
    )
    applied = (
        approval if approval is not None and approval.limit_for(repository) is not None else None
    )
    return Deletions(
        repository_github_id=coverage.repository_github_id,
        candidates=candidates,
        decision=guard(
            len(candidates),
            stored_rows_before_run,
            config,
            None if applied is None else applied.limit,
        ),
        failures_removed=removed,
        approval=applied,
    )
