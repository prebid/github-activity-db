"""Async token bucket rate limiter.

Provides a shared admission gate for GitHub API requests. Multiple concurrent
callers ``acquire()`` tokens from a single bucket whose refill rate adapts to
GitHub's reported rate-limit response headers. This keeps the total request
rate under GitHub's budget regardless of how many workers are running, which
a per-call delay-based pacer cannot do (each worker computes the same delay
independently and the realized rate becomes ``N x intended``).

When ``remaining`` falls below ``hard_floor`` or a ``Retry-After`` is observed,
all acquires block until reset — preventing in-flight overshoot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from github_activity_db.logging import get_logger

logger = get_logger(__name__)


class AsyncTokenBucket:
    """Concurrency-safe adaptive token bucket."""

    def __init__(
        self,
        capacity: float = 10.0,
        initial_rate: float = 1.0,
        min_rate: float = 0.1,
        max_rate: float = 20.0,
        hard_floor_pct: float = 10.0,
        min_hard_floor: int = 50,
    ) -> None:
        """Initialize the bucket.

        Args:
            capacity: Maximum tokens the bucket can hold (burst limit).
            initial_rate: Starting tokens-per-second rate, used until the
                first rate-limit header arrives.
            min_rate: Floor for the adaptive rate (prevents stalls when
                quota is high but reset is short).
            max_rate: Ceiling for the adaptive rate (prevents spamming
                GitHub when the budget is large).
            hard_floor_pct: Reserve percentage of GitHub's reported limit
                below which all acquires block until reset. Recomputed on
                each header update so the floor scales with the actual
                quota (5000 for PATs, 15000 for GitHub Apps, etc.).
            min_hard_floor: Absolute floor of the hard floor — used until
                we observe the limit, and as a lower bound thereafter.
        """
        if capacity < 1.0:
            raise ValueError("capacity must be >= 1.0")
        if min_rate <= 0:
            raise ValueError("min_rate must be > 0")
        if max_rate < min_rate:
            raise ValueError("max_rate must be >= min_rate")
        if min_hard_floor < 0:
            raise ValueError("min_hard_floor must be >= 0")
        if not 0.0 <= hard_floor_pct <= 100.0:
            raise ValueError("hard_floor_pct must be in [0, 100]")

        self._capacity = capacity
        self._tokens = capacity  # start full to allow initial burst
        self._rate = max(min_rate, min(max_rate, initial_rate))
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._hard_floor_pct = hard_floor_pct
        self._min_hard_floor = min_hard_floor
        self._hard_floor = min_hard_floor  # bumped up by first header observed

        self._last_refill = datetime.now(UTC)
        self._wait_until: datetime | None = None

        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        """Current tokens-per-second refill rate."""
        return self._rate

    @property
    def capacity(self) -> float:
        """Maximum tokens the bucket can hold."""
        return self._capacity

    @property
    def hard_floor(self) -> int:
        """Quota threshold below which acquires block until reset."""
        return self._hard_floor

    async def acquire(self) -> None:
        """Block until one token is available, then consume it.

        Safe under concurrent callers: only one acquire wakes per token,
        preserving the rate regardless of concurrency.
        """
        while True:
            async with self._lock:
                wait_remaining = self._forced_wait_remaining()
                if wait_remaining > 0:
                    sleep_for = wait_remaining
                else:
                    self._refill_locked()
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    needed = 1.0 - self._tokens
                    sleep_for = needed / self._rate

            await asyncio.sleep(min(sleep_for, 60.0))

    def update_from_headers(self, headers: dict[str, str]) -> None:
        """Update the refill rate from a GitHub response.

        Reads ``x-ratelimit-remaining`` and ``x-ratelimit-reset`` headers and
        adjusts ``rate`` so token issuance matches the available budget over
        the remaining window. If ``remaining <= hard_floor``, sets a forced
        wait until the reset time.

        Synchronous and lock-free by design: it is called from the synchronous
        response-handler in ``GitHubClient`` and any race with a concurrent
        ``acquire()`` is benign — at worst an in-flight acquire sleeps using
        the prior rate for one cycle, then re-checks the bucket on its next
        loop iteration and observes the new state.
        """
        try:
            remaining = int(headers.get("x-ratelimit-remaining", "-1"))
            reset_ts = int(headers.get("x-ratelimit-reset", "0"))
            limit = int(headers.get("x-ratelimit-limit", "0"))
        except (TypeError, ValueError):
            return

        if remaining < 0 or reset_ts <= 0:
            return

        # Recompute the hard floor from the live limit. PATs have 5000/hr,
        # GitHub Apps with installation tokens have 15000/hr, and unauthed
        # has 60/hr. Computing the floor from the configured percentage keeps
        # the buffer correctly scaled to whatever quota we actually have.
        if limit > 0:
            self._hard_floor = max(
                self._min_hard_floor,
                int(limit * self._hard_floor_pct / 100),
            )

        reset_at = datetime.fromtimestamp(reset_ts, tz=UTC)
        seconds_until_reset = (reset_at - datetime.now(UTC)).total_seconds()

        if remaining <= self._hard_floor:
            # Hard floor: block all acquires until reset
            self._wait_until = reset_at
            self._rate = self._min_rate
            logger.warning(
                "Rate-limit hard floor reached (remaining=%d, hard_floor=%d), "
                "blocking acquires until %s",
                remaining,
                self._hard_floor,
                reset_at.isoformat(),
            )
            return

        if seconds_until_reset <= 0:
            # Reset is past — keep existing rate; next response will recalibrate
            return

        # Adaptive: spread the spendable budget evenly over the remaining window
        budget = remaining - self._hard_floor
        target = budget / seconds_until_reset
        self._rate = max(self._min_rate, min(self._max_rate, target))

    def force_wait_until(self, when: datetime) -> None:
        """Force all acquires to block until ``when``.

        Use on observed rate-limit errors with a ``Retry-After``. Composes
        monotonically: later calls with an earlier deadline are ignored, so
        a strict (primary) limit observation can never be shortened by a
        looser (secondary) one arriving in the same window.

        Synchronous and lock-free, but safe: under asyncio's single-threaded
        model the read-modify-write here cannot be interrupted by another
        coroutine (no ``await`` points). A concurrent in-flight ``acquire``
        observes the new ``_wait_until`` on its next loop iteration.
        """
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if self._wait_until is None or when > self._wait_until:
            self._wait_until = when

    def force_wait(self, seconds: float) -> None:
        """Force all acquires to block for ``seconds`` seconds."""
        self.force_wait_until(datetime.now(UTC) + timedelta(seconds=max(0.0, seconds)))

    def clear_forced_wait(self) -> None:
        """Clear any forced wait state.

        An in-flight ``acquire`` that is mid-sleep when this is called will
        not wake up early; it must finish its current ``asyncio.sleep`` and
        re-check the bucket. ``acquire`` caps each sleep at 60s, so the
        worst-case latency to honor a clear is ~60s.
        """
        self._wait_until = None

    @property
    def is_forced_wait_active(self) -> bool:
        """Whether a forced wait is currently in effect."""
        return self._forced_wait_remaining() > 0

    @property
    def forced_wait_remaining(self) -> float:
        """Seconds remaining in any forced wait (0 if not active)."""
        return self._forced_wait_remaining()

    @property
    def tokens_available(self) -> float:
        """Approximate tokens available right now, without consuming any.

        Lock-free for observability; may be stale by up to one acquire
        cycle. Each individual read is atomic (Python's GIL), so the value
        is never torn — just possibly out of date.
        """
        elapsed = (datetime.now(UTC) - self._last_refill).total_seconds()
        return min(self._capacity, self._tokens + max(0.0, elapsed) * self._rate)

    def get_stats(self) -> dict[str, float | int | bool | None]:
        """Snapshot of current state for monitoring."""
        return {
            "rate_per_second": round(self._rate, 4),
            "capacity": self._capacity,
            "tokens_available": round(self.tokens_available, 2),
            "hard_floor": self._hard_floor,
            "is_forced_wait": self.is_forced_wait_active,
            "forced_wait_remaining_seconds": round(self.forced_wait_remaining, 2),
        }

    # Internals ---------------------------------------------------------------

    def _refill_locked(self) -> None:
        """Refill tokens based on time elapsed since the last refill.

        Caller must hold ``_lock``.
        """
        now = datetime.now(UTC)
        elapsed = (now - self._last_refill).total_seconds()
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

    def _forced_wait_remaining(self) -> float:
        """Compute remaining forced-wait time and clear if expired.

        This is also called outside the lock for read-only properties; the
        write (clearing on expiry) is idempotent so a benign race is fine.
        """
        if self._wait_until is None:
            return 0.0
        delta = (self._wait_until - datetime.now(UTC)).total_seconds()
        if delta <= 0:
            self._wait_until = None
            return 0.0
        return delta
