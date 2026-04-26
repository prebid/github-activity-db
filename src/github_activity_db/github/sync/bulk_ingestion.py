"""Bulk PR Ingestion Service - Multi-PR import with batch execution.

Orchestrates the bulk import of PRs from a repository using the existing
single-PR ingestion pipeline (PRIngestionService) with batch execution
infrastructure for efficient rate-limited imports.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from github_activity_db.db.models import PRState
from github_activity_db.db.repositories import (
    PullRequestRepository,
    RepositoryRepository,
    SyncFailureRepository,
)
from github_activity_db.github.exceptions import GitHubRateLimitError
from github_activity_db.github.pacing import (
    BatchExecutor,
    ProgressTracker,
    RequestPriority,
    RequestScheduler,
)
from github_activity_db.logging import get_logger

from .ingestion import PRIngestionService
from .results import PRIngestionResult

if TYPE_CHECKING:
    from github_activity_db.github.client import GitHubClient

    from .commit_manager import CommitManager

logger = get_logger(__name__)


@dataclass
class BulkIngestionConfig:
    """Configuration for bulk PR ingestion.

    Controls what PRs to import and how to process them.
    """

    since: datetime | None = None
    """Only import PRs whose ``updated_at`` is after this datetime.

    Filtering on ``updated_at`` (rather than ``created_at``) is essential:
    a PR opened months ago can change state (open → merged) at any time,
    and a created-date filter would silently miss those updates. Combined
    with the OPEN-PR sweep in :meth:`ingest_repository`, this guarantees
    state transitions are picked up regardless of when the PR was opened.
    """

    until: datetime | None = None
    """Only import PRs whose ``updated_at`` is before this datetime."""

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

    skipped_abandoned: int = 0
    """PRs skipped because abandoned (closed but not merged)."""

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
        """Total PRs skipped (frozen + unchanged + abandoned)."""
        return self.skipped_frozen + self.skipped_unchanged + self.skipped_abandoned

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
            "skipped_abandoned": self.skipped_abandoned,
            "failed": self.failed,
            "failed_prs": [{"pr_number": num, "error": msg} for num, msg in self.failed_prs],
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

    # Upper bound for the OPEN-PR sweep to prevent a runaway sync on a repo
    # with thousands of stale-open PRs. Picked to leave headroom in a 5000/hr
    # quota: 500 PRs x ~4 calls each = 2000 calls, ~40% of an hour's budget.
    _OPEN_SWEEP_CAP = 500

    def __init__(
        self,
        client: GitHubClient,
        repo_repository: RepositoryRepository,
        pr_repository: PullRequestRepository,
        scheduler: RequestScheduler,
        progress: ProgressTracker | None = None,
        failure_repository: SyncFailureRepository | None = None,
        commit_manager: CommitManager | None = None,
    ) -> None:
        """Initialize the bulk ingestion service.

        Args:
            client: GitHub API client
            repo_repository: Repository for Repository model
            pr_repository: Repository for PullRequest model
            scheduler: RequestScheduler for rate-limited execution
            progress: Optional ProgressTracker for progress reporting
            failure_repository: Optional repository for tracking failed PRs
            commit_manager: Optional CommitManager for batch commits.
                            When provided, commits are made in batches to limit
                            data loss on failure.
        """
        self._client = client
        self._repo_repository = repo_repository
        self._pr_repository = pr_repository
        self._scheduler = scheduler
        self._progress = progress
        self._failure_repository = failure_repository
        self._commit_manager = commit_manager

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

        Includes retry logic for rate limit errors.

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

        max_retries = 3
        pr_numbers: list[int] = []

        for attempt in range(1, max_retries + 1):
            try:
                # Reset for each attempt (in case partial progress was made)
                pr_numbers = []

                # Iterate PRs lazily, sorted by updated date descending.
                # Sorting and filtering on `updated_at` ensures we discover
                # any PR whose state has changed since the cutoff, even if it
                # was opened long before. Early termination is still valid
                # because GitHub returns the list in monotonic descending
                # order of the sort key.
                async for pr in self._client.iter_pull_requests(
                    owner,
                    repo,
                    state="all",
                    sort="updated",
                    direction="desc",
                ):
                    pr_updated = pr.updated_at

                    # Date filtering - since (compares against updated_at)
                    if config.since and pr_updated < config.since:
                        logger.debug("PR #%d updated before since date, stopping", pr.number)
                        break

                    # Date filtering - until (compares against updated_at)
                    if config.until and pr_updated > config.until:
                        logger.debug("PR #%d updated after until date, skipping", pr.number)
                        continue

                    # State filtering
                    # NOTE: The list API does NOT include merge status (pr.merged is always False).
                    # We cannot filter out "abandoned" PRs here - that must happen during ingestion
                    # when we fetch the full PR details which include actual merge status.
                    is_open = pr.state == "open"

                    if config.state == "open" and not is_open:
                        continue
                    elif config.state == "merged" and is_open:
                        # Can only skip open PRs for "merged" filter; closed PRs might be merged
                        continue
                    # For state="all", include both open and closed PRs
                    # The ingestion step will determine if closed PRs are merged or abandoned

                    pr_numbers.append(pr.number)

                    # Max limit check
                    if config.max_prs and len(pr_numbers) >= config.max_prs:
                        logger.info("Reached max PR limit (%d)", config.max_prs)
                        break

                # Success - exit retry loop
                logger.info("Discovered %d PRs matching filters", len(pr_numbers))
                return pr_numbers

            except GitHubRateLimitError as e:
                if attempt == max_retries:
                    logger.error("Max retries (%d) exceeded during PR discovery", max_retries)
                    raise

                # Calculate wait time from reset_at, with a minimum of 60 seconds
                wait_time = 60.0
                if e.reset_at:
                    wait_time = max(5.0, (e.reset_at - datetime.now(UTC)).total_seconds() + 5)

                logger.warning(
                    "Rate limit hit during discovery (attempt %d/%d), waiting %.1f seconds",
                    attempt,
                    max_retries,
                    wait_time,
                )
                await asyncio.sleep(wait_time)

        # Should not reach here, but return empty list as fallback
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

        # Hoisted once so we don't re-fetch the same row 3-4 times below.
        repository = await self._repo_repository.get_by_owner_and_name(owner, repo)

        # Step 1: Discover PRs from GitHub
        discovered = await self.discover_prs(owner, repo, config)

        # Step 1b: Defensive sweep — re-fetch every PR currently OPEN in the
        # DB. Discovery alone can miss state transitions on PRs whose latest
        # GitHub `updated_at` is unchanged from when we last saw them (rare
        # but possible) or whose state changed before any header change we
        # observed. The OPEN sweep guarantees historical drift is auto-healed
        # on every sync at a bounded cost — capped to keep runaway repos
        # (thousands of stale-open PRs) from monopolizing a sync.
        if repository is not None:
            open_in_db = await self._pr_repository.get_numbers_by_state(repository.id, PRState.OPEN)
            if open_in_db:
                discovered_set = set(discovered)
                extra = [n for n in open_in_db if n not in discovered_set]
                if len(extra) > self._OPEN_SWEEP_CAP:
                    logger.warning(
                        "OPEN-PR sweep had %d candidates; capping to %d. "
                        "Run a wider --since to drain the rest.",
                        len(extra),
                        self._OPEN_SWEEP_CAP,
                    )
                    extra = extra[: self._OPEN_SWEEP_CAP]
                if extra:
                    logger.info(
                        "OPEN-PR sweep added %d previously-known open PRs to ingestion",
                        len(extra),
                    )
                    discovered = discovered + extra

        pr_numbers = discovered
        result.total_discovered = len(pr_numbers)

        if not pr_numbers:
            logger.info("No PRs to ingest for %s/%s", owner, repo)
            # Still update last_synced_at - an empty sync is still a sync
            if not config.dry_run and repository is not None:
                await self._repo_repository.update_last_synced(repository.id, datetime.now(UTC))
                if self._commit_manager:
                    await self._commit_manager.record_success()
                    await self._commit_manager.finalize()
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
            return await ingestion_service.ingest_pr(owner, repo, pr_number, dry_run=config.dry_run)

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

        # Step 5: Aggregate results and record commits
        for pr_result in batch_result.succeeded:
            if pr_result.created:
                result.created += 1
                # Record success for batch commits
                if self._commit_manager and not config.dry_run:
                    await self._commit_manager.record_success()
            elif pr_result.updated:
                result.updated += 1
                # Record success for batch commits
                if self._commit_manager and not config.dry_run:
                    await self._commit_manager.record_success()
            elif pr_result.skipped_frozen:
                result.skipped_frozen += 1
            elif pr_result.skipped_unchanged:
                result.skipped_unchanged += 1
            elif pr_result.skipped_abandoned:
                result.skipped_abandoned += 1
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

        # Persist failures if failure repository is provided
        if (
            self._failure_repository
            and result.failed_prs
            and not config.dry_run
            and repository is not None
        ):
            for pr_number, error_msg in result.failed_prs:
                if pr_number > 0:  # Skip invalid PR numbers (-1)
                    await self._failure_repository.record_failure(
                        repository.id, pr_number, Exception(error_msg)
                    )
            logger.debug(
                "Persisted %d failures for %s/%s",
                len([p for p, _ in result.failed_prs if p > 0]),
                owner,
                repo,
            )

        # Finalize any pending commits
        if self._commit_manager and not config.dry_run:
            await self._commit_manager.finalize()

        # Update last_synced_at timestamp for the repository
        if not config.dry_run and repository is not None:
            await self._repo_repository.update_last_synced(repository.id, datetime.now(UTC))
            # Ensure the timestamp is committed via commit manager
            if self._commit_manager:
                await self._commit_manager.record_success()
                await self._commit_manager.finalize()

        result.duration_seconds = time.monotonic() - start_time

        logger.info(
            "Bulk ingestion complete for %s/%s: "
            "created=%d, updated=%d, skipped_frozen=%d, skipped_unchanged=%d, "
            "skipped_abandoned=%d, failed=%d (%.1fs)",
            owner,
            repo,
            result.created,
            result.updated,
            result.skipped_frozen,
            result.skipped_unchanged,
            result.skipped_abandoned,
            result.failed,
            result.duration_seconds,
        )

        return result
