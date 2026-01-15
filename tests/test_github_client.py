"""Tests for GitHub client wrapper."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from github_activity_db.github import (
    GitHubAuthenticationError,
    GitHubClient,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from github_activity_db.schemas.github_api import (
    GitHubCommit,
    GitHubFile,
    GitHubPullRequest,
    GitHubReview,
)
from tests.fixtures.github_responses import (
    GITHUB_COMMITS_RESPONSE,
    GITHUB_FILES_RESPONSE,
    GITHUB_PR_RESPONSE,
    GITHUB_REVIEWS_RESPONSE,
)


class TestGitHubClientInit:
    """Tests for client initialization."""

    def test_init_with_token(self) -> None:
        """Client initializes with provided token."""
        client = GitHubClient(token="test-token")
        assert client._token == "test-token"

    def test_init_without_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Client raises error if no token available."""
        monkeypatch.setenv("GITHUB_TOKEN", "")
        with pytest.raises(GitHubAuthenticationError) as exc_info:
            GitHubClient()
        assert "GITHUB_TOKEN" in str(exc_info.value)


class TestGitHubClientContextManager:
    """Tests for async context manager behavior."""

    async def test_context_manager_clears_client(self) -> None:
        """Context manager properly clears client reference on exit."""
        client = GitHubClient(token="test-token")
        client._client = MagicMock()

        async with client:
            assert client._client is not None

        assert client._client is None


class TestGitHubClientRateLimit:
    """Tests for rate limit method."""

    async def test_get_rate_limit(self) -> None:
        """Rate limit returns expected data structure."""
        client = GitHubClient(token="test-token")

        # Mock the GitHub client response
        mock_response = MagicMock()
        mock_response.parsed_data.resources.core.limit = 5000
        mock_response.parsed_data.resources.core.remaining = 4999
        mock_response.parsed_data.resources.core.used = 1
        mock_response.parsed_data.resources.core.reset = 1704067200  # Jan 1, 2024

        mock_internal = MagicMock()
        mock_internal.rest.rate_limit.async_get = AsyncMock(return_value=mock_response)
        mock_internal.aclose = AsyncMock()
        client._client = mock_internal

        rate = await client.get_rate_limit()

        assert rate["limit"] == 5000
        assert rate["remaining"] == 4999
        assert rate["used"] == 1
        assert isinstance(rate["reset"], datetime)
        assert rate["reset"].tzinfo == UTC

        await client.close()


class TestGitHubClientPullRequests:
    """Tests for pull request methods."""

    async def test_get_pull_request(self) -> None:
        """Get single PR returns GitHubPullRequest schema."""
        client = GitHubClient(token="test-token")

        mock_response = MagicMock()
        mock_response.parsed_data.model_dump.return_value = GITHUB_PR_RESPONSE

        mock_internal = MagicMock()
        mock_internal.rest.pulls.async_get = AsyncMock(return_value=mock_response)
        mock_internal.aclose = AsyncMock()
        client._client = mock_internal

        pr = await client.get_pull_request("prebid", "prebid-server", 1234)

        assert isinstance(pr, GitHubPullRequest)
        assert pr.number == 1234
        assert pr.title == "Add new bidder adapter for ExampleBidder"
        assert pr.user.login == "testuser"
        assert pr.state == "open"

        await client.close()

    async def test_list_pull_requests(self) -> None:
        """List PRs returns list of GitHubPullRequest schemas."""
        client = GitHubClient(token="test-token")

        # Create mock PR data items
        mock_pr1 = MagicMock()
        mock_pr1.model_dump.return_value = GITHUB_PR_RESPONSE

        mock_pr2 = MagicMock()
        mock_pr2.model_dump.return_value = {**GITHUB_PR_RESPONSE, "number": 1235}

        async def mock_paginate(*args, **kwargs):
            """Async generator that yields mock PRs."""
            yield mock_pr1
            yield mock_pr2

        mock_internal = MagicMock()
        mock_internal.paginate = mock_paginate
        mock_internal.aclose = AsyncMock()
        client._client = mock_internal

        prs = await client.list_pull_requests("prebid", "prebid-server")

        assert len(prs) == 2
        assert all(isinstance(pr, GitHubPullRequest) for pr in prs)
        assert prs[0].number == 1234
        assert prs[1].number == 1235

        await client.close()

    async def test_get_pull_request_files(self) -> None:
        """Get PR files returns list of GitHubFile schemas."""
        client = GitHubClient(token="test-token")

        # Create mock file items
        mock_files = []
        for file_data in GITHUB_FILES_RESPONSE:
            mock_file = MagicMock()
            mock_file.model_dump.return_value = file_data
            mock_files.append(mock_file)

        async def mock_paginate(*args, **kwargs):
            for f in mock_files:
                yield f

        mock_internal = MagicMock()
        mock_internal.paginate = mock_paginate
        mock_internal.aclose = AsyncMock()
        client._client = mock_internal

        files = await client.get_pull_request_files("prebid", "prebid-server", 1234)

        assert len(files) == 3
        assert all(isinstance(f, GitHubFile) for f in files)
        assert files[0].filename == "adapters/examplebidder/examplebidder.go"
        assert files[0].status == "added"

        await client.close()

    async def test_get_pull_request_commits(self) -> None:
        """Get PR commits returns list of GitHubCommit schemas."""
        client = GitHubClient(token="test-token")

        mock_commits = []
        for commit_data in GITHUB_COMMITS_RESPONSE:
            mock_commit = MagicMock()
            mock_commit.model_dump.return_value = commit_data
            mock_commits.append(mock_commit)

        async def mock_paginate(*args, **kwargs):
            for c in mock_commits:
                yield c

        mock_internal = MagicMock()
        mock_internal.paginate = mock_paginate
        mock_internal.aclose = AsyncMock()
        client._client = mock_internal

        commits = await client.get_pull_request_commits(
            "prebid", "prebid-server", 1234
        )

        assert len(commits) == 3
        assert all(isinstance(c, GitHubCommit) for c in commits)
        assert commits[0].sha == "commit1sha"
        assert commits[0].commit.message == "Initial adapter implementation"

        await client.close()

    async def test_get_pull_request_reviews(self) -> None:
        """Get PR reviews returns list of GitHubReview schemas."""
        client = GitHubClient(token="test-token")

        mock_reviews = []
        for review_data in GITHUB_REVIEWS_RESPONSE:
            mock_review = MagicMock()
            mock_review.model_dump.return_value = review_data
            mock_reviews.append(mock_review)

        async def mock_paginate(*args, **kwargs):
            for r in mock_reviews:
                yield r

        mock_internal = MagicMock()
        mock_internal.paginate = mock_paginate
        mock_internal.aclose = AsyncMock()
        client._client = mock_internal

        reviews = await client.get_pull_request_reviews(
            "prebid", "prebid-server", 1234
        )

        assert len(reviews) == 3
        assert all(isinstance(r, GitHubReview) for r in reviews)
        assert reviews[0].user.login == "reviewer1"
        assert reviews[0].state == "CHANGES_REQUESTED"

        await client.close()


class TestGitHubClientFullPR:
    """Tests for get_full_pull_request convenience method."""

    async def test_get_full_pull_request(self) -> None:
        """Get full PR returns tuple of all data."""
        client = GitHubClient(token="test-token")

        # Mock all the individual methods
        mock_pr = GitHubPullRequest.model_validate(GITHUB_PR_RESPONSE)
        mock_files = [GitHubFile.model_validate(f) for f in GITHUB_FILES_RESPONSE]
        mock_commits = [GitHubCommit.model_validate(c) for c in GITHUB_COMMITS_RESPONSE]
        mock_reviews = [GitHubReview.model_validate(r) for r in GITHUB_REVIEWS_RESPONSE]

        with (
            patch.object(
                client, "get_pull_request", AsyncMock(return_value=mock_pr)
            ),
            patch.object(
                client, "get_pull_request_files", AsyncMock(return_value=mock_files)
            ),
            patch.object(
                client, "get_pull_request_commits", AsyncMock(return_value=mock_commits)
            ),
            patch.object(
                client, "get_pull_request_reviews", AsyncMock(return_value=mock_reviews)
            ),
        ):
            pr, files, commits, reviews = await client.get_full_pull_request(
                "prebid", "prebid-server", 1234
            )

            assert pr.number == 1234
            assert len(files) == 3
            assert len(commits) == 3
            assert len(reviews) == 3

        await client.close()


class TestGitHubClientErrorHandling:
    """Tests for error handling."""

    async def test_handle_404_error(self) -> None:
        """404 error raises GitHubNotFoundError."""
        client = GitHubClient(token="test-token")

        from githubkit.exception import RequestFailed

        # Create a mock RequestFailed exception
        mock_response = MagicMock()
        mock_response.status_code = 404
        error = RequestFailed(mock_response)

        mock_internal = MagicMock()
        mock_internal.rest.pulls.async_get = AsyncMock(side_effect=error)
        mock_internal.aclose = AsyncMock()
        client._client = mock_internal

        with pytest.raises(GitHubNotFoundError) as exc_info:
            await client.get_pull_request("prebid", "prebid-server", 99999)

        assert "99999" in str(exc_info.value)

        await client.close()

    def test_handle_401_error(self) -> None:
        """401 error maps to GitHubAuthenticationError."""
        client = GitHubClient(token="test-token")

        from githubkit.exception import RequestFailed

        mock_response = MagicMock()
        mock_response.status_code = 401
        error = RequestFailed(mock_response)

        result = client._handle_error(error)

        assert isinstance(result, GitHubAuthenticationError)

    def test_handle_403_rate_limit_error(self) -> None:
        """403 with rate limit headers maps to GitHubRateLimitError."""
        client = GitHubClient(token="test-token")

        from githubkit.exception import RequestFailed

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.headers = {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "1704067200",
        }
        error = RequestFailed(mock_response)

        result = client._handle_error(error)

        assert isinstance(result, GitHubRateLimitError)
        assert result.reset_at is not None
        assert result.reset_at.tzinfo == UTC

    def test_handle_generic_error(self) -> None:
        """Other errors map to generic GitHubClientError."""
        client = GitHubClient(token="test-token")

        from githubkit.exception import RequestFailed

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        error = RequestFailed(mock_response)

        result = client._handle_error(error)

        assert isinstance(result, GitHubClientError)
        assert "500" in str(result)
