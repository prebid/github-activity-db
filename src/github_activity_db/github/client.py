"""Async GitHub API client wrapper using githubkit.

This module provides a typed async interface to the GitHub REST API
for pull request data retrieval with integrated rate limit monitoring.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from githubkit import GitHub
from githubkit.exception import RequestFailed
from pydantic import ValidationError

from github_activity_db.config import get_settings
from github_activity_db.logging import get_logger
from github_activity_db.schemas.github_api import (
    GitHubCommit,
    GitHubFile,
    GitHubPullRequest,
    GitHubReview,
)

from .exceptions import (
    GitHubAuthenticationError,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from .rate_limit.schemas import RateLimitPool

if TYPE_CHECKING:
    from .pacing.pacer import RequestPacer
    from .rate_limit.monitor import RateLimitMonitor

logger = get_logger(__name__)

PRState = Literal["open", "closed", "all"]


class GitHubClient:
    """Async GitHub API client for PR data retrieval.

    Usage:
        async with GitHubClient() as client:
            prs = await client.list_pull_requests("prebid", "prebid-server")
            for pr in prs:
                print(pr.title)

    Or without context manager:
        client = GitHubClient()
        prs = await client.list_pull_requests("prebid", "prebid-server")
        await client.close()
    """

    def __init__(
        self,
        token: str | None = None,
        rate_monitor: RateLimitMonitor | None = None,
        pacer: RequestPacer | None = None,
    ) -> None:
        """Initialize the GitHub client.

        Args:
            token: GitHub PAT. If not provided, uses GITHUB_TOKEN from settings.
            rate_monitor: Optional RateLimitMonitor for tracking rate limits.
                         When provided, response headers are automatically
                         used to update the monitor's state.
            pacer: Optional RequestPacer for rate limit pacing. When provided,
                   the client will automatically apply delays before each API
                   call based on current rate limit state.

        Raises:
            GitHubAuthenticationError: If no token is available.
        """
        self._token = token or get_settings().github_token
        if not self._token:
            raise GitHubAuthenticationError(
                "GitHub token required. Set GITHUB_TOKEN environment variable."
            )
        self._client: GitHub[Any] | None = None
        self._rate_monitor = rate_monitor
        self._pacer = pacer

    @property
    def _github(self) -> GitHub[Any]:
        """Get or create the githubkit client instance."""
        if self._client is None:
            self._client = GitHub(self._token)
        return self._client

    @property
    def rate_monitor(self) -> RateLimitMonitor | None:
        """Access the rate limit monitor (if configured)."""
        return self._rate_monitor

    @property
    def pacer(self) -> RequestPacer | None:
        """Access the request pacer (if configured)."""
        return self._pacer

    async def _apply_pacing(self, pool: RateLimitPool = RateLimitPool.CORE) -> None:
        """Apply rate limit pacing before making a request.

        If a pacer is configured, this method calculates the recommended
        delay based on current rate limit state and waits before proceeding.

        Args:
            pool: The rate limit pool to check (default: CORE)
        """
        if self._pacer is None:
            return

        delay = self._pacer.get_recommended_delay(pool)
        if delay > 0:
            logger.debug("Pacing: waiting %.2fs before request", delay)
            await asyncio.sleep(delay)
        self._pacer.on_request_start()

    def _update_rate_limit_from_response(self, response: Any) -> None:
        """Extract rate limit headers from response and update monitor/pacer.

        Args:
            response: A githubkit Response object with headers attribute.
        """
        if self._rate_monitor is None and self._pacer is None:
            return

        try:
            # githubkit Response objects have a .headers attribute
            headers = getattr(response, "headers", None)
            if headers is None:
                return

            # Convert headers to dict if needed (httpx.Headers → dict)
            if hasattr(headers, "items"):
                header_dict = dict(headers.items())
            else:
                header_dict = dict(headers)

            # Update monitor with rate limit headers
            if self._rate_monitor is not None:
                self._rate_monitor.update_from_headers(header_dict)

            # Notify pacer of request completion with headers
            # This allows the pacer to update its state for next delay calculation
            if self._pacer is not None:
                self._pacer.on_request_complete(header_dict)
        except Exception as e:
            # Don't let rate limit tracking failures break API calls
            logger.debug("Failed to update rate limit from headers: %s", e)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            self._client = None

    async def __aenter__(self) -> GitHubClient:
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit."""
        await self.close()

    # -------------------------------------------------------------------------
    # Rate Limit Info
    # -------------------------------------------------------------------------
    async def get_rate_limit(self) -> dict[str, int | datetime]:
        """Get current rate limit status.

        Returns:
            Dict with 'limit', 'remaining', 'reset' (datetime), 'used' keys.
        """
        await self._apply_pacing()
        resp = await self._github.rest.rate_limit.async_get()
        self._update_rate_limit_from_response(resp)
        core = resp.parsed_data.resources.core
        return {
            "limit": core.limit,
            "remaining": core.remaining,
            "used": core.used,
            "reset": datetime.fromtimestamp(core.reset, tz=UTC),
        }

    # -------------------------------------------------------------------------
    # Pull Request Methods
    # -------------------------------------------------------------------------
    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        state: PRState = "open",
        sort: Literal["created", "updated", "popularity", "long-running"] = "created",
        direction: Literal["asc", "desc"] = "desc",
        per_page: int = 100,
    ) -> list[GitHubPullRequest]:
        """List pull requests for a repository.

        Note: This endpoint returns partial PR data. For full details
        (additions, deletions, changed_files), use get_pull_request().

        Args:
            owner: Repository owner (org or user)
            repo: Repository name
            state: Filter by state ("open", "closed", "all")
            sort: What to sort results by ("created", "updated", "popularity", "long-running")
            direction: Sort direction ("asc", "desc")
            per_page: Results per page (max 100)

        Returns:
            List of GitHubPullRequest objects (partial data - stats may be 0)
        """
        try:
            await self._apply_pacing()
            prs: list[GitHubPullRequest] = []

            pr_data: Any
            async for pr_data in self._github.paginate(
                self._github.rest.pulls.async_list,
                owner=owner,
                repo=repo,
                state=state,
                sort=sort,
                direction=direction,
                per_page=per_page,
            ):
                try:
                    prs.append(GitHubPullRequest.model_validate(pr_data.model_dump()))
                except ValidationError:
                    # Skip PRs that don't validate (shouldn't happen normally)
                    continue

            return prs
        except RequestFailed as e:
            raise self._handle_error(e) from e

    async def iter_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        state: PRState = "open",
        sort: Literal["created", "updated", "popularity", "long-running"] = "created",
        direction: Literal["asc", "desc"] = "desc",
        per_page: int = 100,
    ) -> AsyncIterator[GitHubPullRequest]:
        """Iterate over pull requests lazily (for efficient early termination).

        Unlike list_pull_requests(), this yields PRs one at a time and only
        fetches new pages as needed. Use this when you might break early
        (e.g., date filtering with --since).

        Args:
            owner: Repository owner (org or user)
            repo: Repository name
            state: Filter by state ("open", "closed", "all")
            sort: What to sort results by ("created", "updated", "popularity", "long-running")
            direction: Sort direction ("asc", "desc")
            per_page: Results per page (max 100)

        Yields:
            GitHubPullRequest objects (partial data - stats may be 0)
        """
        try:
            await self._apply_pacing()
            pr_data: Any
            async for pr_data in self._github.paginate(
                self._github.rest.pulls.async_list,
                owner=owner,
                repo=repo,
                state=state,
                sort=sort,
                direction=direction,
                per_page=per_page,
            ):
                try:
                    yield GitHubPullRequest.model_validate(pr_data.model_dump())
                except ValidationError:
                    # Skip PRs that don't validate (shouldn't happen normally)
                    continue
        except RequestFailed as e:
            raise self._handle_error(e) from e

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> GitHubPullRequest:
        """Get full details for a single pull request.

        This endpoint returns complete PR data including stats
        (additions, deletions, changed_files).

        Args:
            owner: Repository owner
            repo: Repository name
            number: PR number

        Returns:
            GitHubPullRequest with full details

        Raises:
            GitHubNotFoundError: If PR doesn't exist
        """
        try:
            await self._apply_pacing()
            resp = await self._github.rest.pulls.async_get(
                owner=owner,
                repo=repo,
                pull_number=number,
            )
            self._update_rate_limit_from_response(resp)
            return GitHubPullRequest.model_validate(resp.parsed_data.model_dump())
        except RequestFailed as e:
            if e.response.status_code == 404:
                raise GitHubNotFoundError(f"PR #{number} not found in {owner}/{repo}") from e
            raise self._handle_error(e) from e

    async def get_pull_request_files(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubFile]:
        """Get files changed in a pull request.

        Args:
            owner: Repository owner
            repo: Repository name
            number: PR number
            per_page: Results per page (max 100)

        Returns:
            List of GitHubFile objects
        """
        try:
            await self._apply_pacing()
            files: list[GitHubFile] = []

            file_data: Any
            async for file_data in self._github.paginate(
                self._github.rest.pulls.async_list_files,
                owner=owner,
                repo=repo,
                pull_number=number,
                per_page=per_page,
            ):
                try:
                    files.append(GitHubFile.model_validate(file_data.model_dump()))
                except ValidationError:
                    continue

            return files
        except RequestFailed as e:
            if e.response.status_code == 404:
                raise GitHubNotFoundError(f"PR #{number} not found in {owner}/{repo}") from e
            raise self._handle_error(e) from e

    async def get_pull_request_commits(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubCommit]:
        """Get commits in a pull request.

        Args:
            owner: Repository owner
            repo: Repository name
            number: PR number
            per_page: Results per page (max 100)

        Returns:
            List of GitHubCommit objects
        """
        try:
            await self._apply_pacing()
            commits: list[GitHubCommit] = []

            commit_data: Any
            async for commit_data in self._github.paginate(
                self._github.rest.pulls.async_list_commits,
                owner=owner,
                repo=repo,
                pull_number=number,
                per_page=per_page,
            ):
                try:
                    commits.append(GitHubCommit.model_validate(commit_data.model_dump()))
                except ValidationError:
                    continue

            return commits
        except RequestFailed as e:
            if e.response.status_code == 404:
                raise GitHubNotFoundError(f"PR #{number} not found in {owner}/{repo}") from e
            raise self._handle_error(e) from e

    async def get_pull_request_reviews(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubReview]:
        """Get reviews for a pull request.

        Args:
            owner: Repository owner
            repo: Repository name
            number: PR number
            per_page: Results per page (max 100)

        Returns:
            List of GitHubReview objects
        """
        try:
            await self._apply_pacing()
            reviews: list[GitHubReview] = []

            review_data: Any
            async for review_data in self._github.paginate(
                self._github.rest.pulls.async_list_reviews,
                owner=owner,
                repo=repo,
                pull_number=number,
                per_page=per_page,
            ):
                try:
                    reviews.append(GitHubReview.model_validate(review_data.model_dump()))
                except ValidationError:
                    continue

            return reviews
        except RequestFailed as e:
            if e.response.status_code == 404:
                raise GitHubNotFoundError(f"PR #{number} not found in {owner}/{repo}") from e
            raise self._handle_error(e) from e

    # -------------------------------------------------------------------------
    # Convenience Methods
    # -------------------------------------------------------------------------
    async def get_full_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> tuple[GitHubPullRequest, list[GitHubFile], list[GitHubCommit], list[GitHubReview]]:
        """Get complete PR data including files, commits, and reviews.

        This makes 4 API calls. Use when you need all PR data for sync.

        Args:
            owner: Repository owner
            repo: Repository name
            number: PR number

        Returns:
            Tuple of (pr, files, commits, reviews)
        """
        pr = await self.get_pull_request(owner, repo, number)
        files = await self.get_pull_request_files(owner, repo, number)
        commits = await self.get_pull_request_commits(owner, repo, number)
        reviews = await self.get_pull_request_reviews(owner, repo, number)
        return pr, files, commits, reviews

    # -------------------------------------------------------------------------
    # Error Handling
    # -------------------------------------------------------------------------
    def _handle_error(self, error: RequestFailed) -> GitHubClientError:
        """Convert githubkit exceptions to our custom exceptions."""
        # Update rate limit from error response headers (they still count)
        self._update_rate_limit_from_response(error.response)

        status = error.response.status_code

        if status == 401:
            return GitHubAuthenticationError("Invalid GitHub token")
        elif status == 403:
            # Check for rate limit
            headers = error.response.headers
            if "x-ratelimit-remaining" in headers:
                remaining = int(headers.get("x-ratelimit-remaining", "0"))
                if remaining == 0:
                    reset_ts = int(headers.get("x-ratelimit-reset", "0"))
                    reset_at = datetime.fromtimestamp(reset_ts, tz=UTC) if reset_ts else None
                    return GitHubRateLimitError(
                        "GitHub rate limit exceeded",
                        reset_at=reset_at,
                    )
            return GitHubClientError(f"Access forbidden: {error}")
        elif status == 404:
            return GitHubNotFoundError(str(error))
        else:
            return GitHubClientError(f"GitHub API error ({status}): {error}")
