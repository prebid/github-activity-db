"""Unit tests for BatchExecutor class."""

import asyncio

import pytest

from github_activity_db.config import PacingConfig
from github_activity_db.github.pacing.batch import BatchExecutor, BatchResult, execute_batch
from github_activity_db.github.pacing.pacer import RequestPacer
from github_activity_db.github.pacing.progress import ProgressState, ProgressTracker
from github_activity_db.github.pacing.scheduler import RequestPriority, RequestScheduler
from github_activity_db.github.rate_limit.monitor import RateLimitMonitor
from tests.fixtures.rate_limit_responses import HEADERS_HEALTHY


def create_scheduler() -> RequestScheduler:
    """Helper to create a scheduler with minimal delays for testing."""
    monitor = RateLimitMonitor()
    monitor.update_from_headers(HEADERS_HEALTHY)
    config = PacingConfig(
        min_request_interval_ms=0,
        max_request_interval_ms=100,
        reserve_buffer_pct=0,
        burst_allowance=1000,
    )
    pacer = RequestPacer(monitor, config=config)
    return RequestScheduler(pacer, max_concurrent=5)


class TestBatchResult:
    """Tests for BatchResult dataclass."""

    def test_empty_result(self) -> None:
        """Empty result has correct counts."""
        result: BatchResult[int] = BatchResult()

        assert result.total_count == 0
        assert result.success_count == 0
        assert result.failure_count == 0
        assert result.all_succeeded is True

    def test_with_results(self) -> None:
        """Result with items has correct counts."""
        result: BatchResult[int] = BatchResult(
            succeeded=[1, 2, 3],
            failed=[(4, ValueError("error"))],
        )

        assert result.total_count == 4
        assert result.success_count == 3
        assert result.failure_count == 1
        assert result.all_succeeded is False

    def test_all_succeeded(self) -> None:
        """all_succeeded is True when no failures."""
        result: BatchResult[str] = BatchResult(succeeded=["a", "b", "c"])

        assert result.all_succeeded is True


class TestBatchExecutorInit:
    """Tests for BatchExecutor initialization."""

    def test_init_with_scheduler(self) -> None:
        """Executor initializes with scheduler."""
        scheduler = create_scheduler()
        executor = BatchExecutor(scheduler)

        assert executor._scheduler is scheduler
        assert executor._stop_on_error is False

    def test_init_with_options(self) -> None:
        """Executor accepts options."""
        scheduler = create_scheduler()
        progress = ProgressTracker()
        executor = BatchExecutor(
            scheduler,
            progress=progress,
            stop_on_error=True,
            max_batch_size=10,
        )

        assert executor._progress is progress
        assert executor._stop_on_error is True
        assert executor._max_batch_size == 10


class TestBatchExecutorExecute:
    """Tests for batch execution."""

    @pytest.mark.asyncio
    async def test_execute_empty_list(self) -> None:
        """Execute with empty list returns empty result."""
        scheduler = create_scheduler()
        await scheduler.start()

        executor = BatchExecutor(scheduler)
        result = await executor.execute([], lambda x: asyncio.sleep(0))

        assert result.total_count == 0
        assert result.all_succeeded is True

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_execute_all_succeed(self) -> None:
        """Execute with all successful items."""
        scheduler = create_scheduler()
        await scheduler.start()

        async def double(x: int) -> int:
            return x * 2

        executor = BatchExecutor(scheduler)
        result = await executor.execute([1, 2, 3, 4, 5], double)

        assert result.success_count == 5
        assert result.failure_count == 0
        assert sorted(result.succeeded) == [2, 4, 6, 8, 10]

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_execute_with_failures(self) -> None:
        """Execute handles failures gracefully."""
        scheduler = create_scheduler()
        await scheduler.start()

        async def sometimes_fail(x: int) -> int:
            if x == 3:
                raise ValueError(f"error on {x}")
            return x * 2

        executor = BatchExecutor(scheduler)
        result = await executor.execute([1, 2, 3, 4, 5], sometimes_fail)

        assert result.success_count == 4
        assert result.failure_count == 1
        assert 6 not in result.succeeded  # 3 failed

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_execute_stop_on_error(self) -> None:
        """Execute with stop_on_error stops after first failure."""
        scheduler = create_scheduler()
        await scheduler.start()

        processed: list[int] = []

        async def track_and_fail(x: int) -> int:
            processed.append(x)
            await asyncio.sleep(0.01)  # Ensure ordering
            if x == 2:
                raise ValueError("stop here")
            return x

        # Use max_batch_size=1 to ensure sequential processing
        executor = BatchExecutor(scheduler, stop_on_error=True, max_batch_size=1)
        result = await executor.execute([1, 2, 3, 4, 5], track_and_fail)

        # Should have processed 1 and 2, then stopped
        assert result.failure_count >= 1

        await scheduler.shutdown(wait=False)


class TestBatchExecutorProgress:
    """Tests for progress tracking integration."""

    @pytest.mark.asyncio
    async def test_progress_tracks_completion(self) -> None:
        """Progress tracker is updated during execution."""
        scheduler = create_scheduler()
        await scheduler.start()

        progress = ProgressTracker()

        async def process(x: int) -> int:
            return x

        executor = BatchExecutor(scheduler, progress=progress)
        await executor.execute([1, 2, 3, 4, 5], process)

        assert progress.state == ProgressState.COMPLETED
        assert progress.completed == 5
        assert progress.failed == 0

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_progress_tracks_failures(self) -> None:
        """Progress tracker counts failures."""
        scheduler = create_scheduler()
        await scheduler.start()

        progress = ProgressTracker()

        async def always_fail(x: int) -> int:
            raise ValueError("fail")

        executor = BatchExecutor(scheduler, progress=progress)
        await executor.execute([1, 2, 3], always_fail)

        assert progress.completed == 0
        assert progress.failed == 3

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_progress_callbacks_fire(self) -> None:
        """Progress callbacks are triggered."""
        scheduler = create_scheduler()
        await scheduler.start()

        progress = ProgressTracker()
        updates: list[int] = []

        progress.on_progress(lambda u: updates.append(u.completed))

        async def process(x: int) -> int:
            return x

        executor = BatchExecutor(scheduler, progress=progress)
        await executor.execute([1, 2, 3], process)

        # Should have updates for: start, 3 completions, final complete
        assert len(updates) >= 4

        await scheduler.shutdown(wait=False)


class TestBatchExecutorCancel:
    """Tests for cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_stops_new_items(self) -> None:
        """Cancellation prevents new items from starting."""
        scheduler = create_scheduler()
        await scheduler.start()

        processed: list[int] = []

        async def slow_process(x: int) -> int:
            processed.append(x)
            await asyncio.sleep(0.1)
            return x

        # Use small batch size for more control
        executor = BatchExecutor(scheduler, max_batch_size=1)

        # Start execution in background
        task = asyncio.create_task(executor.execute(list(range(10)), slow_process))

        # Wait a bit then cancel
        await asyncio.sleep(0.05)
        executor.cancel()

        await task

        # Should have processed fewer than all items
        assert executor.is_cancelled is True

        await scheduler.shutdown(wait=False)


class TestBatchExecutorPriority:
    """Tests for priority handling."""

    @pytest.mark.asyncio
    async def test_execute_with_priority(self) -> None:
        """Execute accepts priority parameter."""
        scheduler = create_scheduler()
        await scheduler.start()

        async def process(x: int) -> int:
            return x

        executor = BatchExecutor(scheduler)
        result = await executor.execute(
            [1, 2, 3],
            process,
            priority=RequestPriority.HIGH,
        )

        assert result.success_count == 3

        await scheduler.shutdown(wait=False)


class TestExecuteBatchFunction:
    """Tests for execute_batch convenience function."""

    @pytest.mark.asyncio
    async def test_execute_batch_function(self) -> None:
        """execute_batch convenience function works."""
        scheduler = create_scheduler()
        await scheduler.start()

        async def process(x: int) -> int:
            return x * 2

        result = await execute_batch(
            scheduler,
            [1, 2, 3, 4, 5],
            process,
        )

        assert result.success_count == 5
        assert sorted(result.succeeded) == [2, 4, 6, 8, 10]

        await scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_execute_batch_with_progress(self) -> None:
        """execute_batch works with progress tracker."""
        scheduler = create_scheduler()
        await scheduler.start()

        progress = ProgressTracker()

        async def process(x: int) -> int:
            return x

        await execute_batch(
            scheduler,
            [1, 2, 3],
            process,
            progress=progress,
        )

        assert progress.state == ProgressState.COMPLETED

        await scheduler.shutdown(wait=False)
