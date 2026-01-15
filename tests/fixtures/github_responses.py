"""Mock GitHub API response fixtures.

These fixtures represent realistic GitHub REST API responses for testing
schema parsing and conversion logic. Structure matches the GitHub REST API v3.

See: https://docs.github.com/en/rest/pulls/pulls
"""

from tests.conftest import (
    JAN_10_ISO,
    JAN_12_ISO,
    JAN_15_AFTERNOON_ISO,
    JAN_15_EVENING_ISO,
    JAN_15_ISO,
    JAN_16_LATE_ISO,
    JAN_16_MID_ISO,
    JAN_16_MORNING_ISO,
    JAN_16_UPDATED_ISO,
)

# -----------------------------------------------------------------------------
# User Response
# -----------------------------------------------------------------------------
GITHUB_USER_RESPONSE = {
    "login": "testuser",
    "id": 12345,
    "type": "User",
}

GITHUB_MERGED_BY_USER_RESPONSE = {
    "login": "maintainer",
    "id": 67890,
    "type": "User",
}

# -----------------------------------------------------------------------------
# Label Response
# -----------------------------------------------------------------------------
GITHUB_LABEL_RESPONSE = {
    "id": 1,
    "name": "bug",
    "color": "d73a4a",
    "description": "Something isn't working",
}

GITHUB_LABELS_RESPONSE = [
    GITHUB_LABEL_RESPONSE,
    {
        "id": 2,
        "name": "enhancement",
        "color": "a2eeef",
        "description": "New feature or request",
    },
]

# -----------------------------------------------------------------------------
# Pull Request Response (Open)
# -----------------------------------------------------------------------------
GITHUB_PR_RESPONSE = {
    "number": 1234,
    "html_url": "https://github.com/prebid/prebid-server/pull/1234",
    "state": "open",
    "title": "Add new bidder adapter for ExampleBidder",
    "body": (
        "This PR adds support for the ExampleBidder adapter.\n\n"
        "## Changes\n- Added adapter implementation\n- Added unit tests"
    ),
    "user": GITHUB_USER_RESPONSE,
    "merged_by": None,
    "created_at": JAN_15_ISO,
    "updated_at": JAN_16_UPDATED_ISO,
    "closed_at": None,
    "merged_at": None,
    "merged": False,
    "commits": 3,
    "additions": 250,
    "deletions": 10,
    "changed_files": 5,
    "labels": GITHUB_LABELS_RESPONSE,
    "requested_reviewers": [
        {"login": "reviewer1", "id": 111, "type": "User"},
        {"login": "reviewer2", "id": 222, "type": "User"},
    ],
    "assignees": [
        {"login": "testuser", "id": 12345, "type": "User"},
    ],
}

# -----------------------------------------------------------------------------
# Pull Request Response (Merged)
# -----------------------------------------------------------------------------
GITHUB_PR_MERGED_RESPONSE = {
    "number": 1235,
    "html_url": "https://github.com/prebid/prebid-server/pull/1235",
    "state": "closed",
    "title": "Fix timeout handling in auction endpoint",
    "body": "Fixes issue where auctions would hang on slow bidders.",
    "user": GITHUB_USER_RESPONSE,
    "merged_by": GITHUB_MERGED_BY_USER_RESPONSE,
    "created_at": JAN_10_ISO,
    "updated_at": JAN_12_ISO,
    "closed_at": JAN_12_ISO,
    "merged_at": JAN_12_ISO,
    "merged": True,
    "commits": 2,
    "additions": 45,
    "deletions": 12,
    "changed_files": 3,
    "labels": [GITHUB_LABEL_RESPONSE],
    "requested_reviewers": [],
    "assignees": [],
}

# -----------------------------------------------------------------------------
# Files Endpoint Response
# -----------------------------------------------------------------------------
GITHUB_FILES_RESPONSE = [
    {
        "sha": "abc123def456",
        "filename": "adapters/examplebidder/examplebidder.go",
        "status": "added",
        "additions": 200,
        "deletions": 0,
        "changes": 200,
    },
    {
        "sha": "def456ghi789",
        "filename": "adapters/examplebidder/examplebidder_test.go",
        "status": "added",
        "additions": 50,
        "deletions": 0,
        "changes": 50,
    },
    {
        "sha": "ghi789jkl012",
        "filename": "exchange/adapter_builders.go",
        "status": "modified",
        "additions": 5,
        "deletions": 0,
        "changes": 5,
    },
]

# -----------------------------------------------------------------------------
# Commits Endpoint Response
# -----------------------------------------------------------------------------
GITHUB_COMMITS_RESPONSE = [
    {
        "sha": "commit1sha",
        "commit": {
            "author": {
                "name": "Test User",
                "email": "testuser@example.com",
                "date": JAN_15_ISO,
            },
            "message": "Initial adapter implementation",
        },
    },
    {
        "sha": "commit2sha",
        "commit": {
            "author": {
                "name": "Test User",
                "email": "testuser@example.com",
                "date": JAN_15_AFTERNOON_ISO,
            },
            "message": "Add unit tests",
        },
    },
    {
        "sha": "commit3sha",
        "commit": {
            "author": {
                "name": "Another Dev",
                "email": "another@example.com",
                "date": JAN_16_MORNING_ISO,
            },
            "message": "Address review feedback",
        },
    },
]

# -----------------------------------------------------------------------------
# Reviews Endpoint Response
# -----------------------------------------------------------------------------
GITHUB_REVIEWS_RESPONSE = [
    {
        "id": 1001,
        "user": {"login": "reviewer1", "id": 111, "type": "User"},
        "state": "CHANGES_REQUESTED",
        "submitted_at": JAN_15_EVENING_ISO,
    },
    {
        "id": 1002,
        "user": {"login": "reviewer1", "id": 111, "type": "User"},
        "state": "APPROVED",
        "submitted_at": JAN_16_MID_ISO,
    },
    {
        "id": 1003,
        "user": {"login": "reviewer2", "id": 222, "type": "User"},
        "state": "COMMENTED",
        "submitted_at": JAN_16_LATE_ISO,
    },
]
