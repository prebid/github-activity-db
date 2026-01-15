"""Tests for GitHub API Pydantic schemas."""


from github_activity_db.db.models import PRState
from github_activity_db.schemas import ParticipantActionType
from github_activity_db.schemas.github_api import (
    GitHubCommit,
    GitHubFile,
    GitHubLabel,
    GitHubPullRequest,
    GitHubReview,
    GitHubUser,
)

from .fixtures import (
    GITHUB_COMMITS_RESPONSE,
    GITHUB_FILES_RESPONSE,
    GITHUB_LABEL_RESPONSE,
    GITHUB_PR_MERGED_RESPONSE,
    GITHUB_PR_RESPONSE,
    GITHUB_REVIEWS_RESPONSE,
    GITHUB_USER_RESPONSE,
)


class TestGitHubUser:
    """Tests for GitHubUser schema."""

    def test_github_user_parse(self):
        """Test parsing user response."""
        user = GitHubUser(**GITHUB_USER_RESPONSE)

        assert user.login == "testuser"
        assert user.id == 12345
        assert user.type == "User"


class TestGitHubLabel:
    """Tests for GitHubLabel schema."""

    def test_github_label_parse(self):
        """Test parsing label response."""
        label = GitHubLabel(**GITHUB_LABEL_RESPONSE)

        assert label.id == 1
        assert label.name == "bug"
        assert label.color == "d73a4a"
        assert label.description == "Something isn't working"


class TestGitHubPullRequest:
    """Tests for GitHubPullRequest schema."""

    def test_github_pr_parse(self):
        """Test parsing full PR response."""
        pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)

        assert pr.number == 1234
        assert pr.title == "Add new bidder adapter for ExampleBidder"
        assert pr.state == "open"
        assert pr.merged is False
        assert pr.user.login == "testuser"
        assert len(pr.labels) == 2
        assert len(pr.requested_reviewers) == 2

    def test_github_pr_to_pr_create(self):
        """Test factory produces valid PRCreate."""
        gh_pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)
        pr_create = gh_pr.to_pr_create(repository_id=1)

        assert pr_create.number == 1234
        assert pr_create.link == "https://github.com/prebid/prebid-server/pull/1234"
        assert pr_create.submitter == "testuser"
        assert pr_create.repository_id == 1

    def test_github_pr_to_pr_sync(self):
        """Test factory produces valid PRSync."""
        gh_pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)
        pr_sync = gh_pr.to_pr_sync()

        assert pr_sync.title == "Add new bidder adapter for ExampleBidder"
        assert pr_sync.state == PRState.OPEN
        assert pr_sync.files_changed == 5
        assert pr_sync.lines_added == 250
        assert pr_sync.commits_count == 3
        assert "bug" in pr_sync.github_labels
        assert "enhancement" in pr_sync.github_labels
        assert "reviewer1" in pr_sync.reviewers

    def test_github_pr_to_pr_sync_with_files(self):
        """Test filenames are extracted from files response."""
        gh_pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)
        files = [GitHubFile(**f) for f in GITHUB_FILES_RESPONSE]

        pr_sync = gh_pr.to_pr_sync(files=files)

        assert len(pr_sync.filenames) == 3
        assert "adapters/examplebidder/examplebidder.go" in pr_sync.filenames
        assert "exchange/adapter_builders.go" in pr_sync.filenames

    def test_github_pr_to_pr_sync_with_commits(self):
        """Test CommitBreakdown is built from commits response."""
        gh_pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)
        commits = [GitHubCommit(**c) for c in GITHUB_COMMITS_RESPONSE]

        pr_sync = gh_pr.to_pr_sync(commits=commits)

        assert len(pr_sync.commits_breakdown) == 3
        assert pr_sync.commits_breakdown[0].author == "Test User"
        assert pr_sync.commits_breakdown[2].author == "Another Dev"

    def test_github_pr_to_pr_sync_with_reviews(self):
        """Test participants are built from reviews response."""
        gh_pr = GitHubPullRequest(**GITHUB_PR_RESPONSE)
        reviews = [GitHubReview(**r) for r in GITHUB_REVIEWS_RESPONSE]

        pr_sync = gh_pr.to_pr_sync(reviews=reviews)

        # Should have 2 unique reviewers
        assert len(pr_sync.participants) == 2

        # Find reviewer1 who had CHANGES_REQUESTED then APPROVED
        reviewer1 = next(p for p in pr_sync.participants if p.username == "reviewer1")
        assert ParticipantActionType.CHANGES_REQUESTED in reviewer1.actions
        assert ParticipantActionType.APPROVAL in reviewer1.actions

        # Find reviewer2 who just commented
        reviewer2 = next(p for p in pr_sync.participants if p.username == "reviewer2")
        assert ParticipantActionType.REVIEW in reviewer2.actions

    def test_github_pr_merged_state(self):
        """Test merged PR sets correct state."""
        gh_pr = GitHubPullRequest(**GITHUB_PR_MERGED_RESPONSE)
        pr_sync = gh_pr.to_pr_sync()

        assert pr_sync.state == PRState.MERGED
        assert gh_pr.merged is True
        assert gh_pr.merged_by is not None
        assert gh_pr.merged_by.login == "maintainer"


class TestGitHubCommit:
    """Tests for GitHubCommit schema."""

    def test_github_commit_parse(self):
        """Test parsing commit response."""
        commit = GitHubCommit(**GITHUB_COMMITS_RESPONSE[0])

        assert commit.sha == "commit1sha"
        assert commit.commit.author.name == "Test User"
        assert commit.commit.author.email == "testuser@example.com"
        assert commit.commit.message == "Initial adapter implementation"


class TestGitHubFile:
    """Tests for GitHubFile schema."""

    def test_github_file_parse(self):
        """Test parsing file response."""
        file = GitHubFile(**GITHUB_FILES_RESPONSE[0])

        assert file.sha == "abc123def456"
        assert file.filename == "adapters/examplebidder/examplebidder.go"
        assert file.status == "added"
        assert file.additions == 200
        assert file.deletions == 0


class TestGitHubReview:
    """Tests for GitHubReview schema."""

    def test_github_review_parse(self):
        """Test parsing review response."""
        review = GitHubReview(**GITHUB_REVIEWS_RESPONSE[0])

        assert review.id == 1001
        assert review.user.login == "reviewer1"
        assert review.state == "CHANGES_REQUESTED"

    def test_github_review_approved(self):
        """Test parsing approved review."""
        review = GitHubReview(**GITHUB_REVIEWS_RESPONSE[1])

        assert review.state == "APPROVED"
