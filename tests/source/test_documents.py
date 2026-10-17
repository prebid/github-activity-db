"""Query documents: normalization, the hydration-version lock, and point costs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from github_activity_db.source import documents
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
        lock = json.loads(LOCK.read_text())
        key = f"{documents.PULL_REQUEST_KIND}:{documents.PULL_REQUEST_HYDRATION_VERSION}"
        current = documents.document_hash(*documents.pull_request_documents())
        assert key in lock, f"add {key!r} to {LOCK.name}"
        assert lock[key] == current, (
            "the pull request documents changed: bump PULL_REQUEST_HYDRATION_VERSION and add "
            f"{documents.PULL_REQUEST_KIND}:<new version> = {current} to {LOCK.name}"
        )

    def test_lock_keys_name_a_kind_and_a_version(self) -> None:
        for key, digest in json.loads(LOCK.read_text()).items():
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
            (documents.overflow_continuation([documents.FILES] * 25), 1),
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
        scalars = documents.normalize(documents.PULL_REQUEST_FIELDS).split("author {")[0]
        assert " fullDatabaseId " in scalars
        assert " databaseId " not in scalars
