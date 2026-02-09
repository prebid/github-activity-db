"""Nested Pydantic models for complex fields."""

from datetime import datetime

from pydantic import BaseModel, Field

from .enums import FileChangeStatus, ParticipantActionType


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


class FileChange(BaseModel):
    """Represents a single file change in a PR with per-file statistics."""

    filename: str = Field(description="File path relative to repository root")
    status: FileChangeStatus = Field(
        description="File change status (added, modified, removed, etc.)",
    )
    additions: int = Field(default=0, ge=0, description="Lines added in this file")
    deletions: int = Field(default=0, ge=0, description="Lines deleted in this file")
    changes: int = Field(default=0, ge=0, description="Total line changes in this file")


def file_changes_from_list(data: list[dict[str, str | int]]) -> list[FileChange]:
    """Convert file_changes list from DB format to list of FileChange.

    Args:
        data: List of dicts with filename, status, additions, deletions, changes

    Returns:
        List of FileChange instances
    """
    result = []
    for item in data:
        status_str = str(item.get("status", "unknown"))
        try:
            status = FileChangeStatus(status_str)
        except ValueError:
            status = FileChangeStatus.UNKNOWN
        result.append(
            FileChange(
                filename=str(item.get("filename", "")),
                status=status,
                additions=int(item.get("additions", 0)),
                deletions=int(item.get("deletions", 0)),
                changes=int(item.get("changes", 0)),
            )
        )
    return result


def file_changes_to_list(entries: list[FileChange]) -> list[dict[str, str | int]]:
    """Convert list of FileChange to DB-storable list of dicts.

    Args:
        entries: List of FileChange instances

    Returns:
        List of dicts ready for JSON serialization
    """
    return [
        {
            "filename": entry.filename,
            "status": entry.status.value,
            "additions": entry.additions,
            "deletions": entry.deletions,
            "changes": entry.changes,
        }
        for entry in entries
    ]
