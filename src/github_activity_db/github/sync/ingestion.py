"""PR Ingestion Service - Fetch → Transform → Store pipeline.

Orchestrates the process of fetching PR data from GitHub API,
transforming it through our schema hierarchy, and persisting
to the database.
"""

import logging

from github_activity_db.db.repositories import PullRequestRepository, RepositoryRepository
from github_activity_db.github.client import GitHubClient
from github_activity_db.schemas import PRMerge

from .results import PRIngestionResult

logger = logging.getLogger(__name__)


class PRIngestionService:
    """Service for ingesting PRs from GitHub to database.

    Coordinates fetching from GitHub API, transforming through schemas,
    and storing via repositories. Handles the full lifecycle including
    merge detection and frozen PR handling.

    Usage:
        async with GitHubClient() as client:
            async with get_session() as session:
                service = PRIngestionService(
                    client=client,
                    repo_repository=RepositoryRepository(session),
                    pr_repository=PullRequestRepository(session),
                )

                result = await service.ingest_pr("prebid", "prebid-server", 123)
                if result.success:
                    print(f"{result.action}: PR #{result.pr.number}")
    """

    def __init__(
        self,
        client: GitHubClient,
        repo_repository: RepositoryRepository,
        pr_repository: PullRequestRepository,
    ) -> None:
        """Initialize the ingestion service.

        Args:
            client: GitHub API client
            repo_repository: Repository for Repository model
            pr_repository: Repository for PullRequest model
        """
        self._client = client
        self._repo_repository = repo_repository
        self._pr_repository = pr_repository

    async def ingest_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        dry_run: bool = False,
    ) -> PRIngestionResult:
        """Fetch single PR from GitHub and store in database.

        Flow:
            1. Ensure repository record exists (get_or_create)
            2. Fetch full PR data from GitHub API
            3. Transform to internal schemas (PRCreate, PRSync)
            4. Check if update needed (diff detection via last_update_date)
            5. Check if frozen (merged > grace_period ago)
            6. Store via repository (create_or_update) unless dry_run
            7. If merged and within grace period, apply merge data

        Args:
            owner: Repository owner
            repo: Repository name
            pr_number: PR number to ingest
            dry_run: If True, don't write to database

        Returns:
            PRIngestionResult with operation details

        Note:
            Errors are captured in result.error, not raised.
        """
        try:
            # Step 1: Ensure repository exists
            repository, repo_created = await self._repo_repository.get_or_create(
                owner, repo
            )
            if repo_created:
                logger.info("Created repository: %s/%s (id=%d)", owner, repo, repository.id)

            # Step 2: Fetch full PR data from GitHub API
            logger.debug("Fetching PR #%d from %s/%s", pr_number, owner, repo)
            gh_pr, files, commits, reviews = await self._client.get_full_pull_request(
                owner, repo, pr_number
            )

            # Step 3: Transform to internal schemas
            pr_create = gh_pr.to_pr_create(repository.id)
            pr_sync = gh_pr.to_pr_sync(files, commits, reviews)

            # Step 4: Check if existing PR and if unchanged
            existing = await self._pr_repository.get_by_number(repository.id, pr_number)

            if existing is not None:
                # Check if frozen (merged past grace period)
                if self._pr_repository._is_frozen(existing):
                    logger.debug("PR #%d is frozen, skipping update", pr_number)
                    return PRIngestionResult.from_skipped_frozen(existing)

                # Check if unchanged (diff detection)
                if self._pr_repository.is_unchanged(existing, pr_sync):
                    logger.debug("PR #%d unchanged, skipping update", pr_number)
                    return PRIngestionResult.from_skipped_unchanged(existing)

            # Step 5: Dry run check
            if dry_run:
                logger.info("Dry run: would %s PR #%d",
                           "create" if existing is None else "update", pr_number)
                # Return what would happen without writing
                if existing is None:
                    return PRIngestionResult(
                        pr=None, created=True
                    )
                return PRIngestionResult(
                    pr=existing, updated=True
                )

            # Step 6: Store via repository
            pr, created = await self._pr_repository.create_or_update(
                repository.id, pr_create, pr_sync
            )

            # Step 7: Handle merge if applicable
            # Apply merge data if GitHub says it's merged and we don't have merge fields yet
            if gh_pr.merged and pr.merged_by is None:
                merge_data = PRMerge(
                    close_date=gh_pr.merged_at or gh_pr.closed_at,  # type: ignore[arg-type]
                    merged_by=gh_pr.merged_by.login if gh_pr.merged_by else None,
                )
                pr = await self._pr_repository.apply_merge(pr.id, merge_data)  # type: ignore[assignment]
                logger.info("Applied merge data to PR #%d", pr_number)

            # Return appropriate result
            if created:
                logger.info("Created PR #%d: %s", pr_number, pr.title[:50])
                return PRIngestionResult.from_created(pr)
            else:
                logger.info("Updated PR #%d: %s", pr_number, pr.title[:50])
                return PRIngestionResult.from_updated(pr)

        except Exception as e:
            logger.error("Failed to ingest PR #%d from %s/%s: %s",
                        pr_number, owner, repo, e)
            return PRIngestionResult.from_error(e)
