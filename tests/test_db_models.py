"""Tests for SQLAlchemy ORM models."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from github_activity_db.db.models import PRState, PullRequest, UserTag, pr_user_tags

from .factories import make_merged_pr, make_pull_request, make_repository, make_user_tag


class TestRepositoryModel:
    """Tests for Repository model."""

    async def test_create_repository(self, db_session):
        """Test creating and querying a repository."""
        repo = make_repository(db_session)
        await db_session.flush()

        # Query it back
        result = await db_session.execute(
            select(repo.__class__).where(repo.__class__.full_name == "prebid/prebid-server")
        )
        fetched = result.scalar_one()

        assert fetched.owner == "prebid"
        assert fetched.name == "prebid-server"
        assert fetched.is_active is True
        assert fetched.last_synced_at is None

    async def test_repository_unique_full_name(self, db_session):
        """Test that duplicate full_name is rejected."""
        make_repository(db_session, name="test-repo")
        await db_session.flush()

        make_repository(db_session, name="test-repo")  # Same full_name

        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestPullRequestModel:
    """Tests for PullRequest model."""

    async def test_create_pull_request(self, db_session):
        """Test creating a pull request with foreign key."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(db_session, repo, number=1234, title="Test PR")
        await db_session.flush()

        assert pr.id is not None
        assert pr.repository_id == repo.id

    async def test_pr_repository_relationship(self, db_session):
        """Test PR.repository backref works."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(db_session, repo)
        await db_session.flush()

        # Access relationship
        assert pr.repository is not None
        assert pr.repository.full_name == "prebid/prebid-server"

    async def test_pr_unique_constraint(self, db_session):
        """Test that duplicate (repo_id, number) is rejected."""
        repo = make_repository(db_session)
        await db_session.flush()

        make_pull_request(db_session, repo, number=100)
        await db_session.flush()

        make_pull_request(db_session, repo, number=100)  # Same number, same repo

        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_pr_state_enum(self, db_session):
        """Test PRState enum values."""
        assert PRState.OPEN.value == "open"
        assert PRState.MERGED.value == "merged"
        assert PRState.CLOSED.value == "closed"

        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(db_session, repo, state=PRState.OPEN)
        await db_session.flush()

        assert pr.state == PRState.OPEN

    async def test_pr_is_open_property(self, db_session):
        """Test PR.is_open property returns correct value."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(db_session, repo, state=PRState.OPEN)
        await db_session.flush()

        assert pr.is_open is True
        assert pr.is_merged is False

    async def test_pr_is_merged_property(self, db_session):
        """Test PR.is_merged property returns correct value."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_merged_pr(db_session, repo)
        await db_session.flush()

        assert pr.is_merged is True
        assert pr.is_open is False


class TestUserTagModel:
    """Tests for UserTag model."""

    async def test_user_tag_many_to_many(self, db_session):
        """Test Tag ↔ PR many-to-many relationship."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(db_session, repo)
        tag = make_user_tag(db_session, name="needs-review")
        await db_session.flush()

        # Associate tag with PR via junction table (async-safe approach)
        await db_session.execute(pr_user_tags.insert().values(pr_id=pr.id, user_tag_id=tag.id))
        await db_session.flush()

        # Query with eager loading to verify relationship
        result = await db_session.execute(
            select(PullRequest)
            .where(PullRequest.id == pr.id)
            .options(selectinload(PullRequest.user_tags))
        )
        fetched_pr = result.scalar_one()
        assert len(fetched_pr.user_tags) == 1
        assert fetched_pr.user_tags[0].name == "needs-review"

        # Verify from tag side
        result = await db_session.execute(
            select(UserTag).where(UserTag.id == tag.id).options(selectinload(UserTag.pull_requests))
        )
        fetched_tag = result.scalar_one()
        assert len(fetched_tag.pull_requests) == 1

    async def test_cascade_delete_pr_tags(self, db_session):
        """Test that deleting PR removes junction table rows."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr = make_pull_request(db_session, repo)
        tag = make_user_tag(db_session, name="to-delete")
        await db_session.flush()

        # Associate via junction table (async-safe)
        await db_session.execute(pr_user_tags.insert().values(pr_id=pr.id, user_tag_id=tag.id))
        await db_session.flush()

        tag_id = tag.id
        await db_session.delete(pr)
        await db_session.flush()

        # Tag should still exist, but no longer associated
        result = await db_session.execute(
            select(UserTag).where(UserTag.id == tag_id).options(selectinload(UserTag.pull_requests))
        )
        fetched_tag = result.scalar_one()
        assert fetched_tag is not None
        assert len(fetched_tag.pull_requests) == 0
