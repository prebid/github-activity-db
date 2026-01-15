"""Tests for nested Pydantic models."""

from github_activity_db.schemas import ParticipantActionType
from github_activity_db.schemas.nested import (
    CommitBreakdown,
    ParticipantEntry,
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
