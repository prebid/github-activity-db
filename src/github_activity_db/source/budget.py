"""Rate-limit budget per pool (redesign plan 5.3 step 4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Usage:
    """What a run spent: ``graphql_points`` sums ``rateLimit.cost``."""

    graphql_points: int = 0
    graphql_requests: int = 0
    rest_calls: int = 0


class BudgetPool:
    """Remaining budget of one pool: GraphQL points or REST core calls.

    GitHub serves requests from several regions, and one token's readings can alternate between
    windows with different ``remaining`` and reset times. Readings are therefore kept per reset
    time, each at its lowest value, and the pool gates on the lowest among windows not yet reset.
    """

    def __init__(self, reserve: int) -> None:
        self.reserve = reserve
        self._windows: dict[datetime, int] = {}

    def record(self, remaining: int, reset_at: datetime) -> None:
        """Record a reading taken from a response."""
        current = self._windows.get(reset_at)
        self._windows[reset_at] = remaining if current is None else min(current, remaining)

    def latest_reset(self, now: datetime) -> datetime | None:
        """The latest reset time among windows not yet reset, if any reading was taken."""
        pending = [reset_at for reset_at in self._windows if reset_at > now]
        return max(pending) if pending else None

    def blocked_until(self, now: datetime) -> datetime | None:
        """Reset time to wait for before the next request, or None when a request may go now."""
        self._windows = {
            reset_at: left for reset_at, left in self._windows.items() if reset_at > now
        }
        below = [
            (left, reset_at) for reset_at, left in self._windows.items() if left < self.reserve
        ]
        if not below:
            return None
        return min(below)[1]
