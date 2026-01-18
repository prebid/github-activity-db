"""Pydantic schemas for GitHub API rate limit data.

These schemas represent rate limit information from:
- GET /rate_limit API endpoint
- x-ratelimit-* response headers
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, computed_field


class RateLimitPool(StrEnum):
    """GitHub rate limit resource pools.

    Each pool has its own separate quota. Most operations use 'core'.
    See: https://docs.github.com/en/rest/rate-limit/rate-limit
    """

    CORE = "core"
    SEARCH = "search"
    GRAPHQL = "graphql"
    CODE_SEARCH = "code_search"
    INTEGRATION_MANIFEST = "integration_manifest"
    DEPENDENCY_SNAPSHOTS = "dependency_snapshots"
    CODE_SCANNING_UPLOAD = "code_scanning_upload"
    ACTIONS_RUNNER_REGISTRATION = "actions_runner_registration"
    SCIM = "scim"


class RateLimitStatus(StrEnum):
    """Rate limit health status.

    Used to categorize the current state of rate limit quota.
    Thresholds are configurable but defaults are:
    - HEALTHY: > 50% remaining
    - WARNING: 20-50% remaining
    - CRITICAL: 5-20% remaining
    - EXHAUSTED: 0 remaining
    """

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


class PoolRateLimit(BaseModel):
    """Rate limit information for a single resource pool.

    Represents the quota state for one GitHub API resource pool
    (e.g., core, search, graphql).
    """

    pool: RateLimitPool = Field(description="Resource pool name")
    limit: int = Field(ge=0, description="Maximum requests allowed per hour")
    remaining: int = Field(ge=0, description="Requests remaining in current window")
    used: int = Field(ge=0, description="Requests used in current window")
    reset_at: datetime = Field(description="UTC datetime when limit resets")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def usage_percent(self) -> float:
        """Percentage of rate limit consumed (0.0 to 100.0)."""
        if self.limit == 0:
            return 100.0
        return (self.used / self.limit) * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining_percent(self) -> float:
        """Percentage of rate limit remaining (0.0 to 100.0)."""
        return 100.0 - self.usage_percent

    @property
    def seconds_until_reset(self) -> int:
        """Seconds until rate limit resets (0 if already past)."""
        delta = self.reset_at - datetime.now(UTC)
        return max(0, int(delta.total_seconds()))

    def get_status(
        self,
        healthy_threshold: float = 50.0,
        warning_threshold: float = 20.0,
        critical_threshold: float = 5.0,
    ) -> RateLimitStatus:
        """Determine rate limit health status.

        Args:
            healthy_threshold: % remaining above which is HEALTHY
            warning_threshold: % remaining above which is WARNING (below healthy)
            critical_threshold: % remaining above which is CRITICAL (below warning)

        Returns:
            RateLimitStatus enum value
        """
        if self.remaining == 0:
            return RateLimitStatus.EXHAUSTED
        if self.remaining_percent >= healthy_threshold:
            return RateLimitStatus.HEALTHY
        if self.remaining_percent >= warning_threshold:
            return RateLimitStatus.WARNING
        return RateLimitStatus.CRITICAL


class RateLimitSnapshot(BaseModel):
    """Complete rate limit snapshot across all pools.

    Represents a point-in-time view of all rate limit pools,
    either from the /rate_limit API or accumulated from response headers.
    """

    timestamp: datetime = Field(description="When this snapshot was taken")
    pools: dict[RateLimitPool, PoolRateLimit] = Field(
        default_factory=dict, description="Rate limits by pool"
    )

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> Self:
        """Parse from GitHub /rate_limit API response.

        Args:
            data: Raw API response dict with 'resources' key

        Returns:
            RateLimitSnapshot instance
        """
        pools: dict[RateLimitPool, PoolRateLimit] = {}
        resources = data.get("resources", {})

        for pool in RateLimitPool:
            pool_key = pool.value
            if pool_key in resources:
                r = resources[pool_key]
                pools[pool] = PoolRateLimit(
                    pool=pool,
                    limit=r["limit"],
                    remaining=r["remaining"],
                    used=r["used"],
                    reset_at=datetime.fromtimestamp(r["reset"], tz=UTC),
                )

        return cls(timestamp=datetime.now(UTC), pools=pools)

    @classmethod
    def from_response_headers(
        cls,
        headers: dict[str, str],
        default_pool: RateLimitPool = RateLimitPool.CORE,
    ) -> Self:
        """Parse from HTTP response headers.

        GitHub includes rate limit info in headers on every response:
        - x-ratelimit-limit
        - x-ratelimit-remaining
        - x-ratelimit-used
        - x-ratelimit-reset
        - x-ratelimit-resource (pool name)

        Args:
            headers: HTTP response headers dict
            default_pool: Default pool if not specified in headers

        Returns:
            RateLimitSnapshot with single pool from headers
        """
        # Determine the resource pool
        resource = headers.get("x-ratelimit-resource", default_pool.value)
        try:
            actual_pool = RateLimitPool(resource)
        except ValueError:
            actual_pool = default_pool

        # Parse values with sensible defaults
        limit = int(headers.get("x-ratelimit-limit", "5000"))
        remaining = int(headers.get("x-ratelimit-remaining", "5000"))
        used = int(headers.get("x-ratelimit-used", "0"))
        reset_ts = int(headers.get("x-ratelimit-reset", "0"))

        reset_at = datetime.fromtimestamp(reset_ts, tz=UTC) if reset_ts > 0 else datetime.now(UTC)

        pool_limit = PoolRateLimit(
            pool=actual_pool,
            limit=limit,
            remaining=remaining,
            used=used,
            reset_at=reset_at,
        )

        return cls(
            timestamp=datetime.now(UTC),
            pools={actual_pool: pool_limit},
        )

    def get_pool(self, pool: RateLimitPool) -> PoolRateLimit | None:
        """Get rate limit for a specific pool.

        Args:
            pool: Rate limit pool to query

        Returns:
            PoolRateLimit or None if no data for that pool
        """
        return self.pools.get(pool)

    def get_core(self) -> PoolRateLimit | None:
        """Convenience accessor for core pool (most common)."""
        return self.pools.get(RateLimitPool.CORE)

    def merge(self, other: "RateLimitSnapshot") -> "RateLimitSnapshot":
        """Merge another snapshot into this one.

        Updates pools from other snapshot, keeping newer data.

        Args:
            other: Snapshot to merge from

        Returns:
            New snapshot with merged pools
        """
        merged_pools = dict(self.pools)
        merged_pools.update(other.pools)
        return RateLimitSnapshot(
            timestamp=max(self.timestamp, other.timestamp),
            pools=merged_pools,
        )


class TokenInfo(BaseModel):
    """Information about the authenticated GitHub token.

    Used to verify that requests are using a properly authenticated
    Personal Access Token (PAT) rather than unauthenticated access.
    """

    is_authenticated: bool = Field(description="Whether token is valid and authenticated")
    rate_limit: int = Field(description="Rate limit (5000=PAT, 60=unauthenticated)")
    token_type: str = Field(description="Token type description")

    @property
    def is_pat(self) -> bool:
        """Whether using a Personal Access Token (vs unauthenticated).

        PAT tokens get 5000 requests/hour, unauthenticated gets 60.
        """
        return self.rate_limit >= 5000

    @classmethod
    def from_rate_limit(cls, limit: int) -> Self:
        """Create TokenInfo from rate limit value.

        Args:
            limit: Rate limit from API response

        Returns:
            TokenInfo instance
        """
        is_authenticated = limit >= 5000
        token_type = "PAT" if is_authenticated else "unauthenticated"
        return cls(
            is_authenticated=is_authenticated,
            rate_limit=limit,
            token_type=token_type,
        )
