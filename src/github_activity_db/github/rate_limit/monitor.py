"""Rate limit monitoring for GitHub API.

This module provides proactive rate limit monitoring to avoid hitting
GitHub API limits during sync operations.

Key Features:
- Passive tracking from response headers (zero API cost)
- PAT verification (5000/hour vs 60/hour)
- Configurable warning thresholds
- Multiple rate limit pool support
- Observable via callbacks and status methods
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from github_activity_db.config import RateLimitConfig, get_settings

from .schemas import (
    PoolRateLimit,
    RateLimitPool,
    RateLimitSnapshot,
    RateLimitStatus,
    TokenInfo,
)

if TYPE_CHECKING:
    from typing import Any

    GitHub = Any  # Avoid generic type parameter issues

logger = logging.getLogger(__name__)

# Type for threshold callbacks
ThresholdCallback = Callable[[PoolRateLimit, RateLimitStatus], Awaitable[None] | None]


class RateLimitMonitor:
    """Monitors GitHub API rate limits proactively.

    This class tracks rate limit state passively from response headers,
    avoiding additional API calls. It provides methods to check current
    status, verify PAT authentication, and register callbacks for
    threshold crossings.

    Usage:
        # Standalone with explicit client
        async with GitHubClient() as client:
            monitor = RateLimitMonitor(client._github)
            await monitor.initialize()

            if monitor.can_make_request():
                prs = await client.list_pull_requests(...)
                monitor.update_from_headers(response.headers)

        # Or integrated into GitHubClient (preferred)
        async with GitHubClient() as client:
            status = client.rate_monitor.get_status()
            if status != RateLimitStatus.EXHAUSTED:
                prs = await client.list_pull_requests(...)

    The monitor tracks rate limits passively from response headers
    to minimize API consumption.
    """

    def __init__(
        self,
        github: GitHub | None = None,
        config: RateLimitConfig | None = None,
    ) -> None:
        """Initialize the rate limit monitor.

        Args:
            github: Optional githubkit GitHub instance for fetching initial limits
            config: Optional rate limit configuration (uses settings if not provided)
        """
        self._github = github
        self._config = config or get_settings().rate_limit

        # State
        self._snapshot: RateLimitSnapshot | None = None
        self._last_fetch: datetime | None = None
        self._token_info: TokenInfo | None = None
        self._initialized: bool = False

        # Callbacks
        self._threshold_callbacks: list[ThresholdCallback] = []
        self._previous_status: dict[RateLimitPool, RateLimitStatus] = {}
        self._callback_tasks: set[asyncio.Task[None]] = set()  # Prevent task GC

        # Lock for thread-safe updates
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------------------
    # Initialization & Token Verification
    # -------------------------------------------------------------------------
    async def initialize(self) -> None:
        """Initialize the monitor by fetching current rate limits.

        This verifies the token is authenticated and populates initial state.
        Should be called once when the client is created.

        Note: The /rate_limit endpoint is free and does not count against quota.

        Raises:
            RuntimeError: If no GitHub client provided and trying to initialize
        """
        if self._initialized:
            return

        if self._github is None:
            # Can work without initialization if headers are provided later
            logger.debug("No GitHub client provided, monitor will track from headers only")
            self._initialized = True
            return

        async with self._lock:
            snapshot = await self._fetch_rate_limits()
            self._snapshot = snapshot
            self._last_fetch = datetime.now(UTC)

            # Verify token type from core limit
            core = snapshot.get_core()
            if core:
                self._token_info = TokenInfo.from_rate_limit(core.limit)

                if not self._token_info.is_pat:
                    logger.warning(
                        "GitHub token appears unauthenticated (limit=%d, expected=5000)",
                        core.limit,
                    )

            self._initialized = True
            logger.info(
                "Rate limit monitor initialized (core_remaining=%s, token_type=%s)",
                core.remaining if core else "unknown",
                self._token_info.token_type if self._token_info else "unknown",
            )

    async def _fetch_rate_limits(self) -> RateLimitSnapshot:
        """Fetch rate limits from API (costs 0 requests).

        The /rate_limit endpoint does not count against your limit.
        """
        if self._github is None:
            raise RuntimeError("Cannot fetch rate limits without GitHub client")

        try:
            resp = await self._github.rest.rate_limit.async_get()
            data = resp.parsed_data.model_dump()
            return RateLimitSnapshot.from_api_response(data)
        except Exception as e:
            logger.error("Failed to fetch rate limits: %s", e)
            raise

    def verify_pat(self) -> bool:
        """Check if the token is a properly authenticated PAT.

        Returns:
            True if rate limit is >= 5000/hour (authenticated PAT)
            False if rate limit is 60/hour (unauthenticated)
            False if not initialized

        Note:
            This method does not raise exceptions. If monitor is not
            initialized, it returns False with a warning log.
        """
        if not self._initialized:
            logger.warning("Monitor not initialized, cannot verify PAT")
            return False

        if self._token_info is None:
            # Try to determine from snapshot
            if self._snapshot:
                core = self._snapshot.get_core()
                if core:
                    self._token_info = TokenInfo.from_rate_limit(core.limit)
                    return self._token_info.is_pat

            return False

        return self._token_info.is_pat

    @property
    def token_info(self) -> TokenInfo | None:
        """Get token information (None if not initialized)."""
        return self._token_info

    @property
    def is_initialized(self) -> bool:
        """Whether the monitor has been initialized."""
        return self._initialized

    # -------------------------------------------------------------------------
    # Passive Tracking (from Response Headers)
    # -------------------------------------------------------------------------
    def update_from_headers(
        self,
        headers: dict[str, str],
        pool: RateLimitPool = RateLimitPool.CORE,
    ) -> None:
        """Update rate limit state from response headers.

        This is the preferred way to track limits as it has zero API cost.
        Call this after every API request.

        Args:
            headers: HTTP response headers dict
            pool: Default pool if not specified in headers
        """
        if not self._config.track_from_headers:
            return

        # Parse headers
        partial = RateLimitSnapshot.from_response_headers(headers, pool)

        # Merge into existing snapshot or create new
        if self._snapshot is None:
            self._snapshot = partial
        else:
            self._snapshot = self._snapshot.merge(partial)

        # Update token info if we didn't have it
        if self._token_info is None:
            for pool_limit in partial.pools.values():
                self._token_info = TokenInfo.from_rate_limit(pool_limit.limit)
                break

        # Mark as initialized if we have data
        if not self._initialized:
            self._initialized = True

        # Check thresholds and fire callbacks
        self._check_thresholds_sync()

    def _check_thresholds_sync(self) -> None:
        """Check if thresholds crossed and fire callbacks (sync version)."""
        if not self._snapshot or not self._threshold_callbacks:
            return

        for pool, limit in self._snapshot.pools.items():
            current_status = limit.get_status(
                self._config.healthy_threshold_pct,
                self._config.warning_threshold_pct,
                self._config.critical_threshold_pct,
            )
            previous_status = self._previous_status.get(pool, RateLimitStatus.HEALTHY)

            # Fire callback on status change (degradation only)
            if current_status != previous_status:
                if self._is_degradation(previous_status, current_status):
                    for callback in self._threshold_callbacks:
                        try:
                            result = callback(limit, current_status)
                            if asyncio.iscoroutine(result):
                                # Schedule coroutine to run
                                task = asyncio.create_task(result)
                                self._callback_tasks.add(task)
                                task.add_done_callback(self._callback_tasks.discard)
                        except Exception as e:
                            logger.error(
                                "Threshold callback failed for pool %s: %s",
                                pool.value,
                                e,
                            )

                self._previous_status[pool] = current_status

    @staticmethod
    def _is_degradation(previous: RateLimitStatus, current: RateLimitStatus) -> bool:
        """Check if status change is a degradation (worse status)."""
        order = [
            RateLimitStatus.HEALTHY,
            RateLimitStatus.WARNING,
            RateLimitStatus.CRITICAL,
            RateLimitStatus.EXHAUSTED,
        ]
        return order.index(current) > order.index(previous)

    # -------------------------------------------------------------------------
    # Query Methods
    # -------------------------------------------------------------------------
    @property
    def snapshot(self) -> RateLimitSnapshot | None:
        """Get current rate limit snapshot (None if never fetched/tracked)."""
        return self._snapshot

    def get_pool_limit(
        self,
        pool: RateLimitPool = RateLimitPool.CORE,
    ) -> PoolRateLimit | None:
        """Get rate limit info for a specific pool.

        Args:
            pool: Rate limit pool to query

        Returns:
            PoolRateLimit or None if no data available
        """
        if self._snapshot is None:
            return None
        return self._snapshot.get_pool(pool)

    def get_status(
        self,
        pool: RateLimitPool = RateLimitPool.CORE,
    ) -> RateLimitStatus:
        """Get health status for a pool.

        Args:
            pool: Rate limit pool to check

        Returns:
            RateLimitStatus enum value (HEALTHY if unknown)
        """
        limit = self.get_pool_limit(pool)
        if limit is None:
            return RateLimitStatus.HEALTHY  # Assume OK if unknown

        return limit.get_status(
            self._config.healthy_threshold_pct,
            self._config.warning_threshold_pct,
            self._config.critical_threshold_pct,
        )

    def can_make_request(
        self,
        pool: RateLimitPool = RateLimitPool.CORE,
        count: int = 1,
    ) -> bool:
        """Check if we can safely make request(s).

        This accounts for the configured buffer.

        Args:
            pool: Rate limit pool to check
            count: Number of requests to make

        Returns:
            True if remaining >= count + buffer
        """
        limit = self.get_pool_limit(pool)
        if limit is None:
            # No data - assume OK but log warning
            logger.warning("No rate limit data available for pool %s", pool.value)
            return True

        buffer = self._config.min_remaining_buffer
        return limit.remaining >= (count + buffer)

    def requests_available(
        self,
        pool: RateLimitPool = RateLimitPool.CORE,
    ) -> int:
        """Get number of requests available (minus buffer).

        Args:
            pool: Rate limit pool to check

        Returns:
            Available requests (0 if no data or exhausted)
        """
        limit = self.get_pool_limit(pool)
        if limit is None:
            return 0

        available = limit.remaining - self._config.min_remaining_buffer
        return max(0, available)

    def time_until_reset(
        self,
        pool: RateLimitPool = RateLimitPool.CORE,
    ) -> int:
        """Get seconds until rate limit resets.

        Args:
            pool: Rate limit pool to check

        Returns:
            Seconds until reset (0 if no data or already reset)
        """
        limit = self.get_pool_limit(pool)
        if limit is None:
            return 0
        return limit.seconds_until_reset

    # -------------------------------------------------------------------------
    # Explicit Refresh
    # -------------------------------------------------------------------------
    async def refresh(self) -> RateLimitSnapshot:
        """Force refresh rate limits from API.

        Use sparingly - prefer passive tracking via update_from_headers().
        The /rate_limit endpoint is free but this is still an HTTP call.

        Returns:
            Fresh RateLimitSnapshot

        Raises:
            RuntimeError: If no GitHub client available
        """
        if self._github is None:
            raise RuntimeError("Cannot refresh without GitHub client")

        async with self._lock:
            self._snapshot = await self._fetch_rate_limits()
            self._last_fetch = datetime.now(UTC)
            return self._snapshot

    # -------------------------------------------------------------------------
    # Callbacks & Observability
    # -------------------------------------------------------------------------
    def on_threshold_crossed(self, callback: ThresholdCallback) -> None:
        """Register a callback for threshold crossings.

        The callback receives the PoolRateLimit and new RateLimitStatus
        when status degrades (e.g., HEALTHY -> WARNING).

        Callbacks are NOT fired on improvement (e.g., CRITICAL -> WARNING).

        Args:
            callback: Async or sync function to call on degradation
        """
        self._threshold_callbacks.append(callback)

    def remove_callback(self, callback: ThresholdCallback) -> bool:
        """Remove a previously registered callback.

        Args:
            callback: The callback to remove

        Returns:
            True if callback was found and removed
        """
        try:
            self._threshold_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    def to_dict(self) -> dict[str, Any]:
        """Export current state as dictionary (for logging/metrics).

        Returns:
            Dict with all rate limit data
        """
        if self._snapshot is None:
            return {"initialized": self._initialized, "pools": {}}

        pools_data: dict[str, Any] = {}
        for pool, limit in self._snapshot.pools.items():
            pools_data[pool.value] = {
                "limit": limit.limit,
                "remaining": limit.remaining,
                "used": limit.used,
                "usage_percent": round(limit.usage_percent, 2),
                "remaining_percent": round(limit.remaining_percent, 2),
                "reset_at": limit.reset_at.isoformat(),
                "seconds_until_reset": limit.seconds_until_reset,
                "status": self.get_status(pool).value,
            }

        return {
            "initialized": self._initialized,
            "timestamp": self._snapshot.timestamp.isoformat(),
            "token": self._token_info.model_dump() if self._token_info else None,
            "pools": pools_data,
        }
