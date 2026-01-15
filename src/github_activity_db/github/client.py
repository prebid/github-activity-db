"""Async GitHub API client wrapper using githubkit.

This module provides a typed async interface to the GitHub REST API
for pull request data retrieval.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from githubkit import GitHub
from githubkit.exception import RequestFailed
from pydantic import ValidationError

from github_activity_db.config import get_settings
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

    def __init__(self, token: str | None = None) -> None:
        """Initialize the GitHub client.

        Args:
            token: GitHub PAT. If not provided, uses GITHUB_TOKEN from settings.

        Raises:
            GitHubAuthenticationError: If no token is available.
        """
        self._token = token or get_settings().github_token
        if not self._token:
            raise GitHubAuthenticationError(
                "GitHub token required. Set GITHUB_TOKEN environment variable."
            )
        self._client: Any = None

    @property
    def _github(self) -> Any:
        """Get or create the githubkit client instance."""
        if self._client is None:
            self._client = GitHub(self._token)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            self._client = None

    async def __aenter__(self) -> "GitHubClient":
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
        resp = await self._github.rest.rate_limit.async_get()
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
        per_page: int = 100,
    ) -> list[GitHubPullRequest]:
        """List pull requests for a repository.

        Note: This endpoint returns partial PR data. For full details
        (additions, deletions, changed_files), use get_pull_request().

        Args:
            owner: Repository owner (org or user)
            repo: Repository name
            state: Filter by state ("open", "closed", "all")
            per_page: Results per page (max 100)

        Returns:
            List of GitHubPullRequest objects (partial data - stats may be 0)
        """
        try:
            prs: list[GitHubPullRequest] = []

            async for pr_data in self._github.paginate(
                self._github.rest.pulls.async_list,
                owner=owner,
                repo=repo,
                state=state,
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
            resp = await self._github.rest.pulls.async_get(
                owner=owner,
                repo=repo,
                pull_number=number,
            )
            return GitHubPullRequest.model_validate(resp.parsed_data.model_dump())
        except RequestFailed as e:
            if e.response.status_code == 404:
                raise GitHubNotFoundError(
                    f"PR #{number} not found in {owner}/{repo}"
                ) from e
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
            files: list[GitHubFile] = []

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
                raise GitHubNotFoundError(
                    f"PR #{number} not found in {owner}/{repo}"
                ) from e
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
            commits: list[GitHubCommit] = []

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
                raise GitHubNotFoundError(
                    f"PR #{number} not found in {owner}/{repo}"
                ) from e
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
            reviews: list[GitHubReview] = []

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
                raise GitHubNotFoundError(
                    f"PR #{number} not found in {owner}/{repo}"
                ) from e
            raise self._handle_error(e) from e

    # -------------------------------------------------------------------------
    # Convenience Methods
    # -------------------------------------------------------------------------
    async def get_full_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> tuple[
        GitHubPullRequest, list[GitHubFile], list[GitHubCommit], list[GitHubReview]
    ]:
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
                    reset_at = (
                        datetime.fromtimestamp(reset_ts, tz=UTC) if reset_ts else None
                    )
                    return GitHubRateLimitError(
                        "GitHub rate limit exceeded",
                        reset_at=reset_at,
                    )
            return GitHubClientError(f"Access forbidden: {error}")
        elif status == 404:
            return GitHubNotFoundError(str(error))
        else:
            return GitHubClientError(f"GitHub API error ({status}): {error}")
