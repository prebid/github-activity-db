"""Real open PR fixture data from prebid/prebid-server.

Captured from PR #4663: Adverxo Bid Adapter: New alias alchemyx
State: OPEN (never merged)

This fixture represents a simple, newly created PR with:
- Single file added
- Single commit
- No reviews yet
- No labels
- No assignees
"""

REAL_OPEN_PR = {
    "pr": {
        "number": 4663,
        "html_url": "https://github.com/prebid/prebid-server/pull/4663",
        "state": "open",
        "title": "Adverxo Bid Adapter: New alias alchemyx",
        "body": None,
        "user": {
            "login": "dev-adverxo",
            "id": 186024688,
            "type": "User",
        },
        "merged_by": None,
        "created_at": "2026-01-16T11:07:42Z",
        "updated_at": "2026-01-16T11:07:42Z",
        "closed_at": None,
        "merged_at": None,
        "merged": False,
        "commits": 1,
        "additions": 9,
        "deletions": 0,
        "changed_files": 1,
        "labels": [],
        "requested_reviewers": [],
        "assignees": [],
    },
    "files": [
        {
            "sha": "1f479b03f7a503e75cc72611193968cfb2283aae",
            "filename": "static/bidder-info/alchemyx.yaml",
            "status": "added",
            "additions": 9,
            "deletions": 0,
            "changes": 9,
        }
    ],
    "commits": [
        {
            "sha": "6530f8f078ab25e006ee45cca43fd6c4b3af9f4d",
            "commit": {
                "author": {
                    "name": "Abraham",
                    "email": "abraham@kimiagroup.com",
                    "date": "2026-01-16T10:58:38Z",
                },
                "message": "Adverxo Bid Adapter: New alias alchemyx",
            },
        }
    ],
    "reviews": [],
}

# Metadata for test assertions
OPEN_PR_METADATA = {
    "repository": {
        "owner": "prebid",
        "name": "prebid-server",
    },
    "expected_state": "open",
    "expected_merged": False,
    "expected_file_count": 1,
    "expected_commit_count": 1,
    "expected_review_count": 0,
}
