"""Enums for Pydantic schemas."""

from enum import Enum


class ParticipantActionType(str, Enum):
    """Types of actions a participant can take on a PR."""

    COMMENT = "comment"
    APPROVAL = "approval"
    CHANGES_REQUESTED = "changes_requested"
    DISMISSED = "dismissed"
    REVIEW = "review"
    COMMIT = "commit"


class FileChangeStatus(str, Enum):
    """Status of a file change in a PR.

    Values match GitHub API file status values.
    'unknown' is used for legacy data migrated before per-file stats.
    """

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"
