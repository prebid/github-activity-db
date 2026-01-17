"""Batch executor for coordinating multiple API requests.

This module provides a BatchExecutor that coordinates batch operations
using the RequestScheduler for rate limiting and the ProgressTracker
for progress reporting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from github_activity_db.logging import get_logger

from .progress import ProgressTracker
from .scheduler import RequestPriority, RequestScheduler

logger = get_logger(__name__)

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class BatchResult(Generic[T]):
    """Result of a batch operation."""

    succeeded: list[T] = field(default_factory=list)
    failed: list[tuple[int, Exception]] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        """Total number of items processed."""
        return len(self.succeeded) + len(self.failed)

    @property
    def success_count(self) -> int:
        """Number of successful items."""
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        """Number of failed items."""
        return len(self.failed)

    @property
    def all_succeeded(self) -> bool:
        """Whether all items succeeded."""
        return len(self.failed) == 0


class BatchExecutor(Generic[T, R]):
    """Coordinates batch API operations with rate limiting and progress.

    Usage:
        scheduler = RequestScheduler(pacer)
        executor = BatchExecutor(scheduler)

        # Define how to process each item
        async def fetch_pr(number: int) -> GitHubPullRequest:
            return await client.get_pull_request("owner", "repo", number)

        # Execute batch
        pr_numbers = [1, 2, 3, 4, 5]
        result = await executor.execute(pr_numbers, fetch_pr)

        print(f"Fetched {result.success_count} PRs")
        for pr in result.succeeded:
            print(pr.title)
    """

    def __init__(
        self,
        scheduler: RequestScheduler,
        progress: ProgressTracker | None = None,
        stop_on_error: bool = False,
        max_batch_size: int = 50,
    ) -> None:
        """Initialize the batch executor.

        Args:
            scheduler: RequestScheduler for rate limiting
            progress: Optional ProgressTracker for progress reporting
            stop_on_error: If True, stop batch on first error
            max_batch_size: Maximum items to process in a single batch
        """
        self._scheduler = scheduler
        self._progress = progress
        self._stop_on_error = stop_on_error
        self._max_batch_size = max_batch_size
        self._cancelled = False

    async def execute(
        self,
        items: Sequence[T],
        processor: Callable[[T], Awaitable[R]],
        *,
        priority: RequestPriority = RequestPriority.NORMAL,
        item_name: Callable[[T], str] | None = None,
    ) -> BatchResult[R]:
        """Execute a batch operation on all items.

        Args:
            items: Sequence of items to process
            processor: Async function to process each item
            priority: Request priority for all items
            item_name: Optional function to get display name for an item

        Returns:
            BatchResult containing succeeded results and failed items
        """
        self._cancelled = False
        result: BatchResult[R] = BatchResult()

        if not items:
            return result

        # Set up progress tracking
        if self._progress:
            self._progress.total = len(items)
            self._progress.start()

        try:
            # Process in batches to avoid overwhelming the queue
            for batch_start in range(0, len(items), self._max_batch_size):
                if self._cancelled:
                    break

                batch = items[batch_start : batch_start + self._max_batch_size]
                batch_result = await self._execute_batch(
                    batch,
                    processor,
                    priority=priority,
                    item_name=item_name,
                    start_index=batch_start,
                )

                result.succeeded.extend(batch_result.succeeded)
                result.failed.extend(batch_result.failed)

                if self._stop_on_error and batch_result.failed:
                    break

            # Mark completion
            if self._progress:
                if self._cancelled:
                    self._progress.cancel()
                elif result.failed and self._stop_on_error:
                    self._progress.fail(f"Stopped on error: {result.failed[0][1]}")
                else:
                    self._progress.complete()

        except Exception as e:
            logger.exception("Batch execution failed")
            if self._progress:
                self._progress.fail(str(e))
            raise

        return result

    async def _execute_batch(
        self,
        batch: Sequence[T],
        processor: Callable[[T], Awaitable[R]],
        *,
        priority: RequestPriority,
        item_name: Callable[[T], str] | None,
        start_index: int,
    ) -> BatchResult[R]:
        """Execute a single batch of items.

        Args:
            batch: Items in this batch
            processor: Async function to process each item
            priority: Request priority
            item_name: Optional name function
            start_index: Starting index for error reporting

        Returns:
            BatchResult for this batch
        """
        result: BatchResult[R] = BatchResult()

        # Create tasks for all items in batch
        tasks: list[asyncio.Task[R]] = []
        for i, item in enumerate(batch):
            if self._cancelled:
                break

            # Update progress with current item
            if self._progress and item_name:
                self._progress.set_current(item_name(item))

            # Create coroutine factory for scheduler
            def make_factory(it: T) -> Callable[[], Awaitable[R]]:
                return lambda: processor(it)

            # Submit to scheduler
            task = asyncio.create_task(
                self._execute_item(
                    item=item,
                    processor=processor,
                    index=start_index + i,
                    priority=priority,
                )
            )
            tasks.append(task)

        # Wait for all tasks to complete
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    result.failed.append((start_index + i, res))
                    if self._progress:
                        self._progress.increment_failed(error=str(res))
                else:
                    result.succeeded.append(res)  # type: ignore[arg-type]
                    if self._progress:
                        self._progress.increment()

        return result

    async def _execute_item(
        self,
        item: T,
        processor: Callable[[T], Awaitable[R]],
        index: int,
        priority: RequestPriority,
    ) -> R:
        """Execute a single item through the scheduler.

        Args:
            item: Item to process
            processor: Processing function
            index: Item index for error reporting
            priority: Request priority

        Returns:
            Processing result
        """
        return await self._scheduler.submit(
            lambda: processor(item),
            priority=priority,
        )

    def cancel(self) -> None:
        """Cancel the batch operation.

        Running items will complete, but no new items will be started.
        """
        self._cancelled = True
        logger.info("Batch execution cancelled")

    @property
    def is_cancelled(self) -> bool:
        """Whether the batch has been cancelled."""
        return self._cancelled


async def execute_batch(
    scheduler: RequestScheduler,
    items: Sequence[T],
    processor: Callable[[T], Awaitable[R]],
    *,
    priority: RequestPriority = RequestPriority.NORMAL,
    progress: ProgressTracker | None = None,
    stop_on_error: bool = False,
    max_batch_size: int = 50,
) -> BatchResult[R]:
    """Convenience function for one-off batch execution.

    Args:
        scheduler: RequestScheduler for rate limiting
        items: Sequence of items to process
        processor: Async function to process each item
        priority: Request priority for all items
        progress: Optional ProgressTracker
        stop_on_error: If True, stop on first error
        max_batch_size: Maximum batch size

    Returns:
        BatchResult containing succeeded results and failed items
    """
    executor: BatchExecutor[T, R] = BatchExecutor(
        scheduler=scheduler,
        progress=progress,
        stop_on_error=stop_on_error,
        max_batch_size=max_batch_size,
    )
    return await executor.execute(items, processor, priority=priority)
