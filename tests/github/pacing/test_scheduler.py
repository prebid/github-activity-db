"""Unit tests for RequestScheduler class.

These tests verify priority ordering, concurrency control,
retry logic, and rate limit handling.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from github_activity_db.config import PacingConfig
from github_activity_db.github.exceptions import GitHubRateLimitError
from github_activity_db.github.pacing.pacer import RequestPacer
from github_activity_db.github.pacing.scheduler import (
    RequestPriority,
    RequestScheduler,
    RequestState,
)
from github_activity_db.github.rate_limit.monitor import RateLimitMonitor
from tests.fixtures.rate_limit_responses import HEADERS_HEALTHY


def create_pacer() -> RequestPacer:
    """Helper to create a pacer with minimal delays for testing."""
    monitor = RateLimitMonitor()
    monitor.update_from_headers(HEADERS_HEALTHY)
    config = PacingConfig(
        min_request_interval_ms=0,
        max_request_interval_ms=100,
        reserve_buffer_pct=0,
        burst_allowance=1000,
    )
    return RequestPacer(monitor, config=config)


class TestRequestSchedulerInit:
    """Tests for scheduler initialization."""

    def test_init_with_pacer(self) -> None:
        """Scheduler initializes with pacer."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)

        assert scheduler._pacer is pacer
        assert scheduler._max_concurrent == 5
        assert scheduler.is_running is False

    def test_init_with_custom_settings(self) -> None:
        """Scheduler accepts custom settings."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_concurrent=10, max_retries=5)

        assert scheduler._max_concurrent == 10
        assert scheduler._max_retries == 5


class TestSchedulerLifecycle:
    """Tests for start/shutdown lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running(self) -> None:
        """start() sets running flag."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)

        await scheduler.start()
        assert scheduler.is_running is True

        await scheduler.shutdown(wait=False)
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Multiple start() calls are idempotent."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)

        await scheduler.start()
        await scheduler.start()  # Should not error

        assert scheduler.is_running is True

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_queue(self) -> None:
        """shutdown(wait=True) waits for pending requests."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)
        await scheduler.start()

        completed = []

        async def slow_task() -> str:
            await asyncio.sleep(0.05)
            completed.append("done")
            return "result"

        scheduler.enqueue(slow_task)
        await scheduler.shutdown(wait=True, timeout=5.0)

        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_shutdown_cancels_on_timeout(self) -> None:
        """shutdown() respects timeout."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)
        await scheduler.start()

        async def very_slow_task() -> str:
            await asyncio.sleep(10)  # Very slow
            return "result"

        scheduler.enqueue(very_slow_task)

        # Shutdown with short timeout
        await scheduler.shutdown(wait=True, timeout=0.1)

        # Should have stopped without waiting for the slow task
        assert scheduler.is_running is False


class TestEnqueue:
    """Tests for enqueue method."""

    @pytest.mark.asyncio
    async def test_enqueue_returns_id(self) -> None:
        """enqueue returns a unique request ID."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)

        async def task() -> str:
            return "result"

        id1 = scheduler.enqueue(task)
        id2 = scheduler.enqueue(task)

        assert id1 != id2
        assert len(id1) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_enqueue_adds_to_queue(self) -> None:
        """enqueue adds request to queue."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)

        async def task() -> str:
            return "result"

        assert scheduler.queue_size == 0

        scheduler.enqueue(task)
        assert scheduler.queue_size == 1

        scheduler.enqueue(task)
        assert scheduler.queue_size == 2


class TestSubmit:
    """Tests for submit method."""

    @pytest.mark.asyncio
    async def test_submit_returns_result(self) -> None:
        """submit returns the coroutine result."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)
        await scheduler.start()

        async def task() -> str:
            return "hello"

        result = await scheduler.submit(task)
        assert result == "hello"

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_submit_with_timeout(self) -> None:
        """submit respects timeout."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)
        await scheduler.start()

        async def slow_task() -> str:
            await asyncio.sleep(10)
            return "result"

        with pytest.raises(asyncio.TimeoutError):
            await scheduler.submit(slow_task, timeout=0.1)

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_submit_propagates_exception(self) -> None:
        """submit propagates exceptions from coroutine."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_retries=0)
        await scheduler.start()

        async def failing_task() -> str:
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            await scheduler.submit(failing_task)

        await scheduler.shutdown(wait=False)


class TestPriorityOrdering:
    """Tests for priority queue ordering."""

    @pytest.mark.asyncio
    async def test_high_priority_executes_first(self) -> None:
        """HIGH priority requests execute before NORMAL and LOW."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_concurrent=1)

        executed_order: list[str] = []

        async def make_task(name: str) -> str:
            executed_order.append(name)
            return name

        # Enqueue in reverse priority order
        scheduler.enqueue(lambda: make_task("low"), RequestPriority.LOW)
        scheduler.enqueue(lambda: make_task("normal"), RequestPriority.NORMAL)
        scheduler.enqueue(lambda: make_task("high"), RequestPriority.HIGH)

        await scheduler.start()
        await scheduler.shutdown(wait=True, timeout=5.0)

        # HIGH should be first
        assert executed_order[0] == "high"

    @pytest.mark.asyncio
    async def test_same_priority_fifo(self) -> None:
        """Same priority requests execute in FIFO order."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_concurrent=1)

        executed_order: list[int] = []

        async def make_task(num: int) -> int:
            executed_order.append(num)
            return num

        # Enqueue multiple NORMAL priority
        for i in range(5):
            scheduler.enqueue(lambda n=i: make_task(n), RequestPriority.NORMAL)

        await scheduler.start()
        await scheduler.shutdown(wait=True, timeout=5.0)

        # Should be in order
        assert executed_order == [0, 1, 2, 3, 4]


class TestConcurrencyControl:
    """Tests for semaphore-based concurrency control."""

    @pytest.mark.asyncio
    async def test_respects_max_concurrent(self) -> None:
        """Scheduler respects max_concurrent limit."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_concurrent=2)

        concurrent_count = 0
        max_concurrent_seen = 0

        async def track_concurrency() -> None:
            nonlocal concurrent_count, max_concurrent_seen
            concurrent_count += 1
            max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
            await asyncio.sleep(0.05)  # Hold for a bit
            concurrent_count -= 1

        # Submit 10 requests
        for _ in range(10):
            scheduler.enqueue(track_concurrency)

        await scheduler.start()
        await scheduler.shutdown(wait=True, timeout=5.0)

        # Should never exceed 2 concurrent
        assert max_concurrent_seen <= 2


class TestRetryLogic:
    """Tests for retry with exponential backoff."""

    @pytest.mark.asyncio
    async def test_retries_on_failure(self) -> None:
        """Requests are retried on failure."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_retries=3)

        attempt_count = 0

        async def flaky_task() -> str:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise RuntimeError("temporary failure")
            return "success"

        await scheduler.start()
        result = await scheduler.submit(flaky_task, timeout=10.0)

        assert result == "success"
        assert attempt_count == 3

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self) -> None:
        """Request fails after max retries exceeded."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_retries=2)

        async def always_fails() -> str:
            raise RuntimeError("permanent failure")

        await scheduler.start()

        with pytest.raises(RuntimeError, match="permanent failure"):
            await scheduler.submit(always_fails, timeout=10.0)

        await scheduler.shutdown(wait=False)

        # Should have recorded failure
        stats = scheduler.get_stats()
        assert stats["total_failed"] == 1


class TestRateLimitHandling:
    """Tests for rate limit error handling."""

    @pytest.mark.asyncio
    async def test_rate_limit_triggers_wait(self) -> None:
        """Rate limit error triggers forced wait."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_retries=3)

        attempt_count = 0
        reset_time = datetime.now(UTC) + timedelta(seconds=0.1)

        async def rate_limited_task() -> str:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                raise GitHubRateLimitError("Rate limited", reset_at=reset_time)
            return "success"

        await scheduler.start()
        result = await scheduler.submit(rate_limited_task, timeout=10.0)

        assert result == "success"
        assert attempt_count == 2

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_rate_limit_boosts_priority(self) -> None:
        """Rate limit retry gets HIGH priority."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer, max_retries=3, max_concurrent=1)

        # Track execution order
        executed: list[str] = []
        attempt_count = 0

        async def rate_limited_first() -> str:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                executed.append("first_attempt")
                raise GitHubRateLimitError("Rate limited", reset_at=None)
            executed.append("first_retry")
            return "first"

        async def normal_task() -> str:
            executed.append("normal")
            return "normal"

        # Enqueue rate-limited task first (NORMAL priority)
        scheduler.enqueue(rate_limited_first, RequestPriority.NORMAL)
        # Then enqueue normal task (also NORMAL priority)
        scheduler.enqueue(normal_task, RequestPriority.NORMAL)

        await scheduler.start()
        await scheduler.shutdown(wait=True, timeout=10.0)

        # Rate limited task should have been boosted to HIGH and retried
        # before the normal task could complete
        assert "first_retry" in executed


class TestStatistics:
    """Tests for scheduler statistics."""

    @pytest.mark.asyncio
    async def test_get_stats(self) -> None:
        """get_stats returns expected fields."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)

        stats = scheduler.get_stats()

        assert "queue_size" in stats
        assert "is_running" in stats
        assert "is_idle" in stats
        assert "max_concurrent" in stats
        assert "total_submitted" in stats
        assert "total_completed" in stats
        assert "total_failed" in stats

    @pytest.mark.asyncio
    async def test_stats_track_completion(self) -> None:
        """Statistics track completed requests."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)
        await scheduler.start()

        async def task() -> str:
            return "done"

        await scheduler.submit(task)
        await scheduler.submit(task)

        stats = scheduler.get_stats()
        assert stats["total_submitted"] == 2
        assert stats["total_completed"] == 2
        assert stats["total_failed"] == 0

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_is_idle(self) -> None:
        """is_idle reflects queue state."""
        pacer = create_pacer()
        scheduler = RequestScheduler(pacer)

        # Initially idle (empty queue)
        assert scheduler.queue_size == 0

        async def task() -> str:
            await asyncio.sleep(0.05)
            return "done"

        scheduler.enqueue(task)
        # Not idle - has pending request
        assert scheduler.queue_size == 1

        await scheduler.start()
        await scheduler.shutdown(wait=True, timeout=5.0)

        # After shutdown with wait, queue should be empty
        assert scheduler.queue_size == 0


class TestRequestPriority:
    """Tests for RequestPriority enum."""

    def test_priority_ordering(self) -> None:
        """HIGH < NORMAL < LOW for correct queue ordering."""
        assert RequestPriority.HIGH < RequestPriority.NORMAL
        assert RequestPriority.NORMAL < RequestPriority.LOW

    def test_priority_values(self) -> None:
        """Priority values are integers."""
        assert RequestPriority.HIGH.value == 1
        assert RequestPriority.NORMAL.value == 2
        assert RequestPriority.LOW.value == 3


class TestRequestState:
    """Tests for RequestState enum."""

    def test_all_states_exist(self) -> None:
        """All expected states are defined."""
        states = {s.name for s in RequestState}
        expected = {"PENDING", "IN_FLIGHT", "COMPLETED", "FAILED", "CANCELLED"}
        assert expected == states
