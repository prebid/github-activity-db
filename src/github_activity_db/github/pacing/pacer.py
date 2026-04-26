"""Request pacing using a shared async token bucket.

The pacer is the admission gate that all GitHub API calls flow through.
A single :class:`AsyncTokenBucket` is shared across all concurrent workers,
ensuring the realized request rate stays under GitHub's budget regardless of
how many workers are running.

This is a behavioral change from the prior per-call delay model: that model
had each worker independently compute a delay, which under N workers
produced a realized rate of ``N x intended_rate`` and could exhaust the
quota even when each worker thought it was being polite.

Usage::

    monitor = RateLimitMonitor()
    pacer = RequestPacer(monitor)

    # Before each request:
    await pacer.acquire()

    # After each response — feed headers back so the rate adapts:
    pacer.on_request_complete(response_headers)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from github_activity_db.config import PacingConfig, get_settings
from github_activity_db.logging import get_logger

from .token_bucket import AsyncTokenBucket

if TYPE_CHECKING:
    from github_activity_db.github.rate_limit.monitor import RateLimitMonitor

logger = get_logger(__name__)


class RequestPacer:
    """Shared admission gate for GitHub API requests.

    Internally wraps an :class:`AsyncTokenBucket`. The pacer's public surface
    keeps the forced-wait API for callers that observe rate-limit errors;
    everything else delegates to the bucket.
    """

    def __init__(
        self,
        monitor: RateLimitMonitor,
        config: PacingConfig | None = None,
    ) -> None:
        """Initialize the request pacer.

        Args:
            monitor: RateLimitMonitor for observability and external status
                queries (e.g., ``ghactivity github rate-limit``). The pacer
                does not read from the monitor for its own pacing decisions
                — it derives state from the bucket.
            config: Optional pacing configuration (uses settings if not
                provided).
        """
        self._monitor = monitor
        self._config = config or get_settings().pacing

        # Map PacingConfig to bucket parameters:
        #   capacity         ← burst_allowance (max tokens accumulated)
        #   max_rate         ← 1000 / min_request_interval_ms (req/s ceiling)
        #   min_rate         ← 1000 / max_request_interval_ms (req/s floor)
        #   hard_floor_pct   ← reserve_buffer_pct (recomputed against the
        #                      actual GitHub-reported limit on first response)
        # Floors and ceilings are protective; the actual rate is set
        # adaptively from response headers.
        capacity = max(1.0, float(self._config.burst_allowance))
        max_rate = 1000.0 / max(1, self._config.min_request_interval_ms)
        min_rate_from_config = 1000.0 / max(1, self._config.max_request_interval_ms)
        # Never floor below 0.01 req/s — that's one request per 100s, the
        # minimum we'd want even on an exhausted quota.
        min_rate = max(0.01, min(min_rate_from_config, max_rate))

        # Initial rate is conservative — the first response header will
        # recalibrate, so we don't want to fire a burst of requests against
        # an unknown quota.
        self._bucket = AsyncTokenBucket(
            capacity=capacity,
            initial_rate=min(1.0, max_rate),
            min_rate=min_rate,
            max_rate=max_rate,
            hard_floor_pct=self._config.reserve_buffer_pct,
        )

    @property
    def config(self) -> PacingConfig:
        """The pacing configuration in effect."""
        return self._config

    @property
    def bucket(self) -> AsyncTokenBucket:
        """The underlying token bucket (for advanced inspection)."""
        return self._bucket

    # Admission gate ----------------------------------------------------------
    async def acquire(self) -> None:
        """Block until one token is available, then consume it.

        Call before every GitHub API request. Concurrency-safe.
        """
        await self._bucket.acquire()

    # Lifecycle hooks ---------------------------------------------------------
    def on_request_complete(self, headers: dict[str, str] | None = None) -> None:
        """Record that a request completed.

        If headers are provided, both the rate-limit monitor and the token
        bucket are updated. The monitor exists for external observability;
        the bucket is what gates further requests.
        """
        if headers is None:
            return
        self._monitor.update_from_headers(headers)
        self._bucket.update_from_headers(headers)

    # Forced wait (for observed 403/429 with Retry-After) ---------------------
    def force_wait(self, seconds: float) -> None:
        """Force all acquires to block for ``seconds`` seconds."""
        self._bucket.force_wait(seconds)
        logger.info("Forced wait set for %.1f seconds", seconds)

    def force_wait_until(self, reset_at: datetime) -> None:
        """Force all acquires to block until ``reset_at``."""
        self._bucket.force_wait_until(reset_at)
        wait_seconds = max(0.0, (reset_at - datetime.now(UTC)).total_seconds())
        logger.info("Forced wait until %s (%.1f seconds)", reset_at.isoformat(), wait_seconds)

    def clear_forced_wait(self) -> None:
        """Clear any forced-wait state."""
        self._bucket.clear_forced_wait()

    @property
    def is_forced_wait_active(self) -> bool:
        """Whether a forced wait is currently active."""
        return self._bucket.is_forced_wait_active

    @property
    def forced_wait_remaining(self) -> float:
        """Seconds remaining in any forced wait (0 if not active)."""
        return self._bucket.forced_wait_remaining

    # Observability -----------------------------------------------------------
    def get_stats(self) -> dict[str, float | int | str | bool | None]:
        """Snapshot the pacer's state for monitoring."""
        bucket_stats = self._bucket.get_stats()
        status = self._monitor.get_status()
        pool_limit = self._monitor.get_pool_limit()
        return {
            "rate_per_second": bucket_stats["rate_per_second"],
            "tokens_available": bucket_stats["tokens_available"],
            "capacity": bucket_stats["capacity"],
            "hard_floor": bucket_stats["hard_floor"],
            "is_forced_wait": bucket_stats["is_forced_wait"],
            "forced_wait_remaining_seconds": bucket_stats["forced_wait_remaining_seconds"],
            "status": status.value,
            "remaining": pool_limit.remaining if pool_limit else None,
            "seconds_until_reset": pool_limit.seconds_until_reset if pool_limit else None,
        }
