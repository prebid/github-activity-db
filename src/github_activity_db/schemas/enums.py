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
