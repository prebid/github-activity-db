"""Repository pattern implementation for database access.

This module provides repository classes that encapsulate all database
access logic, providing a clean abstraction over SQLAlchemy models.
"""

from .base import BaseRepository
from .pull_request import PullRequestRepository
from .repository import RepositoryRepository
from .sync_failure import SyncFailureRepository

__all__ = [
    "BaseRepository",
    "PullRequestRepository",
    "RepositoryRepository",
    "SyncFailureRepository",
]
