"""PR Sync module - GitHub to database synchronization.

This module provides services for syncing PR data from GitHub API
to the local database.

Services:
- PRIngestionService: Single PR ingestion (fetch → transform → store)
- BulkPRIngestionService: Multi-PR ingestion with batch execution
- MultiRepoOrchestrator: Multi-repository sync orchestration
- CommitManager: Batch commit boundaries for database resilience
"""

from .bulk_ingestion import BulkIngestionConfig, BulkIngestionResult, BulkPRIngestionService
from .commit_manager import CommitManager
from .enums import OutputFormat, SyncStrategy
from .ingestion import PRIngestionService
from .multi_repo_orchestrator import (
    MultiRepoOrchestrator,
    MultiRepoSyncResult,
    RepoSyncResult,
)
from .results import PRIngestionResult
from .retry_service import FailureRetryService, RetryResult

__all__ = [
    # Multi-repo orchestration (Phase 1.8)
    "MultiRepoOrchestrator",
    "MultiRepoSyncResult",
    "RepoSyncResult",
    # Bulk ingestion (Phase 1.7)
    "BulkIngestionConfig",
    "BulkIngestionResult",
    "BulkPRIngestionService",
    # Single PR ingestion (Phase 1.6)
    "OutputFormat",
    "PRIngestionResult",
    "PRIngestionService",
    "SyncStrategy",
    # Failure retry (Phase 1.13)
    "FailureRetryService",
    "RetryResult",
    # Commit management (Phase 1.15)
    "CommitManager",
]
