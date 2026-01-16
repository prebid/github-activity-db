"""Contract tests for rate limit Pydantic schemas.

These tests verify that schemas correctly parse GitHub API responses
and response headers.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest

from github_activity_db.github.rate_limit.schemas import (
    PoolRateLimit,
    RateLimitPool,
    RateLimitSnapshot,
    RateLimitStatus,
    TokenInfo,
)
from tests.fixtures.rate_limit_responses import (
    HEADERS_CRITICAL,
    HEADERS_EXHAUSTED,
    HEADERS_GRAPHQL_POOL,
    HEADERS_HEALTHY,
    HEADERS_PARTIAL,
    HEADERS_SEARCH_POOL,
    HEADERS_UNAUTHENTICATED,
    HEADERS_WARNING,
    HEADERS_ZERO_LIMIT,
    RATE_LIMIT_RESPONSE_CRITICAL,
    RATE_LIMIT_RESPONSE_EXHAUSTED,
    RATE_LIMIT_RESPONSE_HEALTHY,
    RATE_LIMIT_RESPONSE_MINIMAL,
    RATE_LIMIT_RESPONSE_UNAUTHENTICATED,
    RATE_LIMIT_RESPONSE_WARNING,
    make_rate_limit_headers,
)


class TestRateLimitPool:
    """Tests for RateLimitPool enum."""

    def test_all_expected_pools_exist(self) -> None:
        """All documented GitHub rate limit pools should be defined."""
        expected = {"core", "search", "graphql", "code_search", "integration_manifest"}
        actual = {pool.value for pool in RateLimitPool}
        assert expected.issubset(actual)

    def test_pool_values_are_strings(self) -> None:
        """Pool enum values should be lowercase strings."""
        for pool in RateLimitPool:
            assert isinstance(pool.value, str)
            assert pool.value == pool.value.lower()


class TestRateLimitStatus:
    """Tests for RateLimitStatus enum."""

    def test_all_statuses_exist(self) -> None:
        """All health statuses should be defined."""
        expected = {"healthy", "warning", "critical", "exhausted"}
        actual = {status.value for status in RateLimitStatus}
        assert expected == actual


class TestPoolRateLimit:
    """Tests for PoolRateLimit model."""

    def test_create_pool_rate_limit(self) -> None:
        """Basic creation with valid data."""
        reset_at = datetime.now(UTC) + timedelta(hours=1)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=4500,
            used=500,
            reset_at=reset_at,
        )

        assert limit.pool == RateLimitPool.CORE
        assert limit.limit == 5000
        assert limit.remaining == 4500
        assert limit.used == 500

    def test_usage_percent_calculation(self) -> None:
        """Usage percentage should be calculated correctly."""
        reset_at = datetime.now(UTC)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=4000,
            used=1000,
            reset_at=reset_at,
        )

        assert limit.usage_percent == 20.0
        assert limit.remaining_percent == 80.0

    def test_usage_percent_zero_limit(self) -> None:
        """Zero limit should return 100% usage to avoid division by zero."""
        reset_at = datetime.now(UTC)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=0,
            remaining=0,
            used=0,
            reset_at=reset_at,
        )

        assert limit.usage_percent == 100.0
        assert limit.remaining_percent == 0.0

    def test_seconds_until_reset_future(self) -> None:
        """Seconds until reset should be positive for future reset time."""
        reset_at = datetime.now(UTC) + timedelta(minutes=30)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=4500,
            used=500,
            reset_at=reset_at,
        )

        # Should be around 1800 seconds (30 min), allow some tolerance
        assert 1795 <= limit.seconds_until_reset <= 1805

    def test_seconds_until_reset_past(self) -> None:
        """Seconds until reset should be 0 for past reset time."""
        reset_at = datetime.now(UTC) - timedelta(minutes=5)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=5000,
            used=0,
            reset_at=reset_at,
        )

        assert limit.seconds_until_reset == 0

    def test_status_healthy(self) -> None:
        """Status should be HEALTHY when > 50% remaining."""
        reset_at = datetime.now(UTC)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=4000,  # 80% remaining
            used=1000,
            reset_at=reset_at,
        )

        assert limit.get_status() == RateLimitStatus.HEALTHY

    def test_status_warning(self) -> None:
        """Status should be WARNING when 20-50% remaining."""
        reset_at = datetime.now(UTC)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=1500,  # 30% remaining
            used=3500,
            reset_at=reset_at,
        )

        assert limit.get_status() == RateLimitStatus.WARNING

    def test_status_critical(self) -> None:
        """Status should be CRITICAL when 5-20% remaining."""
        reset_at = datetime.now(UTC)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=500,  # 10% remaining
            used=4500,
            reset_at=reset_at,
        )

        assert limit.get_status() == RateLimitStatus.CRITICAL

    def test_status_exhausted(self) -> None:
        """Status should be EXHAUSTED when 0 remaining."""
        reset_at = datetime.now(UTC)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=0,
            used=5000,
            reset_at=reset_at,
        )

        assert limit.get_status() == RateLimitStatus.EXHAUSTED

    def test_status_custom_thresholds(self) -> None:
        """Status should respect custom thresholds."""
        reset_at = datetime.now(UTC)
        limit = PoolRateLimit(
            pool=RateLimitPool.CORE,
            limit=5000,
            remaining=2000,  # 40% remaining
            used=3000,
            reset_at=reset_at,
        )

        # With default thresholds (50/20/5), 40% is WARNING
        assert limit.get_status() == RateLimitStatus.WARNING

        # With custom threshold (30% for healthy), 40% is HEALTHY
        assert limit.get_status(healthy_threshold=30.0) == RateLimitStatus.HEALTHY


class TestRateLimitSnapshot:
    """Tests for RateLimitSnapshot model."""

    def test_from_api_response_healthy(self) -> None:
        """Parse healthy rate limit API response."""
        snapshot = RateLimitSnapshot.from_api_response(RATE_LIMIT_RESPONSE_HEALTHY)

        assert RateLimitPool.CORE in snapshot.pools
        assert RateLimitPool.SEARCH in snapshot.pools
        assert RateLimitPool.GRAPHQL in snapshot.pools

        core = snapshot.get_core()
        assert core is not None
        assert core.limit == 5000
        assert core.remaining == 4500
        assert core.used == 500

    def test_from_api_response_warning(self) -> None:
        """Parse warning-level rate limit API response."""
        snapshot = RateLimitSnapshot.from_api_response(RATE_LIMIT_RESPONSE_WARNING)

        core = snapshot.get_core()
        assert core is not None
        assert core.remaining == 1500
        assert core.get_status() == RateLimitStatus.WARNING

    def test_from_api_response_critical(self) -> None:
        """Parse critical-level rate limit API response."""
        snapshot = RateLimitSnapshot.from_api_response(RATE_LIMIT_RESPONSE_CRITICAL)

        core = snapshot.get_core()
        assert core is not None
        assert core.remaining == 250
        assert core.get_status() == RateLimitStatus.CRITICAL

    def test_from_api_response_exhausted(self) -> None:
        """Parse exhausted rate limit API response."""
        snapshot = RateLimitSnapshot.from_api_response(RATE_LIMIT_RESPONSE_EXHAUSTED)

        core = snapshot.get_core()
        assert core is not None
        assert core.remaining == 0
        assert core.get_status() == RateLimitStatus.EXHAUSTED

    def test_from_api_response_unauthenticated(self) -> None:
        """Parse unauthenticated rate limit API response."""
        snapshot = RateLimitSnapshot.from_api_response(RATE_LIMIT_RESPONSE_UNAUTHENTICATED)

        core = snapshot.get_core()
        assert core is not None
        assert core.limit == 60  # Unauthenticated limit
        assert core.remaining == 55

    def test_from_api_response_minimal(self) -> None:
        """Parse minimal response with only core pool."""
        snapshot = RateLimitSnapshot.from_api_response(RATE_LIMIT_RESPONSE_MINIMAL)

        assert RateLimitPool.CORE in snapshot.pools
        assert len(snapshot.pools) == 1  # Only core

    def test_from_response_headers_healthy(self) -> None:
        """Parse healthy rate limit headers."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_HEALTHY)

        core = snapshot.get_core()
        assert core is not None
        assert core.limit == 5000
        assert core.remaining == 4500

    def test_from_response_headers_warning(self) -> None:
        """Parse warning-level headers."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_WARNING)

        core = snapshot.get_core()
        assert core is not None
        assert core.get_status() == RateLimitStatus.WARNING

    def test_from_response_headers_critical(self) -> None:
        """Parse critical-level headers."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_CRITICAL)

        core = snapshot.get_core()
        assert core is not None
        assert core.get_status() == RateLimitStatus.CRITICAL

    def test_from_response_headers_exhausted(self) -> None:
        """Parse exhausted headers."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_EXHAUSTED)

        core = snapshot.get_core()
        assert core is not None
        assert core.get_status() == RateLimitStatus.EXHAUSTED

    def test_from_response_headers_search_pool(self) -> None:
        """Parse headers with search pool resource."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_SEARCH_POOL)

        # Should have search pool, not core
        assert RateLimitPool.SEARCH in snapshot.pools
        search = snapshot.get_pool(RateLimitPool.SEARCH)
        assert search is not None
        assert search.limit == 30

    def test_from_response_headers_graphql_pool(self) -> None:
        """Parse headers with graphql pool resource."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_GRAPHQL_POOL)

        graphql = snapshot.get_pool(RateLimitPool.GRAPHQL)
        assert graphql is not None
        assert graphql.limit == 5000

    def test_from_response_headers_partial(self) -> None:
        """Parse partial headers (missing some fields)."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_PARTIAL)

        core = snapshot.get_core()
        assert core is not None
        assert core.limit == 5000
        assert core.remaining == 100
        # Missing fields use defaults
        assert core.used == 0

    def test_from_response_headers_zero_limit(self) -> None:
        """Handle zero limit gracefully."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_ZERO_LIMIT)

        core = snapshot.get_core()
        assert core is not None
        assert core.limit == 0
        # Should not crash on percentage calculations
        assert core.usage_percent == 100.0

    def test_from_response_headers_unknown_resource(self) -> None:
        """Unknown resource falls back to default pool."""
        headers = make_rate_limit_headers(resource="unknown_pool")
        snapshot = RateLimitSnapshot.from_response_headers(
            headers, default_pool=RateLimitPool.CORE
        )

        # Should fall back to core
        assert RateLimitPool.CORE in snapshot.pools

    def test_get_pool_returns_none_for_missing(self) -> None:
        """get_pool returns None for pools not in snapshot."""
        snapshot = RateLimitSnapshot.from_response_headers(HEADERS_HEALTHY)

        # Only has core, search should be None
        assert snapshot.get_pool(RateLimitPool.SEARCH) is None

    def test_merge_snapshots(self) -> None:
        """Merge two snapshots combines pools."""
        snapshot1 = RateLimitSnapshot.from_response_headers(HEADERS_HEALTHY)
        snapshot2 = RateLimitSnapshot.from_response_headers(HEADERS_SEARCH_POOL)

        merged = snapshot1.merge(snapshot2)

        assert RateLimitPool.CORE in merged.pools
        assert RateLimitPool.SEARCH in merged.pools

    def test_merge_overwrites_same_pool(self) -> None:
        """Merging overwrites existing pool data."""
        headers1 = make_rate_limit_headers(remaining=4500)
        headers2 = make_rate_limit_headers(remaining=4000)

        snapshot1 = RateLimitSnapshot.from_response_headers(headers1)
        snapshot2 = RateLimitSnapshot.from_response_headers(headers2)

        merged = snapshot1.merge(snapshot2)

        core = merged.get_core()
        assert core is not None
        assert core.remaining == 4000  # From snapshot2


class TestTokenInfo:
    """Tests for TokenInfo model."""

    def test_create_token_info_authenticated(self) -> None:
        """Create TokenInfo for authenticated PAT."""
        info = TokenInfo(
            is_authenticated=True,
            rate_limit=5000,
            token_type="PAT",
        )

        assert info.is_authenticated is True
        assert info.rate_limit == 5000
        assert info.is_pat is True

    def test_create_token_info_unauthenticated(self) -> None:
        """Create TokenInfo for unauthenticated access."""
        info = TokenInfo(
            is_authenticated=False,
            rate_limit=60,
            token_type="unauthenticated",
        )

        assert info.is_authenticated is False
        assert info.rate_limit == 60
        assert info.is_pat is False

    def test_from_rate_limit_pat(self) -> None:
        """Factory method detects PAT from 5000 limit."""
        info = TokenInfo.from_rate_limit(5000)

        assert info.is_authenticated is True
        assert info.is_pat is True
        assert info.token_type == "PAT"

    def test_from_rate_limit_unauthenticated(self) -> None:
        """Factory method detects unauthenticated from 60 limit."""
        info = TokenInfo.from_rate_limit(60)

        assert info.is_authenticated is False
        assert info.is_pat is False
        assert info.token_type == "unauthenticated"

    def test_is_pat_threshold(self) -> None:
        """is_pat should be True for limit >= 5000."""
        # Edge case: exactly 5000
        assert TokenInfo.from_rate_limit(5000).is_pat is True

        # GitHub Apps can have higher limits
        assert TokenInfo.from_rate_limit(15000).is_pat is True

        # Below 5000 is not a PAT
        assert TokenInfo.from_rate_limit(4999).is_pat is False
        assert TokenInfo.from_rate_limit(60).is_pat is False


class TestResetTimestampParsing:
    """Tests for reset timestamp parsing edge cases."""

    def test_reset_timestamp_in_past(self) -> None:
        """Handle reset timestamp in the past."""
        past_time = int(time.time()) - 3600  # 1 hour ago
        headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "5000",
            "x-ratelimit-used": "0",
            "x-ratelimit-reset": str(past_time),
            "x-ratelimit-resource": "core",
        }

        snapshot = RateLimitSnapshot.from_response_headers(headers)
        core = snapshot.get_core()
        assert core is not None
        assert core.seconds_until_reset == 0

    def test_reset_timestamp_zero(self) -> None:
        """Handle zero reset timestamp."""
        headers = {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "5000",
            "x-ratelimit-used": "0",
            "x-ratelimit-reset": "0",
            "x-ratelimit-resource": "core",
        }

        snapshot = RateLimitSnapshot.from_response_headers(headers)
        core = snapshot.get_core()
        assert core is not None
        # Should use current time as fallback, so seconds_until_reset should be ~0
        assert core.seconds_until_reset >= 0
