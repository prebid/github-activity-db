"""Query documents: normalization, the hydration-version lock, and point costs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from github_activity_db.source import documents
from github_activity_db.source.limits import OVERFLOW_ALIASES_PER_REQUEST
from tests.data_dictionary import lock_checker
from tests.source.graphql_cost import cost

LOCK = Path(__file__).resolve().parent.parent / "query_versions.json"


class TestNormalize:
    def test_collapses_whitespace_and_drops_comments(self) -> None:
        text = "query  X {\n  # a comment\n  field\t{ a }\n}\n"
        assert documents.normalize(text) == "query X { field { a } }"

    def test_hash_ignores_layout_but_not_fields(self) -> None:
        base = documents.document_hash("query { a b }")
        assert documents.document_hash("query {\n  a\n  b\n}") == base
        assert documents.document_hash("query { a c }") != base
        assert documents.document_hash("query { ab }") != base


class TestHydrationVersionLock:
    """``tests/query_versions.json`` ties each kind's version to its documents (plan Step 2)."""

    def test_current_documents_match_the_locked_hash(self) -> None:
        lock = lock_checker().digests(LOCK.read_text(), str(LOCK))
        key = f"{documents.PULL_REQUEST_KIND}:{documents.PULL_REQUEST_HYDRATION_VERSION}"
        current = documents.document_hash(*documents.pull_request_documents())
        assert key in lock, f"add {key!r} to {LOCK.name}"
        assert lock[key] == current, (
            "the pull request documents changed: bump PULL_REQUEST_HYDRATION_VERSION and add "
            f"{documents.PULL_REQUEST_KIND}:<new version> = {current} to {LOCK.name}"
        )

    def test_lock_keys_name_a_kind_and_a_version(self) -> None:
        for key, digest in lock_checker().digests(LOCK.read_text(), str(LOCK)).items():
            kind, version = key.split(":")
            assert kind == documents.PULL_REQUEST_KIND
            assert int(version) >= 1
            assert len(digest) == 64

    def test_the_version_covers_every_document_the_kind_sends(self) -> None:
        covered = documents.pull_request_documents()
        assert documents.pull_request_batch(1) in covered
        for connection in documents.PULL_REQUEST_CONNECTIONS:
            assert documents.overflow_continuation([connection]) in covered
        assert len(covered) == 1 + len(documents.PULL_REQUEST_CONNECTIONS)


class TestCost:
    """Costs from GitHub's documented formula equal the figures in plan 5.8 and section 7."""

    @pytest.mark.parametrize(
        ("document", "points"),
        [
            (documents.pull_request_batch(25), 2),  # "25 aliases cost 2 points"
            (documents.PULL_REQUEST_INDEX, 1),  # "cost 1 point per 100 items"
            (documents.RESOLVE, 1),
            (documents.REPOSITORY_LABELS, 1),
            (documents.USER_LOOKUP, 1),
            *(
                (documents.overflow_continuation([connection] * 25), 1)
                for connection in documents.PULL_REQUEST_CONNECTIONS
            ),
            # 25 aliases across every connection: still 1 point.
            (
                documents.overflow_continuation(
                    ([*documents.PULL_REQUEST_CONNECTIONS] * 3)[:OVERFLOW_ALIASES_PER_REQUEST]
                ),
                1,
            ),
        ],
    )
    def test_document_cost(self, document: str, points: int) -> None:
        assert cost(document) == points

    def test_a_nested_connection_inside_commits_costs_27(self) -> None:
        # D15 measured 27 points for the same 25 PRs with authors(first: 5) inside commits.
        nested = documents.pull_request_batch(25).replace(
            "commit { oid authoredDate",
            "commit { authors(first: 5) { nodes { name } } oid authoredDate",
        )
        assert nested != documents.pull_request_batch(25)
        assert cost(nested) == 27


class TestDocuments:
    def test_actor_fragment_only_where_it_is_used(self) -> None:
        # GitHub rejects a document that defines a fragment it does not use.
        assert "fragment Actor" not in documents.overflow_continuation([documents.FILES])
        assert "fragment Actor" in documents.overflow_continuation([documents.REVIEWS])
        assert "fragment Actor" in documents.pull_request_batch(1)

    def test_reviews_exclude_pending(self) -> None:
        assert "PENDING" not in documents.REVIEWS.selection()
        assert "states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]" in (
            documents.REVIEWS.selection()
        )

    def test_item_ids_use_full_database_id(self) -> None:
        # PullRequest.databaseId and PullRequestReview.databaseId are deprecated (32-bit).
        assert "fullDatabaseId" in documents.REVIEWS.nodes
        assert "fullDatabaseId" in documents.PULL_REQUEST_INDEX
        top = top_level_fields(documents.PULL_REQUEST_FIELDS)
        assert "fullDatabaseId" in top
        assert "databaseId" not in top
        assert top_level_fields("fragment F on X { a b { databaseId } c(first: 1) { d } }") == [
            "a",
            "b",
            "c",
        ]

    def test_the_user_linked_continuation_keeps_its_filter(self) -> None:
        """The aliased connection continues with userLinkedOnly; the full list never has it."""
        user_linked = documents.normalize(
            documents.overflow_continuation([documents.USER_LINKED_CLOSING_ISSUES])
        )
        assert (
            "userLinkedClosingIssues: closingIssuesReferences(first: 100, after: $after0, "
            "userLinkedOnly: true)" in user_linked
        )
        full = documents.normalize(documents.overflow_continuation([documents.CLOSING_ISSUES]))
        assert "closingIssuesReferences(first: 100, after: $after0)" in full
        assert "userLinkedOnly" not in full
        assert documents.normalize(documents.PULL_REQUEST_FIELDS).count("userLinkedOnly") == 1

    def test_the_documents_select_the_plans_pull_request_fields(self) -> None:
        """The fields plan 5.8 lists for Step 3b-1, every one at the pull request's top level."""
        assert (
            top_level_fields(documents.PULL_REQUEST_FIELDS)
            == (
                "id number fullDatabaseId state isDraft title body createdAt updatedAt mergedAt "
                "closedAt lastEditedAt baseRefName baseRefOid headRefName headRefOid "
                "isCrossRepository authorAssociation headRepository mergeCommit milestone "
                "reviewDecision totalCommentsCount publishedAt includesCreatedEdit "
                "maintainerCanModify headRepositoryOwner mergeable mergeStateStatus canBeRebased "
                "potentialMergeCommit "
                "stack stackEntry additions deletions changedFiles author mergedBy editor labels "
                "closingIssuesReferences userLinkedClosingIssues reviews comments commits files "
                "reviewRequests assignedActors"
            ).split()
        )


def top_level_fields(fragment: str) -> list[str]:
    """Field names (an alias where given) directly inside a fragment's outer braces."""
    text = documents.normalize(fragment)
    body = text[text.index("{") + 1 : text.rindex("}")]
    names: list[str] = []
    depth = 0
    for token in re.findall(r"[{}()]|[A-Za-z_][A-Za-z0-9_]*:?", body):
        if token in "{(":
            depth += 1
        elif token in "})":
            depth -= 1
        elif depth == 0:
            if names and names[-1].endswith(":"):
                names[-1] = names[-1][:-1]  # an alias names the field in the response
            else:
                names.append(token)
    return names


@pytest.mark.parametrize(
    ("connections", "fragment"),
    [
        ((documents.LABELS, documents.COMMENTS), True),
        ((documents.FILES, documents.REVIEWS, documents.COMMITS), True),
        ((documents.LABELS, documents.FILES, documents.CLOSING_ISSUES), False),
    ],
    ids=["labels-and-comments", "files-reviews-commits", "no-actor-connection"],
)
def test_a_mixed_continuation_defines_the_actor_fragment_only_when_used(
    connections: tuple[documents.Connection, ...], fragment: bool
) -> None:
    # GitHub rejects a document that defines a fragment it does not use, or uses one it does
    # not define (plan section 7, "Unused fragments").
    document = documents.overflow_continuation(list(connections))
    assert ("fragment Actor on Actor" in document) is fragment
    assert ("...Actor" in document) is fragment
