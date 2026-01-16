"""Real merged PR fixture data from prebid/prebid-server.

Captured from PR #4646: Optidigital: Add GPP support to user sync
State: MERGED (closed with merge)

This fixture represents a fully processed PR with:
- Merged via GitHub (has merged_by, merged_at)
- Multiple commits (12 total, including branch history)
- Multiple reviews (2 approvals)
- Labels applied
- Assignees assigned
"""

REAL_MERGED_PR = {
    "pr": {
        "number": 4646,
        "html_url": "https://github.com/prebid/prebid-server/pull/4646",
        "state": "closed",
        "title": "Optidigital: Add GPP support to user sync",
        "body": "Adds GPP and GPP_SID macros.",
        "user": {
            "login": "optidigital-prebid",
            "id": 124287395,
            "type": "User",
        },
        "merged_by": {
            "login": "bsardo",
            "id": 1168933,
            "type": "User",
        },
        "created_at": "2025-12-23T14:33:51Z",
        "updated_at": "2026-01-11T03:24:31Z",
        "closed_at": "2026-01-11T03:24:31Z",
        "merged_at": "2026-01-11T03:24:31Z",
        "merged": True,
        "commits": 12,
        "additions": 1,
        "deletions": 1,
        "changed_files": 1,
        "labels": [
            {
                "id": 7969327065,
                "name": "adapter",
                "color": "BAF1E0",
                "description": "",
            }
        ],
        "requested_reviewers": [],
        "assignees": [
            {
                "login": "bsardo",
                "id": 1168933,
                "type": "User",
            },
            {
                "login": "ccorbo",
                "id": 19716777,
                "type": "User",
            },
        ],
    },
    "files": [
        {
            "sha": "30074e3a6f173279c7b6d4fdf8d60e0aa1ea3934",
            "filename": "static/bidder-info/optidigital.yaml",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
        }
    ],
    "commits": [
        {
            "sha": "2d621eb233af83e82b550f1d8f0305b308fe0d4d",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-03-31T07:42:39Z",
                },
                "message": "optidigital adapter",
            },
        },
        {
            "sha": "397502772ec4784fff683739d27c974f213b562b",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-04-01T14:39:34Z",
                },
                "message": "add `divId` parameter as optional",
            },
        },
        {
            "sha": "90b249d4b3564f9e13d0d1ca63ac3190e50fe99c",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-04-01T15:16:00Z",
                },
                "message": "protect strings in yaml just-in-case",
            },
        },
        {
            "sha": "67d0194a3f851caff417808235f4813bcc26770b",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-04-01T15:20:26Z",
                },
                "message": "remove un-used errors slide",
            },
        },
        {
            "sha": "46598d0990624df93d4bc690551d6defabaaf5e0",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-04-02T13:20:31Z",
                },
                "message": "validate params",
            },
        },
        {
            "sha": "5f665898fec86d6151010c7fca713b12ebb9b164",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-04-02T13:45:40Z",
                },
                "message": "[skip ci] fix comment",
            },
        },
        {
            "sha": "c27f8bd200cfa8b9995a39d50d92ff24bd97efeb",
            "commit": {
                "author": {
                    "name": "optidigital-prebid",
                    "email": "124287395+optidigital-prebid@users.noreply.github.com",
                    "date": "2025-04-02T13:47:16Z",
                },
                "message": (
                    "Merge pull request #2 from optidigital-prebid/adapter/optidigital\n\n"
                    "optidigital PBS adapter"
                ),
            },
        },
        {
            "sha": "cee3787d25a5e88cd99c1d8eb7f2195331fb8310",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-04-14T06:38:37Z",
                },
                "message": "normalize checks",
            },
        },
        {
            "sha": "05463ca311a42e52248173d583418ab1cbfdaf41",
            "commit": {
                "author": {
                    "name": "Victor Gonzalez",
                    "email": "victor@optidigital.com",
                    "date": "2025-04-14T06:38:57Z",
                },
                "message": "add bad_response test case",
            },
        },
        {
            "sha": "8b269edf4d60983aa56831c824fa723657108122",
            "commit": {
                "author": {
                    "name": "optidigital-prebid",
                    "email": "124287395+optidigital-prebid@users.noreply.github.com",
                    "date": "2025-04-14T09:46:39Z",
                },
                "message": (
                    "Merge pull request #3 from optidigital-prebid/adapter/optidigital\n\n"
                    "Adapter/Optidigital"
                ),
            },
        },
        {
            "sha": "a1a00837edc7577415e41c54541981b7faabfb45",
            "commit": {
                "author": {
                    "name": "optidigital-prebid",
                    "email": "124287395+optidigital-prebid@users.noreply.github.com",
                    "date": "2025-12-23T14:07:33Z",
                },
                "message": "Merge branch 'prebid:master' into master",
            },
        },
        {
            "sha": "ba18179c1f66550067e85b6356cdb1c51b0a5948",
            "commit": {
                "author": {
                    "name": "optidigital-prebid",
                    "email": "124287395+optidigital-prebid@users.noreply.github.com",
                    "date": "2025-12-23T14:29:44Z",
                },
                "message": "add GPP macros",
            },
        },
    ],
    "reviews": [
        {
            "id": 3628161311,
            "user": {
                "login": "ccorbo",
                "id": 19716777,
                "type": "User",
            },
            "state": "APPROVED",
            "submitted_at": "2026-01-05T19:31:15Z",
        },
        {
            "id": 3647208230,
            "user": {
                "login": "bsardo",
                "id": 1168933,
                "type": "User",
            },
            "state": "APPROVED",
            "submitted_at": "2026-01-10T21:23:57Z",
        },
    ],
}

# Metadata for test assertions
MERGED_PR_METADATA = {
    "repository": {
        "owner": "prebid",
        "name": "prebid-server",
    },
    "expected_state": "closed",
    "expected_merged": True,
    "expected_merged_by": "bsardo",
    "expected_file_count": 1,
    "expected_commit_count": 12,
    "expected_review_count": 2,
    "expected_reviewer_actions": {
        "ccorbo": "APPROVED",
        "bsardo": "APPROVED",
    },
}
