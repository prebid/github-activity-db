"""Tests for PullRequestRepository."""

from datetime import UTC, datetime, timedelta

from github_activity_db.db.models import PRState
from github_activity_db.db.repositories import PullRequestRepository
from github_activity_db.schemas import PRCreate, PRMerge, PRSync
from tests.conftest import JAN_10, JAN_12, JAN_15, JAN_16
from tests.factories import make_merged_pr, make_pull_request, make_repository


class TestPullRequestRepositoryQuery:
    """Query method tests for PullRequestRepository."""

    async def test_get_by_id(self, db_session):
        """Get PR by ID."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, number=1234)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session)
        result = await pr_repository.get_by_id(pr.id)

        assert result is not None
        assert result.id == pr.id
        assert result.number == 1234

    async def test_get_by_number(self, db_session):
        """Get PR by repository and number."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, number=4663)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session)
        result = await pr_repository.get_by_number(repo.id, 4663)

        assert result is not None
        assert result.id == pr.id

    async def test_get_by_number_not_found(self, db_session):
        """Get PR by number returns None if not found."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session)
        result = await pr_repository.get_by_number(repo.id, 9999)

        assert result is None

    async def test_get_by_state(self, db_session):
        """Get PRs by state."""
        repo = make_repository(db_session)
        await db_session.flush()
        make_pull_request(db_session, repo, number=1, state=PRState.OPEN)
        make_pull_request(db_session, repo, number=2, state=PRState.OPEN)
        make_merged_pr(db_session, repo, number=3)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session)
        open_prs = await pr_repository.get_by_state(repo.id, PRState.OPEN)
        merged_prs = await pr_repository.get_by_state(repo.id, PRState.MERGED)

        assert len(open_prs) == 2
        assert len(merged_prs) == 1

    async def test_get_open_prs(self, db_session):
        """Get open PRs convenience method."""
        repo = make_repository(db_session)
        await db_session.flush()
        make_pull_request(db_session, repo, number=1, state=PRState.OPEN)
        make_merged_pr(db_session, repo, number=2)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session)
        open_prs = await pr_repository.get_open_prs(repo.id)

        assert len(open_prs) == 1
        assert open_prs[0].number == 1

    async def test_get_numbers_by_state(self, db_session):
        """Get PR numbers by state."""
        repo = make_repository(db_session)
        await db_session.flush()
        make_pull_request(db_session, repo, number=100, state=PRState.OPEN)
        make_pull_request(db_session, repo, number=200, state=PRState.OPEN)
        make_merged_pr(db_session, repo, number=300)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session)
        numbers = await pr_repository.get_numbers_by_state(repo.id, PRState.OPEN)

        assert sorted(numbers) == [100, 200]


class TestPullRequestRepositoryCreate:
    """Create method tests for PullRequestRepository."""

    async def test_create(self, db_session):
        """Create a new PR with PRCreate and PRSync schemas."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr_create = PRCreate(
            number=4663,
            link="https://github.com/prebid/prebid-server/pull/4663",
            open_date=JAN_15,
            submitter="dev-adverxo",
            repository_id=repo.id,
        )
        pr_sync = PRSync(
            title="Test PR",
            description="Test description",
            last_update_date=JAN_16,
            state=PRState.OPEN,
            files_changed=1,
            lines_added=10,
            lines_deleted=0,
            commits_count=1,
        )

        pr_repository = PullRequestRepository(db_session)
        pr = await pr_repository.create(repo.id, pr_create, pr_sync)

        assert pr.id is not None
        assert pr.number == 4663
        assert pr.submitter == "dev-adverxo"
        assert pr.title == "Test PR"
        assert pr.state == PRState.OPEN

    async def test_create_with_all_fields(self, db_session):
        """Create PR with all sync fields populated."""
        repo = make_repository(db_session)
        await db_session.flush()

        from github_activity_db.schemas import CommitBreakdown, ParticipantEntry
        from github_activity_db.schemas.enums import ParticipantActionType

        pr_create = PRCreate(
            number=4646,
            link="https://github.com/prebid/prebid-server/pull/4646",
            open_date=JAN_10,
            submitter="testuser",
            repository_id=repo.id,
        )
        pr_sync = PRSync(
            title="Full Featured PR",
            description="A PR with all fields",
            last_update_date=JAN_12,
            state=PRState.OPEN,
            files_changed=3,
            lines_added=100,
            lines_deleted=50,
            commits_count=5,
            github_labels=["enhancement", "adapter"],
            filenames=["file1.go", "file2.go"],
            reviewers=["reviewer1"],
            assignees=["assignee1"],
            commits_breakdown=[
                CommitBreakdown(date=JAN_10, author="testuser"),
                CommitBreakdown(date=JAN_12, author="testuser"),
            ],
            participants=[
                ParticipantEntry(
                    username="reviewer1",
                    actions=[ParticipantActionType.APPROVAL],
                ),
            ],
        )

        pr_repository = PullRequestRepository(db_session)
        pr = await pr_repository.create(repo.id, pr_create, pr_sync)

        assert pr.github_labels == ["enhancement", "adapter"]
        assert pr.filenames == ["file1.go", "file2.go"]
        assert pr.reviewers == ["reviewer1"]
        assert len(pr.commits_breakdown) == 2
        assert "reviewer1" in pr.participants


class TestPullRequestRepositoryUpdate:
    """Update method tests for PullRequestRepository."""

    async def test_update_open_pr(self, db_session):
        """Update an open PR."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, number=1234, title="Old Title")
        await db_session.flush()

        pr_sync = PRSync(
            title="New Title",
            last_update_date=JAN_16,
            state=PRState.OPEN,
        )

        pr_repository = PullRequestRepository(db_session)
        result = await pr_repository.update(pr.id, pr_sync)

        assert result is not None
        assert result.title == "New Title"

    async def test_update_not_found(self, db_session):
        """Update returns None if PR not found."""
        pr_sync = PRSync(
            title="New Title",
            last_update_date=JAN_16,
            state=PRState.OPEN,
        )

        pr_repository = PullRequestRepository(db_session)
        result = await pr_repository.update(9999, pr_sync)

        assert result is None


class TestPullRequestRepositoryCreateOrUpdate:
    """create_or_update method tests for PullRequestRepository."""

    async def test_create_or_update_creates_new(self, db_session):
        """create_or_update creates new PR when not exists."""
        repo = make_repository(db_session)
        await db_session.flush()

        pr_create = PRCreate(
            number=1234,
            link="https://github.com/prebid/prebid-server/pull/1234",
            open_date=JAN_15,
            submitter="testuser",
            repository_id=repo.id,
        )
        pr_sync = PRSync(
            title="New PR",
            last_update_date=JAN_16,
            state=PRState.OPEN,
        )

        pr_repository = PullRequestRepository(db_session)
        pr, created = await pr_repository.create_or_update(repo.id, pr_create, pr_sync)

        assert created is True
        assert pr.number == 1234
        assert pr.title == "New PR"

    async def test_create_or_update_updates_existing_open(self, db_session):
        """create_or_update updates existing open PR."""
        repo = make_repository(db_session)
        await db_session.flush()
        existing = make_pull_request(
            db_session,
            repo,
            number=1234,
            title="Old Title",
            state=PRState.OPEN,
        )
        await db_session.flush()

        pr_create = PRCreate(
            number=1234,
            link="https://github.com/prebid/prebid-server/pull/1234",
            open_date=JAN_15,
            submitter="testuser",
            repository_id=repo.id,
        )
        pr_sync = PRSync(
            title="Updated Title",
            last_update_date=JAN_16,
            state=PRState.OPEN,
        )

        pr_repository = PullRequestRepository(db_session)
        pr, created = await pr_repository.create_or_update(repo.id, pr_create, pr_sync)

        assert created is False
        assert pr.id == existing.id
        assert pr.title == "Updated Title"


class TestPullRequestRepositoryMerge:
    """Merge handling tests for PullRequestRepository."""

    async def test_apply_merge(self, db_session):
        """Apply merge data to PR."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, number=1234, state=PRState.OPEN)
        await db_session.flush()

        merge_data = PRMerge(
            close_date=JAN_12,
            merged_by="maintainer",
        )

        pr_repository = PullRequestRepository(db_session)
        result = await pr_repository.apply_merge(pr.id, merge_data)

        assert result is not None
        assert result.state == PRState.MERGED
        assert result.close_date == JAN_12
        assert result.merged_by == "maintainer"

    async def test_apply_merge_with_summary(self, db_session):
        """Apply merge data with AI summary."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, number=1234, state=PRState.OPEN)
        await db_session.flush()

        merge_data = PRMerge(
            close_date=JAN_12,
            merged_by="maintainer",
            ai_summary="This PR fixes a bug.",
        )

        pr_repository = PullRequestRepository(db_session)
        result = await pr_repository.apply_merge(pr.id, merge_data)

        assert result is not None
        assert result.ai_summary == "This PR fixes a bug."


class TestPullRequestRepositoryFrozen:
    """Frozen PR (grace period) tests for PullRequestRepository."""

    async def test_is_frozen_open_pr(self, db_session):
        """Open PR is not frozen."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, state=PRState.OPEN)
        await db_session.flush()

        # Use short grace period for testing
        pr_repository = PullRequestRepository(db_session, grace_period=timedelta(days=14))

        assert pr_repository._is_frozen(pr) is False

    async def test_is_frozen_recently_merged(self, db_session):
        """Recently merged PR is not frozen (within grace period)."""
        repo = make_repository(db_session)
        await db_session.flush()
        # Merged just now
        pr = make_merged_pr(db_session, repo, close_date=datetime.now(UTC))
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session, grace_period=timedelta(days=14))

        assert pr_repository._is_frozen(pr) is False

    async def test_is_frozen_old_merged(self, db_session):
        """Old merged PR is frozen (past grace period)."""
        repo = make_repository(db_session)
        await db_session.flush()
        # Merged 30 days ago
        old_merge_date = datetime.now(UTC) - timedelta(days=30)
        pr = make_merged_pr(db_session, repo, close_date=old_merge_date)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session, grace_period=timedelta(days=14))

        assert pr_repository._is_frozen(pr) is True

    async def test_create_or_update_skips_frozen(self, db_session):
        """create_or_update doesn't update frozen PR."""
        repo = make_repository(db_session)
        await db_session.flush()
        # Merged 30 days ago
        old_merge_date = datetime.now(UTC) - timedelta(days=30)
        existing = make_merged_pr(
            db_session,
            repo,
            number=1234,
            title="Original Title",
            close_date=old_merge_date,
        )
        await db_session.flush()

        pr_create = PRCreate(
            number=1234,
            link="https://github.com/prebid/prebid-server/pull/1234",
            open_date=JAN_15,
            submitter="testuser",
            repository_id=repo.id,
        )
        pr_sync = PRSync(
            title="New Title",
            last_update_date=datetime.now(UTC),
            state=PRState.MERGED,
        )

        pr_repository = PullRequestRepository(db_session, grace_period=timedelta(days=14))
        pr, created = await pr_repository.create_or_update(repo.id, pr_create, pr_sync)

        assert created is False
        assert pr.id == existing.id
        assert pr.title == "Original Title"  # Not updated


class TestPullRequestRepositoryDiffDetection:
    """Diff detection (unchanged) tests for PullRequestRepository."""

    async def test_is_unchanged_same_date(self, db_session):
        """PR with same last_update_date is unchanged."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, last_update_date=JAN_16)
        await db_session.flush()

        pr_sync = PRSync(
            title="Test",
            last_update_date=JAN_16,
            state=PRState.OPEN,
        )

        pr_repository = PullRequestRepository(db_session)

        assert pr_repository.is_unchanged(pr, pr_sync) is True

    async def test_is_unchanged_newer_date(self, db_session):
        """PR with newer sync data is changed."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, last_update_date=JAN_15)
        await db_session.flush()

        pr_sync = PRSync(
            title="Test",
            last_update_date=JAN_16,
            state=PRState.OPEN,
        )

        pr_repository = PullRequestRepository(db_session)

        assert pr_repository.is_unchanged(pr, pr_sync) is False

    async def test_is_unchanged_older_date(self, db_session):
        """PR with older sync data is unchanged (no regression)."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, last_update_date=JAN_16)
        await db_session.flush()

        pr_sync = PRSync(
            title="Test",
            last_update_date=JAN_15,  # Older than existing
            state=PRState.OPEN,
        )

        pr_repository = PullRequestRepository(db_session)

        assert pr_repository.is_unchanged(pr, pr_sync) is True


class TestPullRequestRepositoryStateMachine:
    """State machine transition tests."""

    async def test_open_to_merged_transition(self, db_session):
        """OPEN PR transitions to MERGED via apply_merge."""
        repo = make_repository(db_session)
        await db_session.flush()
        pr = make_pull_request(db_session, repo, state=PRState.OPEN)
        await db_session.flush()

        pr_repository = PullRequestRepository(db_session)

        # Apply merge
        merge_data = PRMerge(close_date=JAN_12, merged_by="merger")
        result = await pr_repository.apply_merge(pr.id, merge_data)

        assert result is not None
        assert result.state == PRState.MERGED
        assert result.close_date == JAN_12

    async def test_merged_within_grace_period_can_update(self, db_session):
        """Merged PR within grace period can still be updated."""
        repo = make_repository(db_session)
        await db_session.flush()
        # Merged just now
        _pr = make_merged_pr(
            db_session,
            repo,
            number=1234,
            title="Old Title",
            close_date=datetime.now(UTC),
        )
        await db_session.flush()

        pr_create = PRCreate(
            number=1234,
            link="https://github.com/prebid/prebid-server/pull/1234",
            open_date=JAN_15,
            submitter="testuser",
            repository_id=repo.id,
        )
        pr_sync = PRSync(
            title="Updated Title",
            last_update_date=datetime.now(UTC),
            state=PRState.MERGED,
        )

        pr_repository = PullRequestRepository(db_session, grace_period=timedelta(days=14))
        result, created = await pr_repository.create_or_update(repo.id, pr_create, pr_sync)

        assert created is False
        assert result.title == "Updated Title"
