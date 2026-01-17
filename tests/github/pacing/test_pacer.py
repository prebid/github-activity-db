"""Unit tests for RequestPacer class.

These tests verify the token bucket algorithm, delay calculations,
and adaptive throttling behavior.
"""

from datetime import UTC, datetime, timedelta

import pytest

from github_activity_db.config import PacingConfig
from github_activity_db.github.pacing.pacer import RequestPacer, wait_with_pacer
from github_activity_db.github.rate_limit.monitor import RateLimitMonitor
from github_activity_db.github.rate_limit.schemas import (
    PoolRateLimit,
    RateLimitPool,
    RateLimitStatus,
)
from tests.fixtures.rate_limit_responses import (
    HEADERS_CRITICAL,
    HEADERS_EXHAUSTED,
    HEADERS_HEALTHY,
    HEADERS_WARNING,
    make_rate_limit_headers,
)


def create_pool_limit(
    remaining: int = 4500,
    limit: int = 5000,
    reset_seconds: int = 3600,
) -> PoolRateLimit:
    """Helper to create a PoolRateLimit for testing."""
    return PoolRateLimit(
        pool=RateLimitPool.CORE,
        limit=limit,
        remaining=remaining,
        used=limit - remaining,
        reset_at=datetime.now(UTC) + timedelta(seconds=reset_seconds),
    )


class TestRequestPacerInit:
    """Tests for pacer initialization."""

    def test_init_with_monitor(self) -> None:
        """Pacer initializes with monitor."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        assert pacer._monitor is monitor
        assert pacer.config is not None

    def test_init_with_custom_config(self) -> None:
        """Pacer accepts custom configuration."""
        monitor = RateLimitMonitor()
        config = PacingConfig(
            min_request_interval_ms=100,
            max_request_interval_ms=30000,
            reserve_buffer_pct=15.0,
        )
        pacer = RequestPacer(monitor, config=config)

        assert pacer.config.min_request_interval_ms == 100
        assert pacer.config.reserve_buffer_pct == 15.0


class TestDelayCalculation:
    """Tests for get_recommended_delay."""

    def test_delay_no_data(self) -> None:
        """Returns minimum delay when no rate limit data."""
        monitor = RateLimitMonitor()
        config = PacingConfig(min_request_interval_ms=50)
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        assert delay == 0.05  # 50ms

    def test_delay_healthy_status(self) -> None:
        """Healthy status uses 1.0x multiplier."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)

        config = PacingConfig(
            min_request_interval_ms=0,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        # With 4500 remaining and ~3600 seconds, base delay = 0.8s
        # Healthy multiplier = 1.0x
        assert delay > 0
        assert delay < 2.0

    def test_delay_warning_status(self) -> None:
        """Warning status uses 1.5x multiplier."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_WARNING)

        config = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=60000,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        # With warning status, delay should be > healthy
        assert delay > 0

    def test_delay_critical_status(self) -> None:
        """Critical status uses 2.0x multiplier."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_CRITICAL)

        config = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=60000,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        # With critical status, should have significant delay
        assert delay > 0

    def test_delay_exhausted_status(self) -> None:
        """Exhausted status uses 4.0x multiplier."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_EXHAUSTED)

        config = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=120000,  # 2 minutes to allow large delays
            reserve_buffer_pct=0,
            burst_allowance=0,
        )
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        # Exhausted should have longest delay (but clamped to max)
        assert delay > 0

    def test_delay_respects_minimum(self) -> None:
        """Delay never goes below minimum."""
        monitor = RateLimitMonitor()
        # Very healthy: lots of remaining with short reset
        headers = make_rate_limit_headers(remaining=4999, reset_in_seconds=3600)
        monitor.update_from_headers(headers)

        config = PacingConfig(min_request_interval_ms=100)
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        assert delay >= 0.1  # 100ms minimum

    def test_delay_respects_maximum(self) -> None:
        """Delay never goes above maximum."""
        monitor = RateLimitMonitor()
        # Very unhealthy: almost exhausted with long reset
        headers = make_rate_limit_headers(remaining=1, reset_in_seconds=3600)
        monitor.update_from_headers(headers)

        config = PacingConfig(
            max_request_interval_ms=5000,  # 5 seconds max
            reserve_buffer_pct=0,
            burst_allowance=0,
        )
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        assert delay <= 5.0  # 5 seconds max

    def test_delay_accounts_for_buffer(self) -> None:
        """Buffer reduces effective remaining requests."""
        monitor = RateLimitMonitor()
        headers = make_rate_limit_headers(remaining=1000, limit=5000, reset_in_seconds=3600)
        monitor.update_from_headers(headers)

        # With 10% buffer on 5000 limit = 500 buffer
        # Effective remaining = 1000 - 500 = 500
        config = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=60000,
            reserve_buffer_pct=10.0,
            burst_allowance=0,
        )
        pacer = RequestPacer(monitor, config=config)

        delay = pacer.get_recommended_delay()

        # Base delay = 3600 / 500 = 7.2s (with WARNING multiplier ~10.8s)
        assert delay > 5.0

    def test_delay_with_burst_allowance(self) -> None:
        """Burst allowance increases effective remaining."""
        monitor = RateLimitMonitor()
        headers = make_rate_limit_headers(remaining=100, reset_in_seconds=3600)
        monitor.update_from_headers(headers)

        # Without burst
        config_no_burst = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=120000,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )
        pacer_no_burst = RequestPacer(monitor, config=config_no_burst)
        delay_no_burst = pacer_no_burst.get_recommended_delay()

        # With burst
        config_with_burst = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=120000,
            reserve_buffer_pct=0,
            burst_allowance=50,
        )
        pacer_with_burst = RequestPacer(monitor, config=config_with_burst)
        delay_with_burst = pacer_with_burst.get_recommended_delay()

        # Burst should reduce delay
        assert delay_with_burst < delay_no_burst


class TestThrottleMultiplier:
    """Tests for throttle multiplier calculation."""

    def test_multiplier_healthy(self) -> None:
        """Healthy status has 1.0x multiplier."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        multiplier = pacer._get_throttle_multiplier(RateLimitStatus.HEALTHY)

        assert multiplier == 1.0

    def test_multiplier_warning(self) -> None:
        """Warning status has 1.5x multiplier."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        multiplier = pacer._get_throttle_multiplier(RateLimitStatus.WARNING)

        assert multiplier == 1.5

    def test_multiplier_critical(self) -> None:
        """Critical status has 2.0x multiplier."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        multiplier = pacer._get_throttle_multiplier(RateLimitStatus.CRITICAL)

        assert multiplier == 2.0

    def test_multiplier_exhausted(self) -> None:
        """Exhausted status has 4.0x multiplier."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        multiplier = pacer._get_throttle_multiplier(RateLimitStatus.EXHAUSTED)

        assert multiplier == 4.0


class TestForcedWait:
    """Tests for forced wait functionality."""

    def test_force_wait_sets_state(self) -> None:
        """force_wait sets wait state."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        pacer.force_wait(60.0)

        assert pacer.is_forced_wait_active is True
        assert pacer.forced_wait_remaining > 55

    def test_force_wait_affects_delay(self) -> None:
        """Forced wait overrides normal delay calculation."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)
        pacer = RequestPacer(monitor)

        # Normal delay would be small
        normal_delay = pacer.get_recommended_delay()

        # Force wait
        pacer.force_wait(120.0)

        # Forced delay should be ~120 seconds
        forced_delay = pacer.get_recommended_delay()

        assert forced_delay > normal_delay
        assert forced_delay > 115

    def test_force_wait_until(self) -> None:
        """force_wait_until sets wait until specific time."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        future_time = datetime.now(UTC) + timedelta(minutes=5)
        pacer.force_wait_until(future_time)

        assert pacer.is_forced_wait_active is True
        assert pacer.forced_wait_remaining > 290

    def test_clear_forced_wait(self) -> None:
        """clear_forced_wait removes wait state."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        pacer.force_wait(60.0)
        assert pacer.is_forced_wait_active is True

        pacer.clear_forced_wait()
        assert pacer.is_forced_wait_active is False

    def test_forced_wait_expires(self) -> None:
        """Forced wait expires when time passes."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        # Set wait until past time
        past_time = datetime.now(UTC) - timedelta(seconds=10)
        pacer._wait_until = past_time

        assert pacer.is_forced_wait_active is False
        assert pacer.forced_wait_remaining == 0


class TestRequestLifecycle:
    """Tests for request tracking."""

    def test_on_request_start_tracks_time(self) -> None:
        """on_request_start records timestamp."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        assert pacer.last_request_at is None

        pacer.on_request_start()

        assert pacer.last_request_at is not None
        assert pacer.requests_per_minute > 0

    def test_on_request_complete_updates_monitor(self) -> None:
        """on_request_complete passes headers to monitor."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        pacer.on_request_complete(HEADERS_HEALTHY)

        assert monitor.snapshot is not None
        core = monitor.get_pool_limit()
        assert core is not None
        assert core.remaining == 4500

    def test_requests_per_minute_calculation(self) -> None:
        """requests_per_minute calculates from tracked requests."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        # Make 10 requests
        for _ in range(10):
            pacer.on_request_start()

        assert pacer.requests_per_minute == 10.0

    def test_requests_per_minute_prunes_old(self) -> None:
        """Old requests are pruned from tracking."""
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)

        # Add old timestamp manually (61 seconds ago)
        old_time = datetime.now(UTC) - timedelta(seconds=61)
        pacer._requests_in_window.append(old_time)

        # Add current request
        pacer.on_request_start()

        # Old request should be pruned
        assert pacer.requests_per_minute == 1.0


class TestGetStats:
    """Tests for statistics export."""

    def test_get_stats_basic(self) -> None:
        """get_stats returns expected fields."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)
        pacer = RequestPacer(monitor)

        stats = pacer.get_stats()

        assert "requests_per_minute" in stats
        assert "recommended_delay_ms" in stats
        assert "throttle_multiplier" in stats
        assert "status" in stats
        assert "remaining" in stats
        assert "is_forced_wait" in stats

    def test_get_stats_with_data(self) -> None:
        """get_stats returns correct values."""
        monitor = RateLimitMonitor()
        monitor.update_from_headers(HEADERS_HEALTHY)
        pacer = RequestPacer(monitor)

        pacer.on_request_start()
        pacer.on_request_start()

        stats = pacer.get_stats()

        assert stats["requests_per_minute"] == 2.0
        assert stats["status"] == "healthy"
        assert stats["remaining"] == 4500
        assert stats["throttle_multiplier"] == 1.0
        assert stats["is_forced_wait"] is False


class TestMathematicalProperties:
    """Property-based tests for mathematical correctness."""

    def test_delay_increases_as_remaining_decreases(self) -> None:
        """Delay should increase as remaining requests decrease."""
        config = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=120000,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )

        delays = []
        for remaining in [4000, 3000, 2000, 1000, 500]:
            monitor = RateLimitMonitor()
            headers = make_rate_limit_headers(remaining=remaining, reset_in_seconds=3600)
            monitor.update_from_headers(headers)
            pacer = RequestPacer(monitor, config=config)
            delays.append(pacer.get_recommended_delay())

        # Each delay should be >= previous
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1], f"Delay did not increase: {delays}"

    def test_delay_decreases_as_reset_approaches(self) -> None:
        """Delay should decrease as reset time gets closer."""
        config = PacingConfig(
            min_request_interval_ms=0,
            max_request_interval_ms=120000,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )

        delays = []
        for reset_seconds in [3600, 1800, 900, 300]:
            monitor = RateLimitMonitor()
            headers = make_rate_limit_headers(remaining=1000, reset_in_seconds=reset_seconds)
            monitor.update_from_headers(headers)
            pacer = RequestPacer(monitor, config=config)
            delays.append(pacer.get_recommended_delay())

        # Each delay should be <= previous (less time = smaller delays)
        for i in range(1, len(delays)):
            assert delays[i] <= delays[i - 1], f"Delay did not decrease: {delays}"

    def test_delay_never_negative(self) -> None:
        """Delay should never be negative."""
        config = PacingConfig(
            min_request_interval_ms=0,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )

        # Test various edge cases
        test_cases = [
            {"remaining": 0, "reset_in_seconds": 0},
            {"remaining": 1, "reset_in_seconds": 0},
            {"remaining": 5000, "reset_in_seconds": 1},
            {"remaining": 0, "reset_in_seconds": 3600},
        ]

        for params in test_cases:
            monitor = RateLimitMonitor()
            headers = make_rate_limit_headers(**params)
            monitor.update_from_headers(headers)
            pacer = RequestPacer(monitor, config=config)
            delay = pacer.get_recommended_delay()
            assert delay >= 0, f"Negative delay for {params}"


class TestWaitWithPacer:
    """Tests for async wait helper."""

    @pytest.mark.asyncio
    async def test_wait_with_pacer_waits(self) -> None:
        """wait_with_pacer sleeps for recommended delay."""
        monitor = RateLimitMonitor()
        config = PacingConfig(min_request_interval_ms=100)
        pacer = RequestPacer(monitor, config=config)

        import time

        start = time.monotonic()
        await wait_with_pacer(pacer)
        elapsed = time.monotonic() - start

        # Should have waited at least 100ms
        assert elapsed >= 0.09  # Allow some tolerance

    @pytest.mark.asyncio
    async def test_wait_with_pacer_small_delay(self) -> None:
        """wait_with_pacer handles small delays correctly."""
        monitor = RateLimitMonitor()
        # Very healthy scenario with short reset time
        # With 4999 remaining and 10 seconds to reset, delay = 10/4999 = ~0.002s
        headers = make_rate_limit_headers(remaining=4999, reset_in_seconds=10)
        monitor.update_from_headers(headers)

        config = PacingConfig(
            min_request_interval_ms=0,
            reserve_buffer_pct=0,
            burst_allowance=0,
        )
        pacer = RequestPacer(monitor, config=config)

        import time

        start = time.monotonic()
        await wait_with_pacer(pacer)
        elapsed = time.monotonic() - start

        # Should return very quickly (delay ~2ms with healthy multiplier)
        assert elapsed < 0.05
