"""Unit tests for AsyncTokenBucket.

The bucket is the admission gate that all GitHub API calls flow through.
Critical properties under test:

* Single-caller acquire blocks and unblocks correctly.
* Concurrent callers share the rate (no per-caller multiplication).
* Adaptive rate matches the GitHub-reported budget.
* Hard floor blocks until reset, preventing in-flight overshoot.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from github_activity_db.github.pacing.token_bucket import AsyncTokenBucket
from tests.fixtures.rate_limit_responses import (
    HEADERS_HEALTHY,
    make_rate_limit_headers,
)


class TestBucketInit:
    """Constructor validation and defaults."""

    def test_default_init(self) -> None:
        bucket = AsyncTokenBucket()
        assert bucket.capacity == 10.0
        assert bucket.rate >= 0.1
        # Initial hard floor is the absolute minimum; it bumps up after the
        # first header is observed (scaled to the actual GitHub limit).
        assert bucket.hard_floor == 50

    def test_init_validates_capacity(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            AsyncTokenBucket(capacity=0.5)

    def test_init_validates_min_rate(self) -> None:
        with pytest.raises(ValueError, match="min_rate"):
            AsyncTokenBucket(min_rate=0)

    def test_init_validates_min_hard_floor(self) -> None:
        with pytest.raises(ValueError, match="min_hard_floor"):
            AsyncTokenBucket(min_hard_floor=-1)

    def test_init_validates_hard_floor_pct(self) -> None:
        with pytest.raises(ValueError, match="hard_floor_pct"):
            AsyncTokenBucket(hard_floor_pct=200.0)

    def test_init_validates_max_rate(self) -> None:
        with pytest.raises(ValueError, match="max_rate"):
            AsyncTokenBucket(min_rate=5.0, max_rate=1.0)


class TestAcquireSingleCaller:
    """Single-caller behavior — burst, refill, block."""

    @pytest.mark.asyncio
    async def test_initial_burst_does_not_block(self) -> None:
        """Bucket starts full; first ``capacity`` acquires return immediately."""
        bucket = AsyncTokenBucket(capacity=5, initial_rate=0.01)

        start = time.monotonic()
        for _ in range(5):
            await bucket.acquire()
        elapsed = time.monotonic() - start

        # Initial 5 acquires should be near-free; allow a generous ceiling
        # for slow CI rather than asserting near-zero.
        assert elapsed < 0.5, f"Initial burst should be near-free, got {elapsed}s"

    @pytest.mark.asyncio
    async def test_acquire_blocks_when_empty(self) -> None:
        """After draining, next acquire waits for refill."""
        bucket = AsyncTokenBucket(capacity=2, initial_rate=10.0)

        # Drain the initial 2 tokens
        await bucket.acquire()
        await bucket.acquire()

        # Next acquire should wait ~0.1s for one token to refill
        start = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - start

        assert 0.05 < elapsed < 0.5, f"Expected ~0.1s wait, got {elapsed}s"

    @pytest.mark.asyncio
    async def test_acquire_capped_by_capacity(self) -> None:
        """Tokens cannot accumulate past capacity even after long idle."""
        bucket = AsyncTokenBucket(capacity=3, initial_rate=100.0)

        # Drain
        for _ in range(3):
            await bucket.acquire()

        # Wait long enough that 100/sec x 0.5s = 50 tokens would accumulate
        await asyncio.sleep(0.5)

        # Capacity caps at 3 — only 3 quick acquires before blocking
        start = time.monotonic()
        for _ in range(3):
            await bucket.acquire()
        elapsed = time.monotonic() - start

        assert elapsed < 0.05  # 3 should be instant
        # The 4th should block because we capped at 3 tokens
        start = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - start
        assert elapsed > 0.005  # had to wait for refill


class TestAcquireConcurrent:
    """Multi-caller behavior — the critical correctness test."""

    @pytest.mark.asyncio
    async def test_concurrent_acquires_share_rate(self) -> None:
        """N concurrent workers all acquiring don't multiply the rate.

        This is the property a per-call delay-based pacer fails: with N
        workers each independently waiting ``delay``, realized rate becomes
        ``N x intended``. The shared bucket must stay close to ``intended``.
        """
        # 20 tokens/sec total, regardless of concurrency. Cap of 5 so the
        # initial burst doesn't dominate the measurement.
        bucket = AsyncTokenBucket(capacity=5, initial_rate=20.0)

        n_workers = 10
        n_per_worker = 6  # 60 acquires total, well past initial cap
        completion_times: list[float] = []

        async def worker() -> None:
            for _ in range(n_per_worker):
                await bucket.acquire()
                completion_times.append(time.monotonic())

        start = time.monotonic()
        await asyncio.gather(*(worker() for _ in range(n_workers)))
        total_elapsed = time.monotonic() - start

        n_total = n_workers * n_per_worker  # 60
        # Expected: 5 burst free + 55 / 20 ≈ 2.75s. The lower bound proves the
        # bucket actually paced (would be ~0s if N workers x rate); the upper
        # bound is loose so a slow CI runner doesn't flake.
        assert total_elapsed > 2.0, (
            f"60 acquires at 20/s should take >= 2s; got {total_elapsed}s "
            "(bucket likely failed to pace)"
        )
        assert total_elapsed < 20.0, (
            f"60 acquires at 20/s should not take >20s; got {total_elapsed}s"
        )
        assert len(completion_times) == n_total

    @pytest.mark.asyncio
    async def test_concurrent_acquires_serialize(self) -> None:
        """Ensure concurrent acquires don't double-issue tokens."""
        bucket = AsyncTokenBucket(capacity=3, initial_rate=5.0)

        # Spawn 10 concurrent acquires; only 3 should return immediately
        completed: list[float] = []

        async def acquire_and_record() -> None:
            await bucket.acquire()
            completed.append(time.monotonic())

        start = time.monotonic()
        tasks = [asyncio.create_task(acquire_and_record()) for _ in range(10)]
        await asyncio.gather(*tasks)

        # First 3 should complete immediately, then 1 every 0.2s
        # Total: ~7 x 0.2s = 1.4s after the initial burst
        elapsed = time.monotonic() - start
        assert elapsed > 1.0, f"Expected serialized acquires (>= 1s); got {elapsed}s"
        assert elapsed < 10.0, f"Should not take >10s; got {elapsed}s"
        assert len(completed) == 10


class TestUpdateFromHeaders:
    """Adaptive rate adjustment from GitHub response headers."""

    def test_update_sets_rate_from_remaining(self) -> None:
        """Rate = (remaining - hard_floor) / seconds_until_reset."""
        # hard_floor_pct=1.0 with limit=5000 → floor=50
        bucket = AsyncTokenBucket(min_rate=0.001, hard_floor_pct=1.0)
        # 1050 remaining, 1000s to reset → spendable budget 1000, rate=1.0
        headers = make_rate_limit_headers(remaining=1050, reset_in_seconds=1000)
        bucket.update_from_headers(headers)
        assert 0.95 < bucket.rate < 1.05

    def test_update_floors_at_min_rate(self) -> None:
        """Even with tiny budget, rate doesn't go below min_rate."""
        bucket = AsyncTokenBucket(min_rate=0.5, hard_floor_pct=1.0)
        # Budget = 51 - 50 = 1, over 1000s = 0.001/s, but min is 0.5
        headers = make_rate_limit_headers(remaining=51, reset_in_seconds=1000)
        bucket.update_from_headers(headers)
        assert bucket.rate == 0.5

    def test_update_caps_at_max_rate(self) -> None:
        """Rate is capped at max_rate even when budget is huge."""
        bucket = AsyncTokenBucket(max_rate=5.0, hard_floor_pct=1.0)
        # Budget = 5050 - 50 = 5000, over 100s = 50/s, but max is 5
        headers = make_rate_limit_headers(remaining=5050, reset_in_seconds=100)
        bucket.update_from_headers(headers)
        assert bucket.rate == 5.0

    def test_hard_floor_engages_forced_wait(self) -> None:
        """Remaining at/below hard_floor blocks acquires until reset."""
        # 2% of 5000 = 100
        bucket = AsyncTokenBucket(hard_floor_pct=2.0)
        headers = make_rate_limit_headers(remaining=99, reset_in_seconds=600)
        bucket.update_from_headers(headers)
        assert bucket.is_forced_wait_active is True
        assert 590 < bucket.forced_wait_remaining < 605

    def test_hard_floor_at_exact_threshold(self) -> None:
        """remaining == hard_floor also engages."""
        # 1% of 5000 = 50
        bucket = AsyncTokenBucket(hard_floor_pct=1.0)
        headers = make_rate_limit_headers(remaining=50, reset_in_seconds=300)
        bucket.update_from_headers(headers)
        assert bucket.is_forced_wait_active is True

    def test_hard_floor_scales_with_reported_limit(self) -> None:
        """Floor adapts to the actual GitHub-reported limit (PAT vs App)."""
        # 10% of 15000 (e.g. GitHub App limit) = 1500
        bucket = AsyncTokenBucket(hard_floor_pct=10.0)
        bucket.update_from_headers(make_rate_limit_headers(limit=15000, remaining=2000))
        assert bucket.hard_floor == 1500
        # Now if we observe a smaller limit, it scales down
        bucket.update_from_headers(make_rate_limit_headers(limit=5000, remaining=2000))
        assert bucket.hard_floor == 500

    def test_update_ignores_malformed_headers(self) -> None:
        """Malformed values don't crash; rate stays put."""
        bucket = AsyncTokenBucket(initial_rate=2.0)
        original = bucket.rate
        bucket.update_from_headers({"x-ratelimit-remaining": "not_a_number"})
        bucket.update_from_headers({})
        bucket.update_from_headers({"x-ratelimit-remaining": "-1"})
        assert bucket.rate == original

    def test_update_with_healthy_fixture(self) -> None:
        """Realistic HEALTHY headers produce a sane rate."""
        bucket = AsyncTokenBucket(min_rate=0.001, hard_floor_pct=1.0)
        bucket.update_from_headers(HEADERS_HEALTHY)
        # 4500 - 50 = 4450 over 3600s ≈ 1.236/s
        assert 1.2 < bucket.rate < 1.3


class TestForcedWait:
    """Manual force-wait paths for observed rate-limit errors."""

    def test_force_wait_seconds(self) -> None:
        bucket = AsyncTokenBucket()
        bucket.force_wait(60.0)
        assert bucket.is_forced_wait_active is True
        assert 55 < bucket.forced_wait_remaining <= 60

    def test_force_wait_until(self) -> None:
        bucket = AsyncTokenBucket()
        when = datetime.now(UTC) + timedelta(minutes=5)
        bucket.force_wait_until(when)
        assert bucket.is_forced_wait_active is True
        assert 290 < bucket.forced_wait_remaining < 305

    def test_force_wait_naive_datetime_treated_as_utc(self) -> None:
        bucket = AsyncTokenBucket()
        when = (datetime.now(UTC) + timedelta(minutes=2)).replace(tzinfo=None)
        bucket.force_wait_until(when)
        assert bucket.is_forced_wait_active is True

    def test_force_wait_extends_but_does_not_shrink(self) -> None:
        """Multiple force_wait calls keep the latest deadline."""
        bucket = AsyncTokenBucket()
        bucket.force_wait(120.0)
        bucket.force_wait(30.0)  # earlier deadline — should be ignored
        assert bucket.forced_wait_remaining > 100

    def test_clear_forced_wait(self) -> None:
        bucket = AsyncTokenBucket()
        bucket.force_wait(60.0)
        bucket.clear_forced_wait()
        assert bucket.is_forced_wait_active is False

    def test_expired_forced_wait_clears_itself(self) -> None:
        bucket = AsyncTokenBucket()
        bucket._wait_until = datetime.now(UTC) - timedelta(seconds=1)
        assert bucket.is_forced_wait_active is False
        assert bucket.forced_wait_remaining == 0.0

    @pytest.mark.asyncio
    async def test_acquire_respects_forced_wait(self) -> None:
        """While forced wait is active, acquire blocks for that duration."""
        bucket = AsyncTokenBucket(capacity=10, initial_rate=100.0)
        bucket.force_wait(0.3)

        start = time.monotonic()
        await bucket.acquire()
        elapsed = time.monotonic() - start

        assert 0.25 < elapsed < 0.6, f"Expected ~0.3s wait, got {elapsed}s"


class TestStats:
    """Observability snapshot."""

    def test_stats_shape(self) -> None:
        bucket = AsyncTokenBucket()
        stats = bucket.get_stats()
        assert "rate_per_second" in stats
        assert "capacity" in stats
        assert "tokens_available" in stats
        assert "hard_floor" in stats
        assert "is_forced_wait" in stats
        assert "forced_wait_remaining_seconds" in stats

    def test_stats_reflect_state(self) -> None:
        bucket = AsyncTokenBucket(capacity=8, initial_rate=2.5, min_hard_floor=25)
        stats = bucket.get_stats()
        assert stats["capacity"] == 8
        assert stats["rate_per_second"] == 2.5
        # Before any header observation, the floor sits at min_hard_floor.
        assert stats["hard_floor"] == 25
        assert stats["is_forced_wait"] is False
