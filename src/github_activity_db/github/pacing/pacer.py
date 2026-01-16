"""Request pacing using token bucket algorithm.

This module calculates optimal request delays based on current rate limit
state to maximize throughput while avoiding rate limit exhaustion.

Algorithm:
    base_delay = time_until_reset / (remaining - buffer)
    adjusted_delay = base_delay * throttle_multiplier

Where throttle_multiplier increases as quota health decreases.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from github_activity_db.config import PacingConfig, get_settings
from github_activity_db.github.rate_limit.schemas import (
    PoolRateLimit,
    RateLimitPool,
    RateLimitStatus,
)

if TYPE_CHECKING:
    from github_activity_db.github.rate_limit.monitor import RateLimitMonitor

logger = logging.getLogger(__name__)


class RequestPacer:
    """Calculates optimal request delays using token bucket algorithm.

    The pacer reads rate limit state from a RateLimitMonitor and calculates
    recommended delays to maximize throughput while avoiding exhaustion.

    Key features:
    - Adaptive throttling based on quota health
    - Configurable min/max delay bounds
    - Burst allowance for short-term flexibility
    - Forced wait support for rate limit recovery

    Usage:
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        # Before each request
        delay = pacer.get_recommended_delay()
        if delay > 0:
            await asyncio.sleep(delay)

        # Make request...

        # After request (update from response headers)
        pacer.on_request_complete()
    """

    def __init__(
        self,
        monitor: RateLimitMonitor,
        config: PacingConfig | None = None,
    ) -> None:
        """Initialize the request pacer.

        Args:
            monitor: RateLimitMonitor instance to read state from
            config: Optional pacing configuration (uses settings if not provided)
        """
        self._monitor = monitor
        self._config = config or get_settings().pacing

        # Request tracking
        self._last_request_at: datetime | None = None
        self._requests_in_window: list[datetime] = []

        # Forced wait state
        self._wait_until: datetime | None = None

        # Lock for thread-safe updates
        self._lock = asyncio.Lock()

    @property
    def config(self) -> PacingConfig:
        """Get the pacing configuration."""
        return self._config

    # -------------------------------------------------------------------------
    # Delay Calculation
    # -------------------------------------------------------------------------
    def get_recommended_delay(
        self,
        pool: RateLimitPool = RateLimitPool.CORE,
    ) -> float:
        """Calculate recommended delay before next request.

        This is the main method to call before making a request.
        Returns seconds to wait (0 = proceed immediately).

        Args:
            pool: Rate limit pool to calculate delay for

        Returns:
            Delay in seconds (0.0 to max_request_interval)
        """
        # Check forced wait first
        if self._wait_until:
            wait_time = (self._wait_until - datetime.now(UTC)).total_seconds()
            if wait_time > 0:
                return wait_time
            self._wait_until = None

        # Get current rate limit status
        pool_limit = self._monitor.get_pool_limit(pool)

        # No data - use minimum interval
        if pool_limit is None:
            return self._config.min_request_interval_ms / 1000

        # Calculate optimal pacing
        return self._calculate_optimal_delay(pool_limit)

    def _calculate_optimal_delay(self, pool_limit: PoolRateLimit) -> float:
        """Calculate optimal delay using token bucket algorithm.

        Formula:
            buffer = limit * reserve_buffer_pct
            effective = max(1, remaining - buffer + burst_allowance)
            base_delay = time_until_reset / effective
            delay = base_delay * throttle_multiplier

        Args:
            pool_limit: Current pool rate limit state

        Returns:
            Recommended delay in seconds
        """
        seconds_until_reset = pool_limit.seconds_until_reset

        # If reset is imminent or past, use minimum delay
        if seconds_until_reset <= 0:
            return self._config.min_request_interval_ms / 1000

        # Calculate buffer (reserve quota)
        buffer = int(pool_limit.limit * (self._config.reserve_buffer_pct / 100))
        effective_remaining = pool_limit.remaining - buffer + self._config.burst_allowance
        effective_remaining = max(1, effective_remaining)  # Prevent division by zero

        # Base delay: spread requests evenly over remaining time
        base_delay = seconds_until_reset / effective_remaining

        # Apply adaptive throttle multiplier
        status = pool_limit.get_status()
        multiplier = self._get_throttle_multiplier(status)
        adjusted_delay = base_delay * multiplier

        # Clamp to configured bounds
        min_delay = self._config.min_request_interval_ms / 1000
        max_delay = self._config.max_request_interval_ms / 1000

        return max(min_delay, min(adjusted_delay, max_delay))

    def _get_throttle_multiplier(self, status: RateLimitStatus) -> float:
        """Get throttle multiplier based on quota health.

        Returns multiplier to apply to base delay:
            - HEALTHY: 1.0x (no throttling)
            - WARNING: 1.5x (moderate throttling)
            - CRITICAL: 2.0x (significant throttling)
            - EXHAUSTED: 4.0x (heavy throttling until reset)

        Args:
            status: Current rate limit status

        Returns:
            Multiplier value (1.0 to 4.0)
        """
        multipliers = {
            RateLimitStatus.HEALTHY: 1.0,
            RateLimitStatus.WARNING: 1.5,
            RateLimitStatus.CRITICAL: 2.0,
            RateLimitStatus.EXHAUSTED: 4.0,
        }
        return multipliers.get(status, 1.0)

    # -------------------------------------------------------------------------
    # Request Lifecycle
    # -------------------------------------------------------------------------
    def on_request_start(self) -> None:
        """Record that a request is starting.

        Call this just before making a request to track request velocity.
        """
        now = datetime.now(UTC)
        self._last_request_at = now
        self._requests_in_window.append(now)

        # Prune old entries (keep last 60 seconds)
        cutoff = now - timedelta(seconds=60)
        self._requests_in_window = [t for t in self._requests_in_window if t > cutoff]

    def on_request_complete(self, headers: dict[str, str] | None = None) -> None:
        """Record that a request completed.

        Optionally pass response headers to update the rate limit monitor.

        Args:
            headers: Optional response headers containing rate limit info
        """
        if headers:
            self._monitor.update_from_headers(headers)

    # -------------------------------------------------------------------------
    # Forced Wait
    # -------------------------------------------------------------------------
    def force_wait(self, seconds: float) -> None:
        """Force waiting for a specified duration.

        Use this when a rate limit error is received to wait until reset.

        Args:
            seconds: Number of seconds to wait
        """
        self._wait_until = datetime.now(UTC) + timedelta(seconds=seconds)
        logger.info("Forced wait set for %.1f seconds", seconds)

    def force_wait_until(self, reset_at: datetime) -> None:
        """Force waiting until a specific time.

        Args:
            reset_at: UTC datetime to wait until
        """
        self._wait_until = reset_at
        wait_seconds = max(0, (reset_at - datetime.now(UTC)).total_seconds())
        logger.info("Forced wait until %s (%.1f seconds)", reset_at.isoformat(), wait_seconds)

    def clear_forced_wait(self) -> None:
        """Clear any forced wait state."""
        self._wait_until = None

    @property
    def is_forced_wait_active(self) -> bool:
        """Check if a forced wait is currently active."""
        if self._wait_until is None:
            return False
        return datetime.now(UTC) < self._wait_until

    @property
    def forced_wait_remaining(self) -> float:
        """Get seconds remaining in forced wait (0 if not active)."""
        if self._wait_until is None:
            return 0.0
        remaining = (self._wait_until - datetime.now(UTC)).total_seconds()
        return max(0.0, remaining)

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------
    @property
    def requests_per_minute(self) -> float:
        """Current request velocity (requests per minute).

        Based on requests tracked in the last 60 seconds.
        """
        if not self._requests_in_window:
            return 0.0

        # Prune old entries first
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=60)
        self._requests_in_window = [t for t in self._requests_in_window if t > cutoff]

        return float(len(self._requests_in_window))

    @property
    def last_request_at(self) -> datetime | None:
        """Timestamp of the last request start."""
        return self._last_request_at

    def get_stats(self) -> dict[str, float | int | str | None]:
        """Get pacer statistics for monitoring.

        Returns:
            Dict with current pacing statistics
        """
        pool_limit = self._monitor.get_pool_limit()
        status = self._monitor.get_status()

        return {
            "requests_per_minute": round(self.requests_per_minute, 2),
            "recommended_delay_ms": round(self.get_recommended_delay() * 1000, 2),
            "throttle_multiplier": self._get_throttle_multiplier(status),
            "status": status.value,
            "remaining": pool_limit.remaining if pool_limit else None,
            "seconds_until_reset": pool_limit.seconds_until_reset if pool_limit else None,
            "is_forced_wait": self.is_forced_wait_active,
            "forced_wait_remaining": round(self.forced_wait_remaining, 2),
        }


async def wait_with_pacer(pacer: RequestPacer) -> None:
    """Async helper to wait for the recommended delay.

    Usage:
        await wait_with_pacer(pacer)
        # Make request...

    Args:
        pacer: RequestPacer instance
    """
    delay = pacer.get_recommended_delay()
    if delay > 0:
        logger.debug("Waiting %.2f seconds before request", delay)
        await asyncio.sleep(delay)
