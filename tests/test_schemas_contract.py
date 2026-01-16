"""Contract tests for GitHub API schemas against real PR data.

These tests validate that our Pydantic schemas correctly parse
real GitHub API responses captured from prebid/prebid-server.
"""


from github_activity_db.db.models import PRState
from github_activity_db.schemas import (
    GitHubCommit,
    GitHubFile,
    GitHubPullRequest,
    GitHubReview,
)

from .fixtures import (
    MERGED_PR_METADATA,
    OPEN_PR_METADATA,
    REAL_MERGED_PR,
    REAL_OPEN_PR,
)


class TestGitHubPRSchemaContract:
    """Contract tests for GitHubPullRequest schema parsing."""

    def test_parse_open_pr(self) -> None:
        """Parse real open PR response."""
        pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])

        assert pr.number == 4663
        assert pr.state == "open"
        assert pr.merged is False
        assert pr.merged_at is None
        assert pr.merged_by is None
        assert pr.user.login == "dev-adverxo"

    def test_parse_merged_pr(self) -> None:
        """Parse real merged PR response."""
        pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])

        assert pr.number == 4646
        assert pr.state == "closed"  # GitHub API returns "closed" for merged
        assert pr.merged is True
        assert pr.merged_at is not None
        assert pr.merged_by is not None
        assert pr.merged_by.login == MERGED_PR_METADATA["expected_merged_by"]

    def test_open_pr_dates(self) -> None:
        """Open PR has created_at and updated_at but no closed_at."""
        pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])

        assert pr.created_at is not None
        assert pr.updated_at is not None
        assert pr.closed_at is None
        assert pr.merged_at is None

    def test_merged_pr_dates(self) -> None:
        """Merged PR has all date fields populated."""
        pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])

        assert pr.created_at is not None
        assert pr.updated_at is not None
        assert pr.closed_at is not None
        assert pr.merged_at is not None
        # closed_at and merged_at should be the same for merged PRs
        assert pr.closed_at == pr.merged_at

    def test_pr_stats_populated(self) -> None:
        """PR stats (additions, deletions, changed_files) are populated."""
        open_pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])
        merged_pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])

        # Open PR stats
        assert open_pr.additions == 9
        assert open_pr.deletions == 0
        assert open_pr.changed_files == 1
        assert open_pr.commits == 1

        # Merged PR stats
        assert merged_pr.additions == 1
        assert merged_pr.deletions == 1
        assert merged_pr.changed_files == 1
        assert merged_pr.commits == 12

    def test_pr_with_labels(self) -> None:
        """Merged PR has labels."""
        pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])

        assert len(pr.labels) == 1
        assert pr.labels[0].name == "adapter"
        assert pr.labels[0].color == "BAF1E0"

    def test_pr_without_labels(self) -> None:
        """Open PR has no labels."""
        pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])

        assert pr.labels == []

    def test_pr_with_assignees(self) -> None:
        """Merged PR has assignees."""
        pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])

        assert len(pr.assignees) == 2
        assignee_logins = [a.login for a in pr.assignees]
        assert "bsardo" in assignee_logins
        assert "ccorbo" in assignee_logins


class TestGitHubFileSchemaContract:
    """Contract tests for GitHubFile schema parsing."""

    def test_parse_files_open_pr(self) -> None:
        """Parse files from open PR."""
        files = [GitHubFile(**f) for f in REAL_OPEN_PR["files"]]

        assert len(files) == OPEN_PR_METADATA["expected_file_count"]
        assert files[0].filename == "static/bidder-info/alchemyx.yaml"
        assert files[0].status == "added"
        assert files[0].additions == 9
        assert files[0].deletions == 0

    def test_parse_files_merged_pr(self) -> None:
        """Parse files from merged PR."""
        files = [GitHubFile(**f) for f in REAL_MERGED_PR["files"]]

        assert len(files) == MERGED_PR_METADATA["expected_file_count"]
        assert files[0].filename == "static/bidder-info/optidigital.yaml"
        assert files[0].status == "modified"


class TestGitHubCommitSchemaContract:
    """Contract tests for GitHubCommit schema parsing."""

    def test_parse_commits_open_pr(self) -> None:
        """Parse commits from open PR."""
        commits = [GitHubCommit(**c) for c in REAL_OPEN_PR["commits"]]

        assert len(commits) == OPEN_PR_METADATA["expected_commit_count"]
        assert commits[0].commit.author.name == "Abraham"
        assert commits[0].commit.message == "Adverxo Bid Adapter: New alias alchemyx"

    def test_parse_commits_merged_pr(self) -> None:
        """Parse commits from merged PR."""
        commits = [GitHubCommit(**c) for c in REAL_MERGED_PR["commits"]]

        assert len(commits) == MERGED_PR_METADATA["expected_commit_count"]
        # First commit
        assert commits[0].commit.author.name == "Victor Gonzalez"
        # Last commit
        assert commits[-1].commit.message == "add GPP macros"


class TestGitHubReviewSchemaContract:
    """Contract tests for GitHubReview schema parsing."""

    def test_parse_reviews_empty(self) -> None:
        """Open PR has no reviews."""
        reviews = [GitHubReview(**r) for r in REAL_OPEN_PR["reviews"]]

        assert len(reviews) == OPEN_PR_METADATA["expected_review_count"]

    def test_parse_reviews_merged_pr(self) -> None:
        """Parse reviews from merged PR."""
        reviews = [GitHubReview(**r) for r in REAL_MERGED_PR["reviews"]]

        assert len(reviews) == MERGED_PR_METADATA["expected_review_count"]

        # Check review states match expected
        expected = MERGED_PR_METADATA["expected_reviewer_actions"]
        for review in reviews:
            assert review.user.login in expected
            assert review.state == expected[review.user.login]


class TestPRCreateFactory:
    """Contract tests for GitHubPullRequest.to_pr_create() factory."""

    def test_to_pr_create_open_pr(self) -> None:
        """Factory produces valid PRCreate for open PR."""
        gh_pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])
        pr_create = gh_pr.to_pr_create(repository_id=1)

        assert pr_create.number == gh_pr.number
        assert pr_create.link == gh_pr.html_url
        assert pr_create.submitter == gh_pr.user.login
        assert pr_create.open_date == gh_pr.created_at
        assert pr_create.repository_id == 1

    def test_to_pr_create_merged_pr(self) -> None:
        """Factory produces valid PRCreate for merged PR."""
        gh_pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])
        pr_create = gh_pr.to_pr_create(repository_id=42)

        assert pr_create.number == gh_pr.number
        assert pr_create.link == gh_pr.html_url
        assert pr_create.submitter == gh_pr.user.login
        assert pr_create.repository_id == 42


class TestPRSyncFactory:
    """Contract tests for GitHubPullRequest.to_pr_sync() factory."""

    def test_to_pr_sync_open_pr(self) -> None:
        """Factory produces valid PRSync for open PR with empty collections."""
        gh_pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])
        files = [GitHubFile(**f) for f in REAL_OPEN_PR["files"]]
        commits = [GitHubCommit(**c) for c in REAL_OPEN_PR["commits"]]
        reviews = [GitHubReview(**r) for r in REAL_OPEN_PR["reviews"]]

        pr_sync = gh_pr.to_pr_sync(files=files, commits=commits, reviews=reviews)

        assert pr_sync.title == gh_pr.title
        assert pr_sync.description == gh_pr.body  # None
        assert pr_sync.state == PRState.OPEN
        assert pr_sync.files_changed == 1
        assert pr_sync.lines_added == 9
        assert pr_sync.lines_deleted == 0
        assert pr_sync.commits_count == 1
        assert pr_sync.github_labels == []
        assert pr_sync.participants == []  # No reviews
        assert len(pr_sync.filenames) == 1
        assert len(pr_sync.commits_breakdown) == 1

    def test_to_pr_sync_merged_pr(self) -> None:
        """Factory produces valid PRSync for merged PR with all data."""
        gh_pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])
        files = [GitHubFile(**f) for f in REAL_MERGED_PR["files"]]
        commits = [GitHubCommit(**c) for c in REAL_MERGED_PR["commits"]]
        reviews = [GitHubReview(**r) for r in REAL_MERGED_PR["reviews"]]

        pr_sync = gh_pr.to_pr_sync(files=files, commits=commits, reviews=reviews)

        assert pr_sync.title == gh_pr.title
        assert pr_sync.description == "Adds GPP and GPP_SID macros."
        assert pr_sync.state == PRState.MERGED
        assert pr_sync.files_changed == 1
        assert pr_sync.lines_added == 1
        assert pr_sync.lines_deleted == 1
        assert pr_sync.commits_count == 12
        assert pr_sync.github_labels == ["adapter"]
        assert len(pr_sync.participants) == 2  # 2 reviewers
        assert len(pr_sync.filenames) == 1
        assert len(pr_sync.commits_breakdown) == 12

    def test_to_pr_sync_handles_empty_collections(self) -> None:
        """Sync handles PRs with no reviews/commits/files."""
        gh_pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])
        pr_sync = gh_pr.to_pr_sync(files=[], commits=[], reviews=[])

        assert pr_sync.participants == []
        assert pr_sync.commits_breakdown == []
        assert pr_sync.filenames == []

    def test_to_pr_sync_reviewers_mapped_correctly(self) -> None:
        """Review states are mapped to correct participant actions."""
        from github_activity_db.schemas.enums import ParticipantActionType

        gh_pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])
        reviews = [GitHubReview(**r) for r in REAL_MERGED_PR["reviews"]]

        pr_sync = gh_pr.to_pr_sync(files=[], commits=[], reviews=reviews)

        # Both reviewers approved
        for participant in pr_sync.participants:
            assert ParticipantActionType.APPROVAL in participant.actions


class TestEdgeCases:
    """Edge case handling tests with real data."""

    def test_pr_with_null_body(self) -> None:
        """PR with null description."""
        gh_pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])
        assert gh_pr.body is None

        pr_sync = gh_pr.to_pr_sync()
        assert pr_sync.description is None

    def test_pr_with_body(self) -> None:
        """PR with description."""
        gh_pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])
        assert gh_pr.body == "Adds GPP and GPP_SID macros."

        pr_sync = gh_pr.to_pr_sync()
        assert pr_sync.description == "Adds GPP and GPP_SID macros."

    def test_title_within_max_length(self) -> None:
        """PR titles are within max length (500 chars)."""
        open_pr = GitHubPullRequest(**REAL_OPEN_PR["pr"])
        merged_pr = GitHubPullRequest(**REAL_MERGED_PR["pr"])

        assert len(open_pr.title) <= 500
        assert len(merged_pr.title) <= 500

        # Schemas should accept these
        open_sync = open_pr.to_pr_sync()
        merged_sync = merged_pr.to_pr_sync()

        assert len(open_sync.title) <= 500
        assert len(merged_sync.title) <= 500
