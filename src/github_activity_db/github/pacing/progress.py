"""Progress tracking for batch operations.

This module provides observable progress tracking for long-running
GitHub API operations like sync jobs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ProgressState(StrEnum):
    """State of a tracked operation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ProgressUpdate:
    """A progress update event."""

    total: int
    completed: int
    failed: int
    state: ProgressState
    current_item: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    elapsed_seconds: float = 0.0

    @property
    def remaining(self) -> int:
        """Number of items remaining."""
        return max(0, self.total - self.completed - self.failed)

    @property
    def progress_percent(self) -> float:
        """Completion percentage (0-100)."""
        if self.total == 0:
            return 100.0
        return ((self.completed + self.failed) / self.total) * 100

    @property
    def success_rate(self) -> float:
        """Success rate percentage (0-100)."""
        processed = self.completed + self.failed
        if processed == 0:
            return 100.0
        return (self.completed / processed) * 100


ProgressCallback = Callable[[ProgressUpdate], None]


class ProgressTracker:
    """Observable progress tracker for batch operations.

    Usage:
        tracker = ProgressTracker(total=100)

        # Register callbacks
        tracker.on_progress(lambda update: print(f"{update.progress_percent}%"))

        # Update progress
        tracker.start()
        for item in items:
            tracker.set_current(item.name)
            process(item)
            tracker.increment()
        tracker.complete()
    """

    def __init__(self, total: int = 0, name: str = "operation") -> None:
        """Initialize the progress tracker.

        Args:
            total: Total number of items to process
            name: Name of the operation for logging
        """
        self._total = total
        self._name = name
        self._completed = 0
        self._failed = 0
        self._state = ProgressState.PENDING
        self._current_item: str | None = None
        self._error: str | None = None
        self._started_at: datetime | None = None
        self._start_time: float | None = None
        self._callbacks: list[ProgressCallback] = []
        self._metadata: dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------
    @property
    def total(self) -> int:
        """Total number of items to process."""
        return self._total

    @total.setter
    def total(self, value: int) -> None:
        """Set total items (useful when total is not known upfront)."""
        self._total = value
        self._notify()

    @property
    def completed(self) -> int:
        """Number of successfully completed items."""
        return self._completed

    @property
    def failed(self) -> int:
        """Number of failed items."""
        return self._failed

    @property
    def state(self) -> ProgressState:
        """Current state of the operation."""
        return self._state

    @property
    def is_running(self) -> bool:
        """Whether the operation is currently in progress."""
        return self._state == ProgressState.IN_PROGRESS

    @property
    def is_done(self) -> bool:
        """Whether the operation has finished (completed, failed, or cancelled)."""
        return self._state in (
            ProgressState.COMPLETED,
            ProgressState.FAILED,
            ProgressState.CANCELLED,
        )

    @property
    def elapsed_seconds(self) -> float:
        """Elapsed time since start in seconds."""
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    # -------------------------------------------------------------------------
    # Callbacks
    # -------------------------------------------------------------------------
    def on_progress(self, callback: ProgressCallback) -> None:
        """Register a progress callback.

        Callback receives ProgressUpdate on every change.

        Args:
            callback: Function to call on progress updates
        """
        self._callbacks.append(callback)

    def _notify(self) -> None:
        """Notify all registered callbacks."""
        update = ProgressUpdate(
            total=self._total,
            completed=self._completed,
            failed=self._failed,
            state=self._state,
            current_item=self._current_item,
            error=self._error,
            started_at=self._started_at,
            elapsed_seconds=self.elapsed_seconds,
        )
        for callback in self._callbacks:
            try:
                callback(update)
            except Exception as e:
                logger.warning("Progress callback error: %s", e)

    # -------------------------------------------------------------------------
    # State Management
    # -------------------------------------------------------------------------
    def start(self) -> None:
        """Mark operation as started."""
        self._state = ProgressState.IN_PROGRESS
        self._started_at = datetime.now(UTC)
        self._start_time = time.monotonic()
        logger.info("Started %s (total=%d)", self._name, self._total)
        self._notify()

    def complete(self) -> None:
        """Mark operation as successfully completed."""
        self._state = ProgressState.COMPLETED
        self._current_item = None
        logger.info(
            "Completed %s: %d succeeded, %d failed in %.1fs",
            self._name,
            self._completed,
            self._failed,
            self.elapsed_seconds,
        )
        self._notify()

    def fail(self, error: str) -> None:
        """Mark operation as failed.

        Args:
            error: Error message describing the failure
        """
        self._state = ProgressState.FAILED
        self._error = error
        self._current_item = None
        logger.error("Failed %s: %s", self._name, error)
        self._notify()

    def cancel(self) -> None:
        """Mark operation as cancelled."""
        self._state = ProgressState.CANCELLED
        self._current_item = None
        logger.info(
            "Cancelled %s at %d/%d", self._name, self._completed + self._failed, self._total
        )
        self._notify()

    # -------------------------------------------------------------------------
    # Progress Updates
    # -------------------------------------------------------------------------
    def set_current(self, item: str) -> None:
        """Set the current item being processed.

        Args:
            item: Description of current item
        """
        self._current_item = item
        self._notify()

    def increment(self, count: int = 1) -> None:
        """Increment completed count.

        Args:
            count: Number of items completed (default 1)
        """
        self._completed += count
        self._current_item = None
        logger.debug(
            "%s progress: %d/%d (%.1f%%)",
            self._name,
            self._completed + self._failed,
            self._total,
            self.get_update().progress_percent,
        )
        self._notify()

    def increment_failed(self, count: int = 1, error: str | None = None) -> None:
        """Increment failed count.

        Args:
            count: Number of items that failed (default 1)
            error: Optional error description
        """
        self._failed += count
        self._current_item = None
        if error:
            logger.warning("%s item failed: %s", self._name, error)
        self._notify()

    def add_total(self, count: int) -> None:
        """Add to total count (for dynamic totals).

        Args:
            count: Number of items to add to total
        """
        self._total += count
        self._notify()

    # -------------------------------------------------------------------------
    # Query Methods
    # -------------------------------------------------------------------------
    def get_update(self) -> ProgressUpdate:
        """Get current progress as an update object."""
        return ProgressUpdate(
            total=self._total,
            completed=self._completed,
            failed=self._failed,
            state=self._state,
            current_item=self._current_item,
            error=self._error,
            started_at=self._started_at,
            elapsed_seconds=self.elapsed_seconds,
        )

    def set_metadata(self, key: str, value: Any) -> None:
        """Store arbitrary metadata.

        Args:
            key: Metadata key
            value: Metadata value
        """
        self._metadata[key] = value

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Retrieve stored metadata.

        Args:
            key: Metadata key
            default: Default value if key not found

        Returns:
            Stored value or default
        """
        return self._metadata.get(key, default)

    def reset(self) -> None:
        """Reset tracker for reuse."""
        self._completed = 0
        self._failed = 0
        self._state = ProgressState.PENDING
        self._current_item = None
        self._error = None
        self._started_at = None
        self._start_time = None
        self._metadata.clear()
        self._notify()
