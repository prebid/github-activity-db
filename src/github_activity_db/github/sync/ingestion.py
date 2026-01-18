"""PR Ingestion Service - Fetch → Transform → Store pipeline.

Orchestrates the process of fetching PR data from GitHub API,
transforming it through our schema hierarchy, and persisting
to the database.
"""

from github_activity_db.db.repositories import PullRequestRepository, RepositoryRepository
from github_activity_db.github.client import GitHubClient
from github_activity_db.github.exceptions import GitHubRetryableError
from github_activity_db.logging import bind_pr, get_logger
from github_activity_db.schemas import PRMerge

from .results import PRIngestionResult

logger = get_logger(__name__)


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
        # Bind PR context for all logs in this method
        pr_logger = bind_pr(owner, repo, pr_number)

        try:
            # Step 1: Ensure repository exists
            repository, repo_created = await self._repo_repository.get_or_create(owner, repo)
            if repo_created:
                pr_logger.info("Created repository record", repo_id=repository.id)

            # Step 2: Fetch full PR data from GitHub API
            pr_logger.debug("Fetching PR data from GitHub")
            gh_pr, files, commits, reviews = await self._client.get_full_pull_request(
                owner, repo, pr_number
            )

            # Step 2.5: Skip abandoned PRs (closed but not merged)
            # NOTE: We check this here because the list API does NOT include merge status.
            # Only the full PR endpoint tells us if a closed PR was actually merged.
            if gh_pr.state == "closed" and not gh_pr.merged:
                pr_logger.debug("PR is abandoned (closed without merge), skipping")
                # Check if we have an existing record to return
                existing = await self._pr_repository.get_by_number(repository.id, pr_number)
                return PRIngestionResult.from_skipped_abandoned(existing)

            # Step 3: Transform to internal schemas
            pr_create = gh_pr.to_pr_create(repository.id)
            pr_sync = gh_pr.to_pr_sync(files, commits, reviews)

            # Step 4: Check if existing PR and if unchanged
            existing = await self._pr_repository.get_by_number(repository.id, pr_number)

            if existing is not None:
                # Check if frozen (merged past grace period)
                if self._pr_repository._is_frozen(existing):
                    pr_logger.debug("PR is frozen, skipping update")
                    return PRIngestionResult.from_skipped_frozen(existing)

                # Check if unchanged (diff detection)
                if self._pr_repository.is_unchanged(existing, pr_sync):
                    pr_logger.debug("PR unchanged, skipping update")
                    return PRIngestionResult.from_skipped_unchanged(existing)

            # Step 5: Dry run check
            if dry_run:
                action = "create" if existing is None else "update"
                pr_logger.info("Dry run: would {action} PR", action=action)
                # Return what would happen without writing
                if existing is None:
                    return PRIngestionResult(pr=None, created=True)
                return PRIngestionResult(pr=existing, updated=True)

            # Step 6: Store via repository
            pr, created = await self._pr_repository.create_or_update(
                repository.id, pr_create, pr_sync
            )

            # Step 7: Handle merge if applicable
            # Apply merge data if GitHub says it's merged and we don't have merge fields yet
            if gh_pr.merged and pr.merged_by is None:
                # A merged PR always has merged_at set by GitHub
                assert gh_pr.merged_at is not None, "Merged PR must have merged_at timestamp"
                merge_data = PRMerge(
                    close_date=gh_pr.merged_at,
                    merged_by=gh_pr.merged_by.login if gh_pr.merged_by else None,
                )
                updated_pr = await self._pr_repository.apply_merge(pr.id, merge_data)
                # PR exists since we just created/updated it
                assert updated_pr is not None, "apply_merge should return PR for existing ID"
                pr = updated_pr
                pr_logger.info("Applied merge data")

            # Return appropriate result
            if created:
                pr_logger.info("Created PR", title=pr.title[:50])
                return PRIngestionResult.from_created(pr)
            else:
                pr_logger.info("Updated PR", title=pr.title[:50])
                return PRIngestionResult.from_updated(pr)

        except GitHubRetryableError:
            # Re-raise retryable errors so the scheduler can retry with proper backoff
            pr_logger.warning("Retryable error during PR ingestion, will retry")
            raise
        except Exception as e:
            pr_logger.error("Failed to ingest PR", error=str(e))
            return PRIngestionResult.from_error(e)
