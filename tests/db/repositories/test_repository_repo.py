"""Tests for RepositoryRepository."""

from datetime import UTC, datetime

from github_activity_db.db.repositories import RepositoryRepository
from tests.factories import make_repository


class TestRepositoryRepositoryQuery:
    """Query method tests for RepositoryRepository."""

    async def test_get_by_id(self, db_session):
        """Get repository by ID."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        result = await repository.get_by_id(repo.id)

        assert result is not None
        assert result.id == repo.id
        assert result.owner == "prebid"
        assert result.name == "prebid-server"

    async def test_get_by_id_not_found(self, db_session):
        """Get repository by ID returns None if not found."""
        repository = RepositoryRepository(db_session)
        result = await repository.get_by_id(9999)

        assert result is None

    async def test_get_by_full_name(self, db_session):
        """Get repository by full name."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        result = await repository.get_by_full_name("prebid/prebid-server")

        assert result is not None
        assert result.id == repo.id

    async def test_get_by_full_name_not_found(self, db_session):
        """Get repository by full name returns None if not found."""
        repository = RepositoryRepository(db_session)
        result = await repository.get_by_full_name("nonexistent/repo")

        assert result is None

    async def test_get_by_owner_and_name(self, db_session):
        """Get repository by owner and name."""
        repo = make_repository(db_session, owner="prebid", name="Prebid.js")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        result = await repository.get_by_owner_and_name("prebid", "Prebid.js")

        assert result is not None
        assert result.id == repo.id

    async def test_get_active(self, db_session):
        """Get all active repositories."""
        repo1 = make_repository(db_session, owner="prebid", name="repo1", is_active=True)
        repo2 = make_repository(db_session, owner="prebid", name="repo2", is_active=True)
        make_repository(db_session, owner="prebid", name="inactive", is_active=False)
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        results = await repository.get_active()

        assert len(results) == 2
        ids = {r.id for r in results}
        assert repo1.id in ids
        assert repo2.id in ids


class TestRepositoryRepositoryCreate:
    """Create method tests for RepositoryRepository."""

    async def test_create(self, db_session):
        """Create a new repository."""
        repository = RepositoryRepository(db_session)
        repo = await repository.create("prebid", "prebid-server")

        assert repo.id is not None
        assert repo.owner == "prebid"
        assert repo.name == "prebid-server"
        assert repo.full_name == "prebid/prebid-server"
        assert repo.is_active is True
        assert repo.last_synced_at is None

    async def test_create_inactive(self, db_session):
        """Create an inactive repository."""
        repository = RepositoryRepository(db_session)
        repo = await repository.create("prebid", "inactive-repo", is_active=False)

        assert repo.is_active is False

    async def test_get_or_create_creates(self, db_session):
        """get_or_create creates a new repository."""
        repository = RepositoryRepository(db_session)
        repo, created = await repository.get_or_create("prebid", "new-repo")

        assert created is True
        assert repo.owner == "prebid"
        assert repo.name == "new-repo"

    async def test_get_or_create_gets_existing(self, db_session):
        """get_or_create returns existing repository."""
        existing = make_repository(db_session, owner="prebid", name="existing")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        repo, created = await repository.get_or_create("prebid", "existing")

        assert created is False
        assert repo.id == existing.id


class TestRepositoryRepositoryUpdate:
    """Update method tests for RepositoryRepository."""

    async def test_update_last_synced(self, db_session):
        """Update last_synced_at timestamp."""
        repo = make_repository(db_session, owner="prebid", name="prebid-server")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        sync_time = datetime.now(UTC)
        result = await repository.update_last_synced(repo.id, sync_time)

        assert result is not None
        assert result.last_synced_at == sync_time

    async def test_update_last_synced_not_found(self, db_session):
        """Update last_synced_at returns None if not found."""
        repository = RepositoryRepository(db_session)
        result = await repository.update_last_synced(9999, datetime.now(UTC))

        assert result is None

    async def test_deactivate(self, db_session):
        """Deactivate a repository."""
        repo = make_repository(db_session, owner="prebid", name="active", is_active=True)
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        result = await repository.deactivate(repo.id)

        assert result is not None
        assert result.is_active is False

    async def test_activate(self, db_session):
        """Activate a repository."""
        repo = make_repository(db_session, owner="prebid", name="inactive", is_active=False)
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        result = await repository.activate(repo.id)

        assert result is not None
        assert result.is_active is True


class TestRepositoryRepositoryBase:
    """Base repository method tests via RepositoryRepository."""

    async def test_count(self, db_session):
        """Count total repositories."""
        make_repository(db_session, owner="prebid", name="repo1")
        make_repository(db_session, owner="prebid", name="repo2")
        make_repository(db_session, owner="prebid", name="repo3")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        count = await repository.count()

        assert count == 3

    async def test_exists(self, db_session):
        """Check if repository exists."""
        repo = make_repository(db_session, owner="prebid", name="exists")
        await db_session.flush()

        repository = RepositoryRepository(db_session)

        assert await repository.exists(repo.id) is True
        assert await repository.exists(9999) is False

    async def test_get_all(self, db_session):
        """Get all repositories."""
        make_repository(db_session, owner="prebid", name="repo1")
        make_repository(db_session, owner="prebid", name="repo2")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        results = await repository.get_all()

        assert len(results) == 2

    async def test_get_all_with_limit(self, db_session):
        """Get all repositories with limit."""
        make_repository(db_session, owner="prebid", name="repo1")
        make_repository(db_session, owner="prebid", name="repo2")
        make_repository(db_session, owner="prebid", name="repo3")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        results = await repository.get_all(limit=2)

        assert len(results) == 2

    async def test_delete(self, db_session):
        """Delete a repository."""
        repo = make_repository(db_session, owner="prebid", name="to-delete")
        await db_session.flush()

        repository = RepositoryRepository(db_session)
        await repository.delete(repo)
        await db_session.flush()

        result = await repository.get_by_id(repo.id)
        assert result is None
