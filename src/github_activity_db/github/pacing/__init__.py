"""Request pacing and scheduling for GitHub API.

This module provides intelligent request pacing to optimize GitHub API usage
while respecting rate limits.

Components:
- AsyncTokenBucket: Concurrency-safe adaptive admission gate
- RequestPacer: Pacer wrapper around the token bucket
- RequestScheduler: Priority queue with concurrency control
- BatchExecutor: Coordinates batch operations
- ProgressTracker: Observable progress reporting
"""

from .batch import BatchExecutor, BatchResult, execute_batch
from .pacer import RequestPacer
from .progress import ProgressCallback, ProgressState, ProgressTracker, ProgressUpdate
from .scheduler import RequestPriority, RequestScheduler, RequestState
from .token_bucket import AsyncTokenBucket

__all__ = [
    # Batch execution
    "BatchExecutor",
    "BatchResult",
    "execute_batch",
    # Pacing
    "AsyncTokenBucket",
    "RequestPacer",
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
