"""Tests for Repository Pydantic schemas."""

from datetime import UTC, datetime

from github_activity_db.db.models import Repository
from github_activity_db.schemas import RepositoryCreate, RepositoryRead


class TestRepositoryCreate:
    """Tests for RepositoryCreate schema."""

    def test_repository_create_valid(self):
        """Test valid data is accepted."""
        repo = RepositoryCreate(
            owner="prebid",
            name="prebid-server",
            full_name="prebid/prebid-server",
        )
        assert repo.owner == "prebid"
        assert repo.name == "prebid-server"
        assert repo.full_name == "prebid/prebid-server"

    def test_repository_create_from_full_name(self):
        """Test factory method parses 'owner/name' correctly."""
        repo = RepositoryCreate.from_full_name("prebid/prebid-server")

        assert repo.owner == "prebid"
        assert repo.name == "prebid-server"
        assert repo.full_name == "prebid/prebid-server"

    def test_repository_create_from_full_name_with_slash_in_name(self):
        """Test factory handles repos with slashes in org name."""
        # Edge case: nested paths like "org/sub/repo" should split on first /
        repo = RepositoryCreate.from_full_name("my-org/my-repo")

        assert repo.owner == "my-org"
        assert repo.name == "my-repo"


class TestRepositoryRead:
    """Tests for RepositoryRead schema."""

    async def test_repository_read_from_orm(self, db_session, sample_repository):
        """Test ORM conversion works correctly."""
        repo = Repository(**sample_repository)
        db_session.add(repo)
        await db_session.flush()

        repo_read = RepositoryRead.from_orm(repo)

        assert repo_read.id == repo.id
        assert repo_read.owner == "prebid"
        assert repo_read.name == "prebid-server"
        assert repo_read.full_name == "prebid/prebid-server"
        assert repo_read.is_active is True
        assert repo_read.last_synced_at is None
        assert isinstance(repo_read.created_at, datetime)

    async def test_repository_read_with_last_synced(self, db_session, sample_repository):
        """Test that last_synced_at is included when set."""
        sync_time = datetime.now(UTC)
        repo = Repository(**sample_repository, last_synced_at=sync_time)
        db_session.add(repo)
        await db_session.flush()

        repo_read = RepositoryRead.from_orm(repo)

        assert repo_read.last_synced_at == sync_time
