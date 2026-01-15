"""Tests for PR Pydantic schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from github_activity_db.db.models import PRState
from github_activity_db.schemas import PRCreate, PRMerge, PRRead, PRSync
from github_activity_db.schemas.nested import CommitBreakdown, ParticipantEntry

from .conftest import JAN_15, JAN_15_ISO, JAN_16, JAN_20
from .factories import make_pull_request, make_repository


class TestPRCreate:
    """Tests for PRCreate schema."""

    def test_pr_create_valid(self):
        """Test valid PRCreate data is accepted."""
        pr = PRCreate(
            number=1234,
            link="https://github.com/prebid/prebid-server/pull/1234",
            open_date=JAN_15,
            submitter="testuser",
            repository_id=1,
        )
        assert pr.number == 1234
        assert pr.submitter == "testuser"

    def test_pr_create_invalid_url(self):
        """Test that invalid URL is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            PRCreate(
                number=1234,
                link="not-a-valid-url",
                open_date=JAN_15,
                submitter="testuser",
                repository_id=1,
            )
        assert "link" in str(exc_info.value)

    def test_pr_create_number_must_be_positive(self):
        """Test that number must be > 0."""
        with pytest.raises(ValidationError) as exc_info:
            PRCreate(
                number=0,
                link="https://github.com/prebid/prebid-server/pull/0",
                open_date=JAN_15,
                submitter="testuser",
                repository_id=1,
            )
        assert "number" in str(exc_info.value)

        with pytest.raises(ValidationError):
            PRCreate(
                number=-1,
                link="https://github.com/prebid/prebid-server/pull/-1",
                open_date=JAN_15,
                submitter="testuser",
                repository_id=1,
            )


class TestPRSync:
    """Tests for PRSync schema."""

    def test_pr_sync_valid(self):
        """Test valid PRSync data is accepted."""
        pr = PRSync(
            title="Add new feature",
            description="Feature description",
            last_update_date=JAN_16,
            state=PRState.OPEN,
            files_changed=5,
            lines_added=100,
            lines_deleted=10,
            commits_count=3,
        )
        assert pr.title == "Add new feature"
        assert pr.state == PRState.OPEN

    def test_pr_sync_parses_commits_breakdown(self):
        """Test that dict format is converted to CommitBreakdown objects."""
        pr = PRSync(
            title="Test",
            last_update_date=datetime.now(UTC),
            commits_breakdown=[
                {"date": JAN_15, "author": "user1"},
                {"date": JAN_16, "author": "user2"},
            ],
        )
        assert len(pr.commits_breakdown) == 2
        assert isinstance(pr.commits_breakdown[0], CommitBreakdown)
        assert pr.commits_breakdown[0].author == "user1"

    def test_pr_sync_parses_participants(self):
        """Test that dict format is converted to ParticipantEntry objects."""
        pr = PRSync(
            title="Test",
            last_update_date=datetime.now(UTC),
            participants={
                "reviewer1": ["comment", "approval"],
                "reviewer2": ["changes_requested"],
            },
        )
        assert len(pr.participants) == 2
        assert isinstance(pr.participants[0], ParticipantEntry)

        # Find reviewer1
        reviewer1 = next(p for p in pr.participants if p.username == "reviewer1")
        assert len(reviewer1.actions) == 2


class TestPRMerge:
    """Tests for PRMerge schema."""

    def test_pr_merge_valid(self):
        """Test valid PRMerge data is accepted."""
        pr = PRMerge(
            close_date=JAN_20,
            merged_by="maintainer",
            ai_summary="This PR adds a new bidder adapter with comprehensive tests.",
        )
        assert pr.merged_by == "maintainer"
        assert pr.ai_summary is not None

    def test_pr_merge_without_merged_by(self):
        """Test PRMerge without merged_by (closed without merge)."""
        pr = PRMerge(
            close_date=JAN_20,
            merged_by=None,
            ai_summary=None,
        )
        assert pr.merged_by is None


class TestPRRead:
    """Tests for PRRead schema."""

    async def test_pr_read_from_orm(self, db_session):
        """Test factory method creates PRRead from ORM model."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(
            db_session,
            repo,
            number=1234,
            title="Add new bidder adapter",
            github_labels=["enhancement", "needs-review"],
            filenames=["adapters/newbidder.go"],
        )
        await db_session.flush()

        # Convert to schema
        pr_read = PRRead.from_orm(pr)

        assert pr_read.number == 1234
        assert pr_read.title == "Add new bidder adapter"
        assert pr_read.state == PRState.OPEN

    async def test_pr_read_is_open_property(self, db_session):
        """Test is_open property reflects state correctly."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(db_session, repo, state=PRState.OPEN)
        await db_session.flush()

        pr_read = PRRead.from_orm(pr)
        assert pr_read.is_open is True
        assert pr_read.is_merged is False

    async def test_pr_read_get_commits_breakdown_typed(self, db_session):
        """Test helper method parses datetime strings."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(
            db_session,
            repo,
            commits_breakdown=[
                {"date": JAN_15_ISO, "author": "testuser"},
            ],
        )
        await db_session.flush()

        pr_read = PRRead.from_orm(pr)
        typed = pr_read.get_commits_breakdown_typed()

        assert len(typed) == 1
        assert isinstance(typed[0], CommitBreakdown)
        assert isinstance(typed[0].date, datetime)
        assert typed[0].author == "testuser"
