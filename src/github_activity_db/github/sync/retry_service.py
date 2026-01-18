"""Failure Retry Service - Retry previously failed PR ingestions.

Orchestrates retrying failed PRs that are stored in the sync_failures table,
with configurable max retry limits and status tracking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from github_activity_db.db.repositories import (
    RepositoryRepository,
    SyncFailureRepository,
)
from github_activity_db.logging import get_logger

from .ingestion import PRIngestionService
from .results import PRIngestionResult

if TYPE_CHECKING:
    from github_activity_db.db.models import SyncFailure

logger = get_logger(__name__)


@dataclass
class RetryResult:
    """Result of a failure retry operation.

    Aggregates results from retrying multiple failed PRs.
    """

    total_pending: int = 0
    """Total pending failures found to retry."""

    succeeded: int = 0
    """Failures that were successfully resolved."""

    failed_again: int = 0
    """Failures that failed again on retry."""

    marked_permanent: int = 0
    """Failures marked as permanent (max retries exceeded)."""

    skipped_dry_run: int = 0
    """Failures skipped due to dry-run mode."""

    duration_seconds: float = 0.0
    """Total time taken for the operation."""

    results: list[tuple[int, PRIngestionResult]] = field(default_factory=list)
    """List of (pr_number, ingestion_result) tuples."""

    @property
    def total_attempted(self) -> int:
        """Total failures that were actually attempted (not dry-run)."""
        return self.succeeded + self.failed_again + self.marked_permanent

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "total_pending": self.total_pending,
            "succeeded": self.succeeded,
            "failed_again": self.failed_again,
            "marked_permanent": self.marked_permanent,
            "skipped_dry_run": self.skipped_dry_run,
            "total_attempted": self.total_attempted,
            "duration_seconds": round(self.duration_seconds, 2),
            "results": [
                {
                    "pr_number": pr_num,
                    "success": r.success,
                    "action": r.action,
                    "error": str(r.error) if r.error else None,
                }
                for pr_num, r in self.results
            ],
        }


class FailureRetryService:
    """Service for retrying previously failed PR ingestions.

    Reads pending failures from the database, attempts to re-ingest each PR,
    and updates the failure status based on the result.

    Usage:
        async with GitHubClient() as client:
            async with get_session() as session:
                service = FailureRetryService(
                    ingestion_service=PRIngestionService(client, repo_repo, pr_repo),
                    failure_repository=SyncFailureRepository(session),
                    repo_repository=RepositoryRepository(session),
                )

                result = await service.retry_failures()
                print(f"Resolved: {result.succeeded}, Failed again: {result.failed_again}")
    """

    MAX_RETRIES = 3
    """Maximum retry attempts before marking failure as permanent."""

    def __init__(
        self,
        ingestion_service: PRIngestionService,
        failure_repository: SyncFailureRepository,
        repo_repository: RepositoryRepository,
    ) -> None:
        """Initialize the retry service.

        Args:
            ingestion_service: Service for ingesting individual PRs
            failure_repository: Repository for sync failure records
            repo_repository: Repository for repository records
        """
        self._ingestion_service = ingestion_service
        self._failure_repository = failure_repository
        self._repo_repository = repo_repository

    async def retry_failures(
        self,
        repository_id: int | None = None,
        max_items: int | None = None,
        dry_run: bool = False,
    ) -> RetryResult:
        """Retry pending failures.

        Args:
            repository_id: Filter by repository (optional)
            max_items: Maximum number of failures to retry (optional)
            dry_run: If True, don't actually retry, just report what would happen

        Returns:
            RetryResult with aggregated statistics
        """
        start_time = time.monotonic()
        result = RetryResult()

        # Get pending failures
        limit = max_items or 100
        pending = await self._failure_repository.get_pending(
            repository_id=repository_id,
            limit=limit,
        )
        result.total_pending = len(pending)

        if not pending:
            logger.info("No pending failures to retry")
            result.duration_seconds = time.monotonic() - start_time
            return result

        logger.info(
            "Found %d pending failures to retry (limit=%d, dry_run=%s)",
            len(pending),
            limit,
            dry_run,
        )

        # Cache repositories to avoid repeated lookups
        repo_cache: dict[int, tuple[str, str]] = {}

        for failure in pending:
            pr_result = await self._retry_single_failure(
                failure, repo_cache, dry_run
            )
            result.results.append((failure.pr_number, pr_result))

            if dry_run:
                result.skipped_dry_run += 1
            elif pr_result.success:
                result.succeeded += 1
                await self._failure_repository.mark_resolved(failure.id)
                logger.info(
                    "Resolved failure for PR #%d (retry %d)",
                    failure.pr_number,
                    failure.retry_count,
                )
            else:
                # Check if we've exceeded max retries
                if failure.retry_count >= self.MAX_RETRIES - 1:  # -1 because we just tried
                    result.marked_permanent += 1
                    await self._failure_repository.mark_permanent(failure.id)
                    logger.warning(
                        "PR #%d failed permanently after %d retries: %s",
                        failure.pr_number,
                        failure.retry_count + 1,
                        pr_result.error,
                    )
                else:
                    result.failed_again += 1
                    # Update the failure record with new error and increment retry count
                    await self._failure_repository.record_failure(
                        failure.repository_id,
                        failure.pr_number,
                        pr_result.error or Exception("Unknown error"),
                    )
                    logger.warning(
                        "PR #%d failed again (retry %d/%d): %s",
                        failure.pr_number,
                        failure.retry_count + 1,
                        self.MAX_RETRIES,
                        pr_result.error,
                    )

        result.duration_seconds = time.monotonic() - start_time

        logger.info(
            "Retry complete: succeeded=%d, failed_again=%d, permanent=%d (%.1fs)",
            result.succeeded,
            result.failed_again,
            result.marked_permanent,
            result.duration_seconds,
        )

        return result

    async def _retry_single_failure(
        self,
        failure: SyncFailure,
        repo_cache: dict[int, tuple[str, str]],
        dry_run: bool,
    ) -> PRIngestionResult:
        """Retry a single failure.

        Args:
            failure: The failure record to retry
            repo_cache: Cache of repository_id -> (owner, name)
            dry_run: If True, don't actually retry

        Returns:
            PRIngestionResult from the ingestion attempt
        """
        # Get repository info from cache or database
        if failure.repository_id not in repo_cache:
            repo = await self._repo_repository.get_by_id(failure.repository_id)
            if repo is None:
                logger.error(
                    "Repository %d not found for failure %d",
                    failure.repository_id,
                    failure.id,
                )
                return PRIngestionResult.from_error(
                    ValueError(f"Repository {failure.repository_id} not found")
                )
            repo_cache[failure.repository_id] = (repo.owner, repo.name)

        owner, name = repo_cache[failure.repository_id]

        logger.debug(
            "Retrying PR #%d from %s/%s (attempt %d)",
            failure.pr_number,
            owner,
            name,
            failure.retry_count + 1,
        )

        # Attempt to ingest the PR
        return await self._ingestion_service.ingest_pr(
            owner, name, failure.pr_number, dry_run=dry_run
        )

    async def get_failure_stats(
        self,
        repository_id: int | None = None,
    ) -> dict[str, Any]:
        """Get statistics about failures.

        Args:
            repository_id: Filter by repository (optional)

        Returns:
            Dictionary with failure statistics by status
        """
        return await self._failure_repository.get_stats(repository_id)
