"""Bulk PR Ingestion Service - Multi-PR import with batch execution.

Orchestrates the bulk import of PRs from a repository using the existing
single-PR ingestion pipeline (PRIngestionService) with batch execution
infrastructure for efficient rate-limited imports.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from github_activity_db.db.repositories import PullRequestRepository, RepositoryRepository
from github_activity_db.github.pacing import (
    BatchExecutor,
    ProgressTracker,
    RequestPriority,
    RequestScheduler,
)

from .ingestion import PRIngestionService
from .results import PRIngestionResult

if TYPE_CHECKING:
    from github_activity_db.github.client import GitHubClient

logger = logging.getLogger(__name__)


@dataclass
class BulkIngestionConfig:
    """Configuration for bulk PR ingestion.

    Controls what PRs to import and how to process them.
    """

    since: datetime | None = None
    """Only import PRs created after this datetime."""

    until: datetime | None = None
    """Only import PRs created before this datetime."""

    state: Literal["open", "merged", "all"] = "all"
    """Filter PRs by state. 'all' includes both open and merged (excludes abandoned)."""

    max_prs: int | None = None
    """Maximum number of PRs to import (useful for testing)."""

    concurrency: int = 5
    """Number of concurrent PR ingestions."""

    dry_run: bool = False
    """If True, don't write to database."""


@dataclass
class BulkIngestionResult:
    """Result of a bulk PR ingestion operation.

    Aggregates results from multiple individual PR ingestions.
    """

    total_discovered: int = 0
    """Total PRs discovered matching filters."""

    created: int = 0
    """New PRs created in database."""

    updated: int = 0
    """Existing PRs updated (hot path)."""

    skipped_frozen: int = 0
    """PRs skipped because frozen (cold path - merged past grace period)."""

    skipped_unchanged: int = 0
    """PRs skipped because unchanged since last sync."""

    failed: int = 0
    """PRs that failed to ingest."""

    failed_prs: list[tuple[int, str]] = field(default_factory=list)
    """List of (pr_number, error_message) for failed PRs."""

    duration_seconds: float = 0.0
    """Total time taken for the operation."""

    @property
    def total_processed(self) -> int:
        """Total PRs that were processed (not including skipped)."""
        return self.created + self.updated + self.failed

    @property
    def total_skipped(self) -> int:
        """Total PRs skipped (frozen + unchanged)."""
        return self.skipped_frozen + self.skipped_unchanged

    @property
    def success_rate(self) -> float:
        """Percentage of processed PRs that succeeded (excluding skipped)."""
        processed = self.total_processed
        if processed == 0:
            return 100.0
        return ((self.created + self.updated) / processed) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "total_discovered": self.total_discovered,
            "created": self.created,
            "updated": self.updated,
            "skipped_frozen": self.skipped_frozen,
            "skipped_unchanged": self.skipped_unchanged,
            "failed": self.failed,
            "failed_prs": [
                {"pr_number": num, "error": msg} for num, msg in self.failed_prs
            ],
            "duration_seconds": round(self.duration_seconds, 2),
            "success_rate": round(self.success_rate, 1),
        }


class BulkPRIngestionService:
    """Service for bulk PR ingestion from a repository.

    Orchestrates the discovery and batch import of multiple PRs using
    the existing PRIngestionService for per-PR processing and the
    BatchExecutor for rate-limited parallel execution.

    Usage:
        async with GitHubClient() as client:
            async with get_session() as session:
                scheduler = RequestScheduler(pacer)
                await scheduler.start()

                service = BulkPRIngestionService(
                    client=client,
                    repo_repository=RepositoryRepository(session),
                    pr_repository=PullRequestRepository(session),
                    scheduler=scheduler,
                )

                config = BulkIngestionConfig(since=datetime(2024, 10, 1))
                result = await service.ingest_repository("prebid", "prebid-server", config)

                await scheduler.shutdown()
    """

    def __init__(
        self,
        client: GitHubClient,
        repo_repository: RepositoryRepository,
        pr_repository: PullRequestRepository,
        scheduler: RequestScheduler,
        progress: ProgressTracker | None = None,
    ) -> None:
        """Initialize the bulk ingestion service.

        Args:
            client: GitHub API client
            repo_repository: Repository for Repository model
            pr_repository: Repository for PullRequest model
            scheduler: RequestScheduler for rate-limited execution
            progress: Optional ProgressTracker for progress reporting
        """
        self._client = client
        self._repo_repository = repo_repository
        self._pr_repository = pr_repository
        self._scheduler = scheduler
        self._progress = progress

    async def discover_prs(
        self,
        owner: str,
        repo: str,
        config: BulkIngestionConfig,
    ) -> list[int]:
        """Discover PR numbers matching the configuration filters.

        Lists PRs from GitHub API and filters them based on:
        - Date range (since/until)
        - State (open, merged, or both - always excludes abandoned)
        - Max count limit

        Args:
            owner: Repository owner
            repo: Repository name
            config: Bulk ingestion configuration

        Returns:
            List of PR numbers to ingest
        """
        logger.info(
            "Discovering PRs for %s/%s (since=%s, until=%s, state=%s, max=%s)",
            owner,
            repo,
            config.since,
            config.until,
            config.state,
            config.max_prs,
        )

        # Iterate PRs lazily, sorted by created date descending
        # Using iter_pull_requests allows early termination when we hit PRs
        # older than the since date - saves API calls on large repos
        pr_numbers: list[int] = []

        async for pr in self._client.iter_pull_requests(
            owner,
            repo,
            state="all",
            sort="created",
            direction="desc",
        ):
            # Date filtering - since
            if config.since and pr.created_at < config.since:
                # PRs are sorted by created desc, so we can stop early
                logger.debug("PR #%d created before since date, stopping", pr.number)
                break

            # Date filtering - until
            if config.until and pr.created_at > config.until:
                logger.debug("PR #%d created after until date, skipping", pr.number)
                continue

            # State filtering - exclude abandoned PRs (closed but not merged)
            is_open = pr.state == "open"
            is_merged = pr.merged

            if config.state == "open" and not is_open:
                continue
            elif config.state == "merged" and not is_merged:
                continue
            elif config.state == "all":
                # Include open and merged, exclude abandoned (closed but not merged)
                if not is_open and not is_merged:
                    logger.debug(
                        "PR #%d is abandoned (closed but not merged), skipping",
                        pr.number,
                    )
                    continue

            pr_numbers.append(pr.number)

            # Max limit check
            if config.max_prs and len(pr_numbers) >= config.max_prs:
                logger.info("Reached max PR limit (%d)", config.max_prs)
                break

        logger.info("Discovered %d PRs matching filters", len(pr_numbers))
        return pr_numbers

    async def ingest_repository(
        self,
        owner: str,
        repo: str,
        config: BulkIngestionConfig,
    ) -> BulkIngestionResult:
        """Ingest all PRs from a repository matching the configuration.

        Flow:
            1. Discover PR numbers matching filters
            2. Create PRIngestionService for per-PR processing
            3. Use BatchExecutor to process PRs in parallel with rate limiting
            4. Aggregate individual results into BulkIngestionResult

        Args:
            owner: Repository owner
            repo: Repository name
            config: Bulk ingestion configuration

        Returns:
            BulkIngestionResult with aggregated statistics
        """
        start_time = time.monotonic()
        result = BulkIngestionResult()

        # Step 1: Discover PRs
        pr_numbers = await self.discover_prs(owner, repo, config)
        result.total_discovered = len(pr_numbers)

        if not pr_numbers:
            logger.info("No PRs to ingest for %s/%s", owner, repo)
            result.duration_seconds = time.monotonic() - start_time
            return result

        # Step 2: Create per-PR ingestion service
        ingestion_service = PRIngestionService(
            client=self._client,
            repo_repository=self._repo_repository,
            pr_repository=self._pr_repository,
        )

        # Step 3: Define processor function
        async def ingest_one(pr_number: int) -> PRIngestionResult:
            return await ingestion_service.ingest_pr(
                owner, repo, pr_number, dry_run=config.dry_run
            )

        # Step 4: Execute batch
        # Create progress tracker if not provided
        progress = self._progress
        if progress is None:
            progress = ProgressTracker(total=len(pr_numbers), name="PR Import")

        executor: BatchExecutor[int, PRIngestionResult] = BatchExecutor(
            scheduler=self._scheduler,
            progress=progress,
            stop_on_error=False,
            max_batch_size=50,
        )

        batch_result = await executor.execute(
            pr_numbers,
            ingest_one,
            priority=RequestPriority.NORMAL,
            item_name=lambda n: f"PR #{n}",
        )

        # Step 5: Aggregate results
        for pr_result in batch_result.succeeded:
            if pr_result.created:
                result.created += 1
            elif pr_result.updated:
                result.updated += 1
            elif pr_result.skipped_frozen:
                result.skipped_frozen += 1
            elif pr_result.skipped_unchanged:
                result.skipped_unchanged += 1
            elif pr_result.error:
                result.failed += 1
                result.failed_prs.append(
                    (pr_result.pr.number if pr_result.pr else -1, str(pr_result.error))
                )

        # Handle batch-level failures (exceptions during processing)
        for index, error in batch_result.failed:
            result.failed += 1
            pr_number = pr_numbers[index] if index < len(pr_numbers) else -1
            result.failed_prs.append((pr_number, str(error)))

        result.duration_seconds = time.monotonic() - start_time

        logger.info(
            "Bulk ingestion complete for %s/%s: "
            "created=%d, updated=%d, skipped_frozen=%d, skipped_unchanged=%d, failed=%d "
            "(%.1fs)",
            owner,
            repo,
            result.created,
            result.updated,
            result.skipped_frozen,
            result.skipped_unchanged,
            result.failed,
            result.duration_seconds,
        )

        return result
