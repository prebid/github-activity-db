"""Request pacing and scheduling for GitHub API.

This module provides intelligent request pacing to optimize
GitHub API usage while respecting rate limits.

Components:
- RequestPacer: Token bucket algorithm for delay calculation
- RequestScheduler: Priority queue with concurrency control
- BatchExecutor: Coordinates batch operations
- ProgressTracker: Observable progress reporting
"""

from .batch import BatchExecutor, BatchResult, execute_batch
from .pacer import RequestPacer, wait_with_pacer
from .progress import ProgressCallback, ProgressState, ProgressTracker, ProgressUpdate
from .scheduler import RequestPriority, RequestScheduler, RequestState

__all__ = [
    # Batch execution
    "BatchExecutor",
    "BatchResult",
    "execute_batch",
    # Pacing
    "RequestPacer",
    "wait_with_pacer",
    # Progress tracking
    "ProgressCallback",
    "ProgressState",
    "ProgressTracker",
    "ProgressUpdate",
    # Scheduling
    "RequestPriority",
    "RequestScheduler",
    "RequestState",
]
