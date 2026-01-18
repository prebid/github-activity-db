"""Multi-Repository Sync Orchestrator - Sync all tracked repositories.

Coordinates bulk PR ingestion across multiple repositories using the
existing BulkPRIngestionService for per-repository processing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from github_activity_db.config import get_settings
from github_activity_db.db.repositories import (
    PullRequestRepository,
    RepositoryRepository,
    SyncFailureRepository,
)
from github_activity_db.logging import get_logger

from .bulk_ingestion import BulkIngestionConfig, BulkIngestionResult, BulkPRIngestionService

if TYPE_CHECKING:
    from github_activity_db.github.client import GitHubClient
    from github_activity_db.github.pacing import RequestScheduler

    from .commit_manager import CommitManager

logger = get_logger(__name__)


@dataclass
class RepoSyncResult:
    """Result of syncing a single repository.

    Wraps BulkIngestionResult with repository context and timing.
    """

    repository: str
    """Full repository name (owner/repo)."""

    result: BulkIngestionResult
    """Bulk ingestion result for this repository."""

    started_at: datetime
    """When sync started for this repository."""

    completed_at: datetime
    """When sync completed for this repository."""

    @property
    def duration_seconds(self) -> float:
        """Time taken to sync this repository."""
        return (self.completed_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        # Spread bulk result first, then override with repo-specific values
        return {
            **self.result.to_dict(),
            "repository": self.repository,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
        }


@dataclass
class MultiRepoSyncResult:
    """Result of syncing multiple repositories.

    Aggregates results from all repository syncs.
    """

    repo_results: list[RepoSyncResult] = field(default_factory=list)
    """Results for each repository."""

    total_discovered: int = 0
    """Total PRs discovered across all repositories."""

    total_created: int = 0
    """Total PRs created across all repositories."""

    total_updated: int = 0
    """Total PRs updated across all repositories."""

    total_skipped: int = 0
    """Total PRs skipped (frozen + unchanged) across all repositories."""

    total_failed: int = 0
    """Total PRs that failed across all repositories."""

    duration_seconds: float = 0.0
    """Total time taken for all syncs."""

    @property
    def repos_succeeded(self) -> int:
        """Number of repositories that synced without errors."""
        return sum(1 for r in self.repo_results if r.result.failed == 0)

    @property
    def repos_with_failures(self) -> int:
        """Number of repositories that had at least one failure."""
        return sum(1 for r in self.repo_results if r.result.failed > 0)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "summary": {
                "total_repos": len(self.repo_results),
                "repos_succeeded": self.repos_succeeded,
                "repos_with_failures": self.repos_with_failures,
                "total_discovered": self.total_discovered,
                "total_created": self.total_created,
                "total_updated": self.total_updated,
                "total_skipped": self.total_skipped,
                "total_failed": self.total_failed,
                "duration_seconds": round(self.duration_seconds, 2),
            },
            "repositories": [r.to_dict() for r in self.repo_results],
        }


class MultiRepoOrchestrator:
    """Orchestrates syncing of multiple GitHub repositories.

    Coordinates bulk PR ingestion across all tracked repositories using
    the existing BulkPRIngestionService for per-repository processing.

    Usage:
        async with GitHubClient() as client:
            async with get_session() as session:
                scheduler = RequestScheduler(pacer)
                await scheduler.start()

                orchestrator = MultiRepoOrchestrator(
                    client=client,
                    repo_repository=RepositoryRepository(session),
                    pr_repository=PullRequestRepository(session),
                    scheduler=scheduler,
                )

                config = BulkIngestionConfig(since=datetime(2024, 10, 1))
                result = await orchestrator.sync_all(config)

                await scheduler.shutdown()
    """

    def __init__(
        self,
        client: GitHubClient,
        repo_repository: RepositoryRepository,
        pr_repository: PullRequestRepository,
        scheduler: RequestScheduler,
        failure_repository: SyncFailureRepository | None = None,
        commit_manager: CommitManager | None = None,
    ) -> None:
        """Initialize the multi-repo orchestrator.

        Args:
            client: GitHub API client
            repo_repository: Repository for Repository model
            pr_repository: Repository for PullRequest model
            scheduler: RequestScheduler for rate-limited execution
            failure_repository: Optional repository for tracking sync failures
            commit_manager: Optional CommitManager for batch commits.
                            When provided, commits are made per repository
                            and in batches within each repository.
        """
        self._client = client
        self._repo_repository = repo_repository
        self._pr_repository = pr_repository
        self._scheduler = scheduler
        self._failure_repository = failure_repository
        self._commit_manager = commit_manager
        self._settings = get_settings()

    async def initialize_repositories(
        self,
        repos: list[str] | None = None,
    ) -> list[str]:
        """Ensure all repositories exist in the database.

        Creates Repository records for any repos that don't exist yet.

        Args:
            repos: List of repos to initialize (owner/repo format).
                   If None, uses Settings.tracked_repos.

        Returns:
            List of repository full names that were initialized.
        """
        repo_list = repos if repos is not None else self._settings.tracked_repos
        initialized: list[str] = []

        for full_name in repo_list:
            owner, name = full_name.split("/", 1)
            _repo, created = await self._repo_repository.get_or_create(
                owner=owner,
                name=name,
            )
            if created:
                logger.info("Created repository record: %s", full_name)
            initialized.append(full_name)

        return initialized

    async def sync_all(
        self,
        config: BulkIngestionConfig,
        repos: list[str] | None = None,
    ) -> MultiRepoSyncResult:
        """Sync all tracked repositories.

        Processes each repository sequentially using BulkPRIngestionService.
        All PRs within a repository are processed concurrently per the config.

        Args:
            config: Bulk ingestion configuration (applies to all repos)
            repos: List of repos to sync (owner/repo format).
                   If None, uses Settings.tracked_repos.

        Returns:
            MultiRepoSyncResult with aggregated statistics.
        """
        start_time = time.monotonic()
        result = MultiRepoSyncResult()

        # Get repo list
        repo_list = repos if repos is not None else self._settings.tracked_repos

        # Ensure all repos exist in DB
        await self.initialize_repositories(repo_list)

        # Create bulk ingestion service
        bulk_service = BulkPRIngestionService(
            client=self._client,
            repo_repository=self._repo_repository,
            pr_repository=self._pr_repository,
            scheduler=self._scheduler,
            failure_repository=self._failure_repository,
            commit_manager=self._commit_manager,
        )

        # Sync each repository
        for full_name in repo_list:
            owner, name = full_name.split("/", 1)
            repo_start = datetime.now()

            logger.info("Starting sync for %s", full_name)

            try:
                bulk_result = await bulk_service.ingest_repository(owner, name, config)

                repo_result = RepoSyncResult(
                    repository=full_name,
                    result=bulk_result,
                    started_at=repo_start,
                    completed_at=datetime.now(),
                )
                result.repo_results.append(repo_result)

                # Aggregate totals
                result.total_discovered += bulk_result.total_discovered
                result.total_created += bulk_result.created
                result.total_updated += bulk_result.updated
                result.total_skipped += bulk_result.total_skipped
                result.total_failed += bulk_result.failed

                logger.info(
                    "Completed sync for %s: created=%d, updated=%d, skipped=%d, failed=%d",
                    full_name,
                    bulk_result.created,
                    bulk_result.updated,
                    bulk_result.total_skipped,
                    bulk_result.failed,
                )

            except Exception as e:
                # Log error but continue with other repos
                logger.exception("Failed to sync %s: %s", full_name, e)
                # Create a failed result
                failed_result = BulkIngestionResult(failed=1)
                failed_result.failed_prs.append((-1, f"Repository sync failed: {e}"))
                repo_result = RepoSyncResult(
                    repository=full_name,
                    result=failed_result,
                    started_at=repo_start,
                    completed_at=datetime.now(),
                )
                result.repo_results.append(repo_result)
                result.total_failed += 1

        result.duration_seconds = time.monotonic() - start_time

        logger.info(
            "Multi-repo sync complete: repos=%d, discovered=%d, "
            "created=%d, updated=%d, skipped=%d, failed=%d (%.1fs)",
            len(result.repo_results),
            result.total_discovered,
            result.total_created,
            result.total_updated,
            result.total_skipped,
            result.total_failed,
            result.duration_seconds,
        )

        return result
