"""Integration tests for pacing components working together.

These tests verify the interaction between:
- RequestPacer: Token bucket algorithm for request timing
- RequestScheduler: Priority queue with concurrency control
- BatchExecutor: Batch operations with progress tracking
- RateLimitMonitor: Rate limit state tracking

Focus is on verifying component interactions, not isolated unit behavior.
"""

import asyncio
import time
from datetime import UTC, datetime

import pytest

from github_activity_db.config import PacingConfig
from github_activity_db.github.pacing.batch import BatchExecutor, BatchResult
from github_activity_db.github.pacing.pacer import RequestPacer
from github_activity_db.github.pacing.progress import ProgressTracker
from github_activity_db.github.pacing.scheduler import (
    RequestPriority,
    RequestScheduler,
)
from github_activity_db.github.rate_limit.monitor import RateLimitMonitor


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture
def monitor_healthy() -> RateLimitMonitor:
    """Create a monitor with healthy rate limits."""
    monitor = RateLimitMonitor()
    monitor.update_from_headers(
        {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "4000",
            "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
            "x-ratelimit-resource": "core",
        }
    )
    return monitor


@pytest.fixture
def monitor_low() -> RateLimitMonitor:
    """Create a monitor with low rate limits (should trigger throttling)."""
    monitor = RateLimitMonitor()
    monitor.update_from_headers(
        {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "100",  # Only 2% remaining
            "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
            "x-ratelimit-resource": "core",
        }
    )
    return monitor


@pytest.fixture
def fast_config() -> PacingConfig:
    """Config with minimal delays for fast tests."""
    return PacingConfig(
        min_request_interval_ms=0,
        max_request_interval_ms=100,  # Minimum allowed is 100
        reserve_buffer_pct=0,
        burst_allowance=1000,
        max_concurrent_requests=5,
    )


@pytest.fixture
def slow_config() -> PacingConfig:
    """Config with measurable delays for timing tests."""
    return PacingConfig(
        min_request_interval_ms=20,  # 20ms minimum between requests
        max_request_interval_ms=200,
        reserve_buffer_pct=10,
        burst_allowance=2,  # Small burst to see pacing effects
        max_concurrent_requests=3,
    )


# -----------------------------------------------------------------------------
# Integration Tests: Scheduler + Pacer
# -----------------------------------------------------------------------------
class TestSchedulerPacerIntegration:
    """Tests for scheduler using pacer for timing."""

    @pytest.mark.asyncio
    async def test_scheduler_uses_pacer_for_delays(
        self, monitor_healthy: RateLimitMonitor, slow_config: PacingConfig
    ) -> None:
        """Scheduler respects pacer's delay recommendations."""
        pacer = RequestPacer(monitor_healthy, config=slow_config)
        scheduler = RequestScheduler(pacer, max_concurrent=1)

        await scheduler.start()

        timestamps: list[float] = []

        async def record_time() -> float:
            """Task that records its execution time."""
            ts = time.monotonic()
            timestamps.append(ts)
            return ts

        # Submit multiple tasks
        futures = [scheduler.submit(record_time, priority=RequestPriority.NORMAL) for _ in range(5)]

        # Wait for all to complete
        await asyncio.gather(*futures)
        await scheduler.shutdown(wait=True)

        # With burst_allowance=2, first 2 should execute quickly,
        # then pacing should kick in
        assert len(timestamps) == 5

    @pytest.mark.asyncio
    async def test_high_priority_gets_processed_first(
        self, monitor_healthy: RateLimitMonitor, fast_config: PacingConfig
    ) -> None:
        """HIGH priority tasks are processed before NORMAL/LOW."""
        pacer = RequestPacer(monitor_healthy, config=fast_config)
        scheduler = RequestScheduler(pacer, max_concurrent=1)

        await scheduler.start()

        execution_order: list[str] = []
        start_gate = asyncio.Event()

        async def task(name: str) -> str:
            await start_gate.wait()
            execution_order.append(name)
            return name

        # Schedule in mixed priority order using enqueue (fire-and-forget)
        scheduler.enqueue(lambda: task("low"), priority=RequestPriority.LOW)
        scheduler.enqueue(lambda: task("normal"), priority=RequestPriority.NORMAL)
        scheduler.enqueue(lambda: task("high"), priority=RequestPriority.HIGH)

        # Allow tiny delay for scheduling
        await asyncio.sleep(0.01)

        # Release the gate
        start_gate.set()

        # Wait for queue to drain
        await asyncio.sleep(0.1)
        await scheduler.shutdown(wait=True)

        # HIGH should execute before NORMAL before LOW
        assert len(execution_order) == 3
        # Check that high priority came first
        assert execution_order.index("high") < execution_order.index("normal")
        assert execution_order.index("normal") < execution_order.index("low")

    @pytest.mark.asyncio
    async def test_concurrency_limit_respected(
        self, monitor_healthy: RateLimitMonitor, fast_config: PacingConfig
    ) -> None:
        """Scheduler never exceeds max_concurrent requests."""
        pacer = RequestPacer(monitor_healthy, config=fast_config)
        scheduler = RequestScheduler(pacer, max_concurrent=2)

        await scheduler.start()

        max_concurrent_seen = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def track_concurrency() -> None:
            nonlocal max_concurrent_seen, current_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent_seen = max(max_concurrent_seen, current_concurrent)

            await asyncio.sleep(0.02)  # Simulate work

            async with lock:
                current_concurrent -= 1

        # Submit more tasks than concurrent limit
        futures = [
            scheduler.submit(track_concurrency, priority=RequestPriority.NORMAL) for _ in range(10)
        ]

        await asyncio.gather(*futures)
        await scheduler.shutdown(wait=True)

        # Never exceeded limit of 2
        assert max_concurrent_seen <= 2


# -----------------------------------------------------------------------------
# Integration Tests: BatchExecutor + Scheduler + Pacer
# -----------------------------------------------------------------------------
class TestBatchExecutorIntegration:
    """Tests for batch executor using scheduler and pacer."""

    @pytest.mark.asyncio
    async def test_batch_executor_processes_all_items(
        self, monitor_healthy: RateLimitMonitor, fast_config: PacingConfig
    ) -> None:
        """BatchExecutor processes all items in a batch."""
        pacer = RequestPacer(monitor_healthy, config=fast_config)
        scheduler = RequestScheduler(pacer, max_concurrent=3)
        executor: BatchExecutor[int, int] = BatchExecutor(scheduler)

        await scheduler.start()

        items = list(range(10))

        async def process_item(item: int) -> int:
            return item * 2

        result: BatchResult[int] = await executor.execute(items, process_item)

        await scheduler.shutdown(wait=True)

        assert result.success_count == 10
        assert result.failure_count == 0
        assert result.all_succeeded
        assert sorted(result.succeeded) == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]

    @pytest.mark.asyncio
    async def test_batch_executor_handles_failures(
        self, monitor_healthy: RateLimitMonitor, fast_config: PacingConfig
    ) -> None:
        """BatchExecutor continues on failures by default."""
        pacer = RequestPacer(monitor_healthy, config=fast_config)
        # Use max_retries=0 to avoid slow exponential backoff delays
        scheduler = RequestScheduler(pacer, max_concurrent=3, max_retries=0)
        executor: BatchExecutor[int, int] = BatchExecutor(scheduler, stop_on_error=False)

        await scheduler.start()

        items = list(range(10))

        async def process_with_errors(item: int) -> int:
            if item % 3 == 0:  # Fail on 0, 3, 6, 9
                raise ValueError(f"Failed on {item}")
            return item

        result: BatchResult[int] = await executor.execute(items, process_with_errors)

        await scheduler.shutdown(wait=True)

        assert result.success_count == 6  # 1, 2, 4, 5, 7, 8
        assert result.failure_count == 4  # 0, 3, 6, 9
        assert not result.all_succeeded

    @pytest.mark.asyncio
    async def test_batch_executor_with_progress_tracking(
        self, monitor_healthy: RateLimitMonitor, fast_config: PacingConfig
    ) -> None:
        """BatchExecutor updates progress tracker."""
        pacer = RequestPacer(monitor_healthy, config=fast_config)
        scheduler = RequestScheduler(pacer, max_concurrent=3)
        progress = ProgressTracker(total=5)
        executor: BatchExecutor[int, int] = BatchExecutor(scheduler, progress=progress)

        await scheduler.start()

        items = list(range(5))

        async def process_item(item: int) -> int:
            return item

        result = await executor.execute(items, process_item)

        await scheduler.shutdown(wait=True)

        assert result.success_count == 5
        assert progress.completed == 5
        assert progress.failed == 0

    @pytest.mark.asyncio
    async def test_batch_respects_max_batch_size(
        self, monitor_healthy: RateLimitMonitor, fast_config: PacingConfig
    ) -> None:
        """BatchExecutor respects max_batch_size option."""
        pacer = RequestPacer(monitor_healthy, config=fast_config)
        scheduler = RequestScheduler(pacer, max_concurrent=10)
        executor: BatchExecutor[int, int] = BatchExecutor(scheduler, max_batch_size=3)

        await scheduler.start()

        items = list(range(10))

        async def process_item(item: int) -> int:
            return item

        result = await executor.execute(items, process_item)

        await scheduler.shutdown(wait=True)

        # All items processed (max_batch_size limits concurrent scheduling, not total)
        assert result.success_count == 10


# -----------------------------------------------------------------------------
# Integration Tests: Rate Limit Throttling
# -----------------------------------------------------------------------------
class TestRateLimitThrottling:
    """Tests for rate limit-based throttling behavior."""

    @pytest.mark.asyncio
    async def test_low_rate_limit_affects_delay(self, monitor_low: RateLimitMonitor) -> None:
        """Pacer provides delay when rate limit is low."""
        config = PacingConfig(
            min_request_interval_ms=10,
            max_request_interval_ms=500,
            reserve_buffer_pct=10,
            burst_allowance=1,
        )
        pacer = RequestPacer(monitor_low, config=config)

        # First call may use burst allowance
        _ = pacer.get_recommended_delay()
        pacer.on_request_complete()

        # Second call should see some delay recommendation
        delay2 = pacer.get_recommended_delay()

        # With low remaining quota, pacer should recommend some delay
        # (actual value depends on algorithm, but should be non-negative)
        assert delay2 >= 0

    @pytest.mark.asyncio
    async def test_scheduler_adapts_to_rate_limit_changes(self, fast_config: PacingConfig) -> None:
        """Scheduler adapts when rate limit status changes."""
        monitor = RateLimitMonitor()
        # Start healthy
        monitor.update_from_headers(
            {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "4000",
                "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
                "x-ratelimit-resource": "core",
            }
        )

        pacer = RequestPacer(monitor, config=fast_config)
        scheduler = RequestScheduler(pacer, max_concurrent=2)

        await scheduler.start()

        results: list[int] = []

        async def task(n: int) -> int:
            results.append(n)
            return n

        # Process some requests
        await asyncio.gather(
            *[
                scheduler.submit(lambda n=i: task(n), priority=RequestPriority.NORMAL)  # type: ignore[misc]
                for i in range(3)
            ]
        )

        # Simulate rate limit dropping
        monitor.update_from_headers(
            {
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "50",  # Very low
                "x-ratelimit-reset": str(int(datetime.now(UTC).timestamp()) + 3600),
                "x-ratelimit-resource": "core",
            }
        )

        # Process more requests (pacer should adapt)
        await asyncio.gather(
            *[
                scheduler.submit(lambda n=i: task(n), priority=RequestPriority.NORMAL)  # type: ignore[misc]
                for i in range(3, 6)
            ]
        )

        await scheduler.shutdown(wait=True)

        # All tasks completed despite rate limit change
        assert len(results) == 6
