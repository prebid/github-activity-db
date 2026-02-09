"""Tests for nested Pydantic models."""

from github_activity_db.schemas import ParticipantActionType
from github_activity_db.schemas.enums import FileChangeStatus
from github_activity_db.schemas.nested import (
    CommitBreakdown,
    FileChange,
    ParticipantEntry,
    file_changes_from_list,
    file_changes_to_list,
    participants_from_dict,
    participants_to_dict,
)

from .conftest import JAN_15


class TestCommitBreakdown:
    """Tests for CommitBreakdown model."""

    def test_commit_breakdown_valid(self):
        """Test valid data is accepted."""
        commit = CommitBreakdown(
            date=JAN_15,
            author="testuser",
        )
        assert commit.author == "testuser"
        assert commit.date.year == 2024

    def test_commit_breakdown_with_timezone(self):
        """Test datetime with timezone is preserved."""
        commit = CommitBreakdown(date=JAN_15, author="user")
        assert commit.date.tzinfo is not None


class TestParticipantEntry:
    """Tests for ParticipantEntry model."""

    def test_participant_entry_valid(self):
        """Test valid participant data."""
        entry = ParticipantEntry(
            username="reviewer1",
            actions=[ParticipantActionType.COMMENT, ParticipantActionType.APPROVAL],
        )
        assert entry.username == "reviewer1"
        assert len(entry.actions) == 2
        assert ParticipantActionType.APPROVAL in entry.actions

    def test_participant_entry_empty_actions(self):
        """Test participant with no actions."""
        entry = ParticipantEntry(username="user", actions=[])
        assert entry.username == "user"
        assert len(entry.actions) == 0

    def test_participant_entry_from_dict(self):
        """Test factory method creates entry from dict format."""
        entry = ParticipantEntry.from_dict(
            username="reviewer",
            actions=["comment", "approval"],
        )
        assert entry.username == "reviewer"
        assert ParticipantActionType.COMMENT in entry.actions
        assert ParticipantActionType.APPROVAL in entry.actions

    def test_participant_entry_skips_unknown_actions(self):
        """Test that unknown action types are skipped for forward compatibility."""
        entry = ParticipantEntry.from_dict(
            username="reviewer",
            actions=["comment", "unknown_future_action", "approval"],
        )
        # Should only have the known actions
        assert len(entry.actions) == 2
        assert ParticipantActionType.COMMENT in entry.actions
        assert ParticipantActionType.APPROVAL in entry.actions


class TestParticipantConversion:
    """Tests for participant dict ↔ list conversion functions."""

    def test_participants_from_dict(self):
        """Test converting dict format to list of ParticipantEntry."""
        data = {
            "user1": ["comment", "approval"],
            "user2": ["changes_requested"],
        }
        result = participants_from_dict(data)

        assert len(result) == 2

        user1 = next(p for p in result if p.username == "user1")
        assert len(user1.actions) == 2

        user2 = next(p for p in result if p.username == "user2")
        assert ParticipantActionType.CHANGES_REQUESTED in user2.actions

    def test_participants_from_dict_empty(self):
        """Test converting empty dict."""
        result = participants_from_dict({})
        assert result == []

    def test_participants_to_dict(self):
        """Test converting list of ParticipantEntry back to dict."""
        entries = [
            ParticipantEntry(
                username="user1",
                actions=[ParticipantActionType.COMMENT, ParticipantActionType.APPROVAL],
            ),
            ParticipantEntry(
                username="user2",
                actions=[ParticipantActionType.REVIEW],
            ),
        ]
        result = participants_to_dict(entries)

        assert result == {
            "user1": ["comment", "approval"],
            "user2": ["review"],
        }

    def test_participants_to_dict_empty(self):
        """Test converting empty list."""
        result = participants_to_dict([])
        assert result == {}

    def test_participants_roundtrip(self):
        """Test that dict → list → dict preserves data."""
        original = {
            "reviewer1": ["comment", "approval"],
            "reviewer2": ["changes_requested", "review"],
        }

        entries = participants_from_dict(original)
        result = participants_to_dict(entries)

        assert result == original


class TestFileChange:
    """Tests for FileChange model."""

    def test_file_change_valid(self):
        """Test valid data is accepted."""
        fc = FileChange(
            filename="adapters/newbidder.go",
            status=FileChangeStatus.ADDED,
            additions=100,
            deletions=0,
            changes=100,
        )
        assert fc.filename == "adapters/newbidder.go"
        assert fc.status == FileChangeStatus.ADDED
        assert fc.additions == 100
        assert fc.deletions == 0
        assert fc.changes == 100

    def test_file_change_defaults(self):
        """Test default values for optional fields."""
        fc = FileChange(
            filename="README.md",
            status=FileChangeStatus.MODIFIED,
        )
        assert fc.additions == 0
        assert fc.deletions == 0
        assert fc.changes == 0

    def test_file_change_all_statuses(self):
        """Test all FileChangeStatus enum values are accepted."""
        for status in FileChangeStatus:
            fc = FileChange(filename="test.go", status=status)
            assert fc.status == status


class TestFileChangeConversion:
    """Tests for file_changes list ↔ FileChange conversion functions."""

    def test_file_changes_from_list(self):
        """Test converting DB list format to FileChange objects."""
        data: list[dict[str, str | int]] = [
            {"filename": "a.go", "status": "added", "additions": 50, "deletions": 0, "changes": 50},
            {"filename": "b.go", "status": "modified", "additions": 10, "deletions": 5, "changes": 15},
        ]
        result = file_changes_from_list(data)

        assert len(result) == 2
        assert result[0].filename == "a.go"
        assert result[0].status == FileChangeStatus.ADDED
        assert result[0].additions == 50
        assert result[1].filename == "b.go"
        assert result[1].status == FileChangeStatus.MODIFIED
        assert result[1].deletions == 5

    def test_file_changes_from_list_unknown_status(self):
        """Test that unknown status falls back to UNKNOWN."""
        data: list[dict[str, str | int]] = [
            {"filename": "x.go", "status": "some_future_status", "additions": 1, "deletions": 0, "changes": 1},
        ]
        result = file_changes_from_list(data)

        assert len(result) == 1
        assert result[0].status == FileChangeStatus.UNKNOWN

    def test_file_changes_from_list_missing_status(self):
        """Test that missing status defaults to UNKNOWN."""
        data: list[dict[str, str | int]] = [
            {"filename": "y.go"},
        ]
        result = file_changes_from_list(data)

        assert result[0].status == FileChangeStatus.UNKNOWN
        assert result[0].additions == 0

    def test_file_changes_from_list_empty(self):
        """Test converting empty list."""
        result = file_changes_from_list([])
        assert result == []

    def test_file_changes_to_list(self):
        """Test converting FileChange objects to DB list format."""
        entries = [
            FileChange(filename="a.go", status=FileChangeStatus.ADDED, additions=50, deletions=0, changes=50),
            FileChange(filename="b.go", status=FileChangeStatus.REMOVED, additions=0, deletions=30, changes=30),
        ]
        result = file_changes_to_list(entries)

        assert result == [
            {"filename": "a.go", "status": "added", "additions": 50, "deletions": 0, "changes": 50},
            {"filename": "b.go", "status": "removed", "additions": 0, "deletions": 30, "changes": 30},
        ]

    def test_file_changes_to_list_empty(self):
        """Test converting empty list."""
        result = file_changes_to_list([])
        assert result == []

    def test_file_changes_roundtrip(self):
        """Test that list → FileChange → list preserves data."""
        original: list[dict[str, str | int]] = [
            {"filename": "a.go", "status": "added", "additions": 50, "deletions": 0, "changes": 50},
            {"filename": "b.go", "status": "renamed", "additions": 5, "deletions": 3, "changes": 8},
        ]

        entries = file_changes_from_list(original)
        result = file_changes_to_list(entries)

        assert result == original
