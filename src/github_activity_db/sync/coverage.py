"""The coverage rule for pull requests (redesign plan section 3)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Dated(Protocol):
    """A pull request as the index or a hydration reports it: its state and dates."""

    @property
    def state(self) -> str: ...
    @property
    def merged_at(self) -> datetime | None: ...
    @property
    def closed_at(self) -> datetime | None: ...


def state_contradicts_dates(entry: Dated) -> bool:
    """``CLOSED`` without ``closedAt``, ``MERGED`` without ``mergedAt``, or ``mergedAt`` with
    another state."""
    if entry.state == "CLOSED" and entry.closed_at is None:
        return True
    if entry.state == "MERGED":
        return entry.merged_at is None
    return entry.merged_at is not None


def in_coverage(entry: Dated, floor: datetime) -> bool:
    """``OPEN``, ``MERGED`` with ``mergedAt >= floor``, or ``CLOSED`` with ``closedAt >= floor``.

    An entry whose state its dates contradict counts as in coverage, so it is never a deletion
    candidate; the planner fails it without hydrating it.
    """
    if state_contradicts_dates(entry):
        return True
    if entry.state == "MERGED":
        return entry.merged_at is not None and entry.merged_at >= floor
    if entry.state == "CLOSED":
        return entry.closed_at is not None and entry.closed_at >= floor
    return True
