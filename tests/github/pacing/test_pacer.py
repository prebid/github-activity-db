"""Unit tests for RequestPacer.

The pacer wraps an :class:`AsyncTokenBucket` (covered in detail in
``test_token_bucket.py``). These tests verify the integration:

* Config values map onto bucket parameters correctly.
* ``acquire()`` delegates to the bucket.
* ``on_request_complete(headers)`` updates both the monitor *and* the bucket.
* Forced-wait API delegates correctly.
* ``get_stats()`` exposes the combined view.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from github_activity_db.config import PacingConfig
from github_activity_db.github.pacing.pacer import RequestPacer
from github_activity_db.github.rate_limit.monitor import RateLimitMonitor
from tests.fixtures.rate_limit_responses import (
    HEADERS_HEALTHY,
    make_rate_limit_headers,
)


def make_pacer(
    *,
    burst_allowance: int = 10,
    min_interval_ms: int = 50,
    max_interval_ms: int = 60_000,
    reserve_buffer_pct: float = 10.0,
) -> RequestPacer:
    """Construct a pacer with controlled config for tests."""
    monitor = RateLimitMonitor()
    config = PacingConfig(
        burst_allowance=burst_allowance,
        min_request_interval_ms=min_interval_ms,
        max_request_interval_ms=max_interval_ms,
        reserve_buffer_pct=reserve_buffer_pct,
    )
    return RequestPacer(monitor, config=config)


class TestPacerInit:
    """Constructor maps PacingConfig to bucket parameters."""

    def test_init_with_monitor(self) -> None:
        monitor = RateLimitMonitor()
        pacer = RequestPacer(monitor)
        assert pacer.bucket is not None
        assert pacer.config is not None

    def test_capacity_from_burst_allowance(self) -> None:
        pacer = make_pacer(burst_allowance=25)
        assert pacer.bucket.capacity == 25

    def test_max_rate_from_min_interval(self) -> None:
        # 50ms min interval → 1000/50 = 20 req/s ceiling
        pacer = make_pacer(min_interval_ms=50)
        # Bucket's internal _max_rate isn't a public property, but updating
        # with a huge budget should produce a rate at the cap.
        headers = make_rate_limit_headers(remaining=999_999, reset_in_seconds=1)
        pacer.bucket.update_from_headers(headers)
        assert pacer.bucket.rate == pytest.approx(20.0, abs=0.1)

    def test_hard_floor_from_reserve_pct(self) -> None:
        """Floor is recomputed against the live limit on the first response."""
        pacer = make_pacer(reserve_buffer_pct=10.0)
        # Before any header: floor sits at the absolute min (50).
        assert pacer.bucket.hard_floor == 50
        # After observing a 5000-limit header: 10% x 5000 = 500.
        pacer.on_request_complete(HEADERS_HEALTHY)
        assert pacer.bucket.hard_floor == 500

    def test_hard_floor_scales_with_observed_limit(self) -> None:
        """A higher GitHub limit (e.g. GitHub App) scales the floor up."""
        pacer = make_pacer(reserve_buffer_pct=10.0)
        # Simulate a 15000/hr GitHub App limit
        from tests.fixtures.rate_limit_responses import make_rate_limit_headers

        pacer.on_request_complete(make_rate_limit_headers(limit=15000, remaining=5000))
        assert pacer.bucket.hard_floor == 1500

    def test_hard_floor_minimum_50(self) -> None:
        """Even with 0% reserve, floor never drops below the absolute min."""
        pacer = make_pacer(reserve_buffer_pct=0.0)
        pacer.on_request_complete(HEADERS_HEALTHY)
        assert pacer.bucket.hard_floor == 50


class TestAcquire:
    """``acquire()`` delegates to the bucket."""

    async def test_acquire_succeeds(self) -> None:
        """First acquire on fresh bucket completes (capacity > 0)."""
        pacer = make_pacer()

        start = time.monotonic()
        await pacer.acquire()
        elapsed = time.monotonic() - start

        assert elapsed < 0.05  # initial burst is free


class TestOnRequestComplete:
    """``on_request_complete`` updates both monitor and bucket."""

    def test_with_headers_updates_monitor(self) -> None:
        pacer = make_pacer()
        pacer.on_request_complete(HEADERS_HEALTHY)
        pool = pacer._monitor.get_pool_limit()
        assert pool is not None
        assert pool.remaining == 4500

    def test_with_headers_updates_bucket_rate(self) -> None:
        """Bucket rate adapts when headers arrive."""
        pacer = make_pacer(reserve_buffer_pct=10.0)  # hard_floor=500
        # 4500 remaining - 500 floor = 4000 budget over 3600s ≈ 1.111/s
        pacer.on_request_complete(HEADERS_HEALTHY)
        assert 1.0 < pacer.bucket.rate < 1.2

    def test_with_none_is_noop(self) -> None:
        pacer = make_pacer()
        original_rate = pacer.bucket.rate
        pacer.on_request_complete(None)
        assert pacer.bucket.rate == original_rate

    def test_hard_floor_engages_forced_wait(self) -> None:
        """remaining < hard_floor causes pacer.is_forced_wait_active = True."""
        pacer = make_pacer(reserve_buffer_pct=10.0)  # hard_floor=500
        headers = make_rate_limit_headers(remaining=100, reset_in_seconds=600)
        pacer.on_request_complete(headers)
        assert pacer.is_forced_wait_active is True


class TestForcedWait:
    """Forced-wait API delegates to the bucket."""

    def test_force_wait_seconds(self) -> None:
        pacer = make_pacer()
        pacer.force_wait(60.0)
        assert pacer.is_forced_wait_active is True
        assert 55 < pacer.forced_wait_remaining <= 60

    def test_force_wait_until(self) -> None:
        pacer = make_pacer()
        when = datetime.now(UTC) + timedelta(minutes=5)
        pacer.force_wait_until(when)
        assert pacer.is_forced_wait_active is True
        assert 290 < pacer.forced_wait_remaining < 305

    def test_clear_forced_wait(self) -> None:
        pacer = make_pacer()
        pacer.force_wait(60.0)
        pacer.clear_forced_wait()
        assert pacer.is_forced_wait_active is False


class TestGetStats:
    """Stats snapshot exposes both bucket and monitor state."""

    def test_stats_shape(self) -> None:
        pacer = make_pacer()
        pacer.on_request_complete(HEADERS_HEALTHY)
        stats = pacer.get_stats()
        assert "rate_per_second" in stats
        assert "tokens_available" in stats
        assert "capacity" in stats
        assert "hard_floor" in stats
        assert "is_forced_wait" in stats
        assert "status" in stats
        assert "remaining" in stats
        assert "seconds_until_reset" in stats

    def test_stats_reflect_state(self) -> None:
        pacer = make_pacer()
        pacer.on_request_complete(HEADERS_HEALTHY)
        stats = pacer.get_stats()
        assert stats["remaining"] == 4500
        assert stats["status"] == "healthy"
        assert stats["is_forced_wait"] is False
