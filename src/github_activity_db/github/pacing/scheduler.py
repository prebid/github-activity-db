"""Request scheduler with priority queue.

This module provides a priority-based async request scheduler that
coordinates with the RequestPacer to execute requests at optimal times.

Features:
- Priority queue (HIGH > NORMAL > LOW)
- Semaphore-controlled concurrency
- Retry with exponential backoff
- Rate limit error handling
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any, Generic, TypeVar

from github_activity_db.github.exceptions import GitHubRateLimitError

from .pacer import RequestPacer

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RequestPriority(IntEnum):
    """Priority levels for request scheduling.

    Lower values = higher priority (executed first).
    """

    HIGH = 1  # Rate limit checks, critical operations
    NORMAL = 2  # Regular sync operations
    LOW = 3  # Background/optional operations


class RequestState(IntEnum):
    """State of a queued request."""

    PENDING = 1
    IN_FLIGHT = 2
    COMPLETED = 3
    FAILED = 4
    CANCELLED = 5


@dataclass(order=True)
class QueuedRequest(Generic[T]):
    """A request waiting to be executed.

    Ordering is by (priority, created_at) for heapq.
    """

    # Fields used for ordering (must come first for dataclass ordering)
    priority: int = field(compare=True)
    created_at_ts: float = field(compare=True)

    # Non-ordering fields
    id: str = field(compare=False)
    coro_factory: Callable[[], Awaitable[T]] = field(compare=False)
    state: RequestState = field(default=RequestState.PENDING, compare=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC), compare=False)
    started_at: datetime | None = field(default=None, compare=False)
    completed_at: datetime | None = field(default=None, compare=False)
    result: Any = field(default=None, compare=False)
    error: Exception | None = field(default=None, compare=False)
    retry_count: int = field(default=0, compare=False)
    max_retries: int = field(default=3, compare=False)


class RequestScheduler:
    """Priority-based async request scheduler with rate limiting.

    Usage:
        pacer = RequestPacer(monitor)
        scheduler = RequestScheduler(pacer)
        await scheduler.start()

        # Submit and wait for result
        result = await scheduler.submit(
            lambda: client.get_pull_request(owner, repo, 123)
        )

        # Or fire-and-forget
        request_id = scheduler.enqueue(my_coro_factory, priority=RequestPriority.LOW)

        await scheduler.shutdown()
    """

    def __init__(
        self,
        pacer: RequestPacer,
        max_concurrent: int = 5,
        max_retries: int = 3,
    ) -> None:
        """Initialize the request scheduler.

        Args:
            pacer: RequestPacer for delay calculations
            max_concurrent: Maximum concurrent requests (default 5)
            max_retries: Maximum retry attempts per request (default 3)
        """
        self._pacer = pacer
        self._max_concurrent = max_concurrent
        self._max_retries = max_retries

        # Priority queue: stores QueuedRequest objects
        self._queue: list[QueuedRequest[Any]] = []
        self._queue_lock = asyncio.Lock()

        # Concurrency control
        self._semaphore = asyncio.Semaphore(max_concurrent)

        # State
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None
        self._pending_futures: dict[str, asyncio.Future[Any]] = {}
        self._active_tasks: set[asyncio.Task[None]] = set()  # Prevent task GC

        # Statistics
        self._total_submitted = 0
        self._total_completed = 0
        self._total_failed = 0

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------
    async def start(self) -> None:
        """Start the scheduler worker loop.

        The worker loop processes requests from the queue,
        respecting rate limits and concurrency.
        """
        if self._running:
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("Request scheduler started (max_concurrent=%d)", self._max_concurrent)

    async def shutdown(self, wait: bool = True, timeout: float = 30.0) -> None:
        """Stop the scheduler.

        Args:
            wait: If True, wait for pending requests to complete
            timeout: Maximum seconds to wait for pending requests
        """
        self._running = False

        if wait and self._queue:
            logger.info("Waiting for %d pending requests...", len(self._queue))
            try:
                # Wait for queue to drain with timeout
                start = asyncio.get_event_loop().time()
                while self._queue and (asyncio.get_event_loop().time() - start) < timeout:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        # Cancel any pending futures
        for future in self._pending_futures.values():
            if not future.done():
                future.cancel()
        self._pending_futures.clear()

        logger.info(
            "Request scheduler stopped (completed=%d, failed=%d)",
            self._total_completed,
            self._total_failed,
        )

    @property
    def is_running(self) -> bool:
        """Whether the scheduler is running."""
        return self._running

    # -------------------------------------------------------------------------
    # Request Submission
    # -------------------------------------------------------------------------
    def enqueue(
        self,
        coro_factory: Callable[[], Awaitable[T]],
        priority: RequestPriority = RequestPriority.NORMAL,
    ) -> str:
        """Add a request to the queue (fire-and-forget).

        Args:
            coro_factory: Factory function that creates the coroutine to execute
            priority: Request priority (HIGH, NORMAL, LOW)

        Returns:
            Request ID for tracking
        """
        request_id = str(uuid.uuid4())
        now = datetime.now(UTC)

        request: QueuedRequest[T] = QueuedRequest(
            priority=priority.value,
            created_at_ts=now.timestamp(),
            id=request_id,
            coro_factory=coro_factory,
            created_at=now,
            max_retries=self._max_retries,
        )

        heapq.heappush(self._queue, request)
        self._total_submitted += 1

        logger.debug(
            "Enqueued request %s (priority=%s, queue_size=%d)",
            request_id[:8],
            priority.name,
            len(self._queue),
        )

        return request_id

    async def submit(
        self,
        coro_factory: Callable[[], Awaitable[T]],
        priority: RequestPriority = RequestPriority.NORMAL,
        timeout: float | None = None,
    ) -> T:
        """Submit a request and wait for its result.

        Args:
            coro_factory: Factory function that creates the coroutine to execute
            priority: Request priority (HIGH, NORMAL, LOW)
            timeout: Optional timeout in seconds

        Returns:
            Result of the coroutine

        Raises:
            asyncio.TimeoutError: If timeout exceeded
            Exception: Any exception from the coroutine
        """
        request_id = self.enqueue(coro_factory, priority)

        # Create a future to wait on
        future: asyncio.Future[T] = asyncio.get_event_loop().create_future()
        self._pending_futures[request_id] = future

        try:
            if timeout:
                return await asyncio.wait_for(future, timeout)
            return await future
        finally:
            self._pending_futures.pop(request_id, None)

    # -------------------------------------------------------------------------
    # Worker Loop
    # -------------------------------------------------------------------------
    async def _worker_loop(self) -> None:
        """Main worker loop that processes the queue."""
        while self._running or self._queue:
            if not self._queue:
                await asyncio.sleep(0.01)
                continue

            # Get recommended delay from pacer
            delay = self._pacer.get_recommended_delay()
            if delay > 0:
                await asyncio.sleep(delay)

            # Get next request (if available)
            async with self._queue_lock:
                if not self._queue:
                    continue
                request = heapq.heappop(self._queue)

            # Execute with concurrency control
            task = asyncio.create_task(self._execute_request(request))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _execute_request(self, request: QueuedRequest[Any]) -> None:
        """Execute a single request with semaphore control."""
        async with self._semaphore:
            request.state = RequestState.IN_FLIGHT
            request.started_at = datetime.now(UTC)

            logger.debug("Executing request %s", request.id[:8])

            try:
                # Record request start for velocity tracking
                self._pacer.on_request_start()

                # Execute the coroutine
                result = await request.coro_factory()

                # Record completion
                self._pacer.on_request_complete()

                request.result = result
                request.state = RequestState.COMPLETED
                request.completed_at = datetime.now(UTC)
                self._total_completed += 1

                logger.debug("Request %s completed successfully", request.id[:8])

                # Resolve pending future if any
                if request.id in self._pending_futures:
                    future = self._pending_futures[request.id]
                    if not future.done():
                        future.set_result(result)

            except Exception as e:
                await self._handle_request_error(request, e)

    async def _handle_request_error(
        self,
        request: QueuedRequest[Any],
        error: Exception,
    ) -> None:
        """Handle request failure with retry logic."""
        request.retry_count += 1
        request.error = error

        logger.warning(
            "Request %s failed (attempt %d/%d): %s",
            request.id[:8],
            request.retry_count,
            request.max_retries,
            error,
        )

        # Handle rate limit errors specially
        if isinstance(error, GitHubRateLimitError):
            if error.reset_at:
                wait_time = (error.reset_at - datetime.now(UTC)).total_seconds()
                if wait_time > 0:
                    logger.info("Rate limited, waiting %.1f seconds", wait_time)
                    self._pacer.force_wait(wait_time + 5)  # Add 5s buffer

            # Requeue with high priority if retries remaining
            if request.retry_count <= request.max_retries:
                request.state = RequestState.PENDING
                request.priority = RequestPriority.HIGH.value  # Boost priority
                heapq.heappush(self._queue, request)
                return

        # Retry with exponential backoff for other errors
        if request.retry_count <= request.max_retries:
            backoff = min(2 ** request.retry_count, 60)  # Cap at 60 seconds
            logger.debug("Retrying request %s in %d seconds", request.id[:8], backoff)
            await asyncio.sleep(backoff)

            request.state = RequestState.PENDING
            heapq.heappush(self._queue, request)
            return

        # Max retries exceeded
        request.state = RequestState.FAILED
        request.completed_at = datetime.now(UTC)
        self._total_failed += 1

        logger.error("Request %s failed permanently: %s", request.id[:8], error)

        # Reject pending future
        if request.id in self._pending_futures:
            future = self._pending_futures[request.id]
            if not future.done():
                future.set_exception(error)

    # -------------------------------------------------------------------------
    # Query Methods
    # -------------------------------------------------------------------------
    @property
    def queue_size(self) -> int:
        """Number of pending requests in queue."""
        return len(self._queue)

    @property
    def is_idle(self) -> bool:
        """True if no pending or in-flight requests."""
        return len(self._queue) == 0 and self._semaphore._value == self._max_concurrent

    def get_stats(self) -> dict[str, int | bool]:
        """Get scheduler statistics.

        Returns:
            Dict with queue_size, total_submitted, total_completed, etc.
        """
        return {
            "queue_size": len(self._queue),
            "is_running": self._running,
            "is_idle": self.is_idle,
            "max_concurrent": self._max_concurrent,
            "total_submitted": self._total_submitted,
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
        }
