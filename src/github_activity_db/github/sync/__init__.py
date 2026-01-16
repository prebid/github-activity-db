"""PR Sync module - GitHub to database synchronization.

This module provides services for syncing PR data from GitHub API
to the local database.

Services:
- PRIngestionService: Single PR ingestion (fetch → transform → store)
- BulkPRIngestionService: Multi-PR ingestion with batch execution
"""

from .bulk_ingestion import BulkIngestionConfig, BulkIngestionResult, BulkPRIngestionService
from .enums import OutputFormat, SyncStrategy
from .ingestion import PRIngestionService
from .results import PRIngestionResult

__all__ = [
    # Bulk ingestion (Phase 1.7)
    "BulkIngestionConfig",
    "BulkIngestionResult",
    "BulkPRIngestionService",
    # Single PR ingestion (Phase 1.6)
    "OutputFormat",
    "PRIngestionResult",
    "PRIngestionService",
    "SyncStrategy",
]
