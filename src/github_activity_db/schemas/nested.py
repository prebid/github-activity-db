"""Nested Pydantic models for complex fields."""

from datetime import datetime

from pydantic import BaseModel, Field

from .enums import ParticipantActionType


class CommitBreakdown(BaseModel):
    """Represents a single commit in the PR commit history."""

    date: datetime = Field(description="Commit timestamp in UTC")
    author: str = Field(max_length=100, description="GitHub username of commit author")


class ParticipantEntry(BaseModel):
    """Represents a participant's involvement in a PR."""

    username: str = Field(max_length=100, description="GitHub username")
    actions: list[ParticipantActionType] = Field(
        default_factory=list,
        description="List of actions taken by this participant",
    )

    @classmethod
    def from_dict(cls, username: str, actions: list[str]) -> "ParticipantEntry":
        """
        Factory method to create from the raw dict format stored in DB.

        Args:
            username: GitHub username
            actions: List of action strings

        Returns:
            ParticipantEntry instance with validated action types
        """
        valid_actions = []
        for action in actions:
            try:
                valid_actions.append(ParticipantActionType(action))
            except ValueError:
                # Skip unknown action types for forward compatibility
                pass
        return cls(username=username, actions=valid_actions)


def participants_from_dict(data: dict[str, list[str]]) -> list[ParticipantEntry]:
    """
    Convert participants dict from DB format to list of ParticipantEntry.

    Args:
        data: Dict mapping username to list of action strings

    Returns:
        List of ParticipantEntry instances
    """
    return [ParticipantEntry.from_dict(username, actions) for username, actions in data.items()]


def participants_to_dict(entries: list[ParticipantEntry]) -> dict[str, list[str]]:
    """
    Convert list of ParticipantEntry back to DB dict format.

    Args:
        entries: List of ParticipantEntry instances

    Returns:
        Dict mapping username to list of action strings
    """
    return {entry.username: [action.value for action in entry.actions] for entry in entries}
