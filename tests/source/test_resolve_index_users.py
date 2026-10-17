"""Resolve, index, and user lookup against recorded responses (plan 5.3 steps 1-2, 5.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from github_activity_db.source import Account
from github_activity_db.source.errors import (
    IndexCountMismatch,
    IndexOrderViolation,
    LabelCountMismatch,
    RepositoryNotFound,
    RepositoryRequestFailed,
    RepositoryShapeError,
    RepositoryWithoutDefaultBranch,
    UserLookupFailed,
)
from github_activity_db.source.limits import INDEX_BOUNDARY_MARGIN
from tests.source.recordings import Unit, whole_stamp
from tests.source.replay import Replay, synthetic

DEPENDABOT = "MDM6Qm90NDk2OTkzMzM="
PATMMCCANN = "MDQ6VXNlcjE2ODMxNzU="
NO_SUCH_USER = "MDQ6VXNlcjk5OTk5OTk5OTk="


def operation(name: str):
    return lambda seen: seen.operation == name


class TestResolve:
    async def test_resolves_names_ids_and_labels(self, open_source) -> None:
        async with open_source() as source:
            repository = await source.resolve("prebid", "Prebid.js")
        assert repository.full_name == "prebid/Prebid.js"
        assert repository.github_id == 39321023
        assert repository.default_branch == "master"
        assert repository.is_private is False
        assert "pmc-review" in repository.label_names
        assert len(repository.label_names) == 47

    async def test_not_found(self, open_source, replay: Replay) -> None:
        def missing(body: dict) -> None:
            body["data"]["repository"] = None
            body["errors"] = [{"type": "NOT_FOUND", "path": ["repository"], "message": "x"}]

        replay.modify(missing, when=operation("Resolve"))
        async with open_source() as source:
            with pytest.raises(RepositoryNotFound):
                await source.resolve("prebid", "go-gdpr")

    async def test_without_a_default_branch(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["defaultBranchRef"] = None
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(RepositoryWithoutDefaultBranch):
                await source.resolve("prebid", "go-gdpr")

    async def test_an_unexpected_shape_fails_the_repository(
        self, open_source, replay: Replay
    ) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["isPrivate"] = "no"
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError):
                await source.resolve("prebid", "go-gdpr")

    async def test_labels_page_past_100(self, open_source, replay: Replay) -> None:
        # Modifies the recorded go-gdpr unit: one more label behind a cursor.
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        labels = unit.data["labels"]
        labels["totalCount"] = 11
        labels["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        replay.add(_label_page(unit, "page-2", ["needs triage"], total=11))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
        assert "needs triage" in repository.label_names
        assert len(repository.label_names) == 11

    async def test_label_pages_must_add_up(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["totalCount"] = 12
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        replay.add(_label_page(unit, "page-2", ["needs triage"], total=12))
        async with open_source() as source:
            with pytest.raises(LabelCountMismatch):
                await source.resolve("prebid", "go-gdpr")

    async def test_label_count_without_pages_must_match(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["totalCount"] = 11
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(LabelCountMismatch):
                await source.resolve("prebid", "go-gdpr")


def _label_page(resolve: Unit, cursor: str, names: list[str], *, total: int) -> Unit:
    from github_activity_db.source import documents

    return Unit(
        kind="repository_labels",
        key={"id": resolve.data["id"], "after": cursor},
        document_sha256=whole_stamp(documents.REPOSITORY_LABELS),
        recorded_at=resolve.recorded_at,
        headers=resolve.headers,
        data={
            "id": resolve.data["id"],
            "labels": {
                "totalCount": total,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"name": name} for name in names],
            },
        },
        source="synthetic: a second label page for the recorded go-gdpr resolve unit",
    )


class TestIndex:
    async def test_one_page(self, open_source, replay: Replay) -> None:
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            index = await source.index_pull_requests(repository)
        assert index.total_count == len(index.entries) == 37
        assert index.scans == 1
        first_page = replay.unit("index", owner="prebid", name="go-gdpr", after=None)
        date = datetime.strptime(first_page.headers["date"], "%a, %d %b %Y %H:%M:%S GMT")
        assert index.started_at == date.replace(tzinfo=UTC) - INDEX_BOUNDARY_MARGIN
        numbers = [entry.number for entry in index.entries]
        assert len(set(numbers)) == len(numbers)
        assert all(entry.created_at.tzinfo is not None for entry in index.entries)

    async def test_two_pages(self, open_source) -> None:
        async with open_source() as source:
            repository = await source.resolve("prebid", "prebid-cache-java")
            index = await source.index_pull_requests(repository)
        assert index.total_count == len(index.entries) == 157
        created = [entry.created_at for entry in index.entries]
        assert created == sorted(created)
        assert all(entry.github_id > 0 for entry in index.entries)

    async def test_a_mismatch_rescans_once(self, open_source, replay: Replay) -> None:
        def inflate(body: dict) -> None:
            body["data"]["repository"]["pullRequests"]["totalCount"] += 1

        replay.modify(inflate, when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            index = await source.index_pull_requests(repository)
        assert index.scans == 2
        assert index.total_count == 37

    async def test_a_second_mismatch_fails_the_repository(
        self, open_source, replay: Replay
    ) -> None:
        def inflate(body: dict) -> None:
            body["data"]["repository"]["pullRequests"]["totalCount"] += 1

        replay.modify(inflate, when=operation("PullRequestIndex"), times=2)
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            with pytest.raises(IndexCountMismatch) as raised:
                await source.index_pull_requests(repository)
        assert (raised.value.enumerated, raised.value.total_count) == (37, 38)

    async def test_the_last_page_total_is_the_oracle(self, open_source, replay: Replay) -> None:
        # A PR created during the scan sorts last: the first page's totalCount is one lower.
        def deflate(body: dict) -> None:
            body["data"]["repository"]["pullRequests"]["totalCount"] -= 1

        replay.modify(
            deflate,
            when=lambda seen: seen.operation == "PullRequestIndex"
            and seen.variables.get("after") is None,
        )
        async with open_source() as source:
            repository = await source.resolve("prebid", "prebid-cache-java")
            index = await source.index_pull_requests(repository)
        assert index.scans == 1

    async def test_a_duplicate_number_fails_the_repository(
        self, open_source, replay: Replay
    ) -> None:
        def duplicate(body: dict) -> None:
            nodes = body["data"]["repository"]["pullRequests"]["nodes"]
            nodes[1] = dict(nodes[0])

        replay.modify(duplicate, when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            with pytest.raises(IndexOrderViolation, match="listed twice"):
                await source.index_pull_requests(repository)

    async def test_creation_order_is_checked(self, open_source, replay: Replay) -> None:
        def reorder(body: dict) -> None:
            nodes = body["data"]["repository"]["pullRequests"]["nodes"]
            nodes[0], nodes[1] = nodes[1], nodes[0]

        replay.modify(reorder, when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            with pytest.raises(IndexOrderViolation, match="created before"):
                await source.index_pull_requests(repository)

    @pytest.mark.parametrize("path", [("nodes", 0), ("nodes", 0, "closingIssuesReferences")])
    async def test_a_null_entry_fails_the_repository(
        self, open_source, replay: Replay, path
    ) -> None:
        def null(body: dict) -> None:
            target = body["data"]["repository"]["pullRequests"]
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = None

        replay.modify(null, when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            with pytest.raises(RepositoryShapeError):
                await source.index_pull_requests(repository)

    async def test_a_failed_request_fails_the_repository(self, open_source, replay: Replay) -> None:
        replay.fault(
            lambda request: synthetic("server_error"),
            when=operation("PullRequestIndex"),
            times=None,
        )
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            with pytest.raises(RepositoryRequestFailed):
                await source.index_pull_requests(repository)


class TestUserLookup:
    async def test_a_commit_linked_bot_gets_its_real_type(self, open_source) -> None:
        async with open_source() as source:
            accounts = await source.lookup_users([DEPENDABOT, PATMMCCANN, NO_SUCH_USER, DEPENDABOT])
        assert accounts[DEPENDABOT] == Account(
            github_id=49699333, node_id=DEPENDABOT, login="dependabot", type="Bot"
        )
        assert accounts[PATMMCCANN] is not None and accounts[PATMMCCANN].type == "User"
        assert accounts[NO_SUCH_USER] is None
        assert len(accounts) == 3

    async def test_batches_of_100(self, open_source, replay: Replay) -> None:
        template = replay.unit("user", id=PATMMCCANN)
        ids = [f"synthetic-{i}" for i in range(150)]
        for number, node_id in enumerate(ids):
            unit = replay.unit("user", id=PATMMCCANN)
            unit.key = {"id": node_id}
            unit.data = {**template.data, "id": node_id, "databaseId": number + 1}
            unit.source = "synthetic: copies of the recorded patmmccann lookup with new ids"
            replay.add(unit)
        async with open_source() as source:
            accounts = await source.lookup_users(ids)
        assert len(accounts) == 150
        sizes = [
            len(seen.variables["ids"]) for seen in replay.seen if seen.operation == "UserLookup"
        ]
        assert sizes == [100, 50]

    async def test_an_error_other_than_not_found_fails_the_lookup(
        self, open_source, replay: Replay
    ) -> None:
        def forbid(body: dict) -> None:
            body["data"]["nodes"][0] = None
            body["errors"] = [{"type": "FORBIDDEN", "path": ["nodes", 0], "message": "no"}]

        replay.modify(forbid, when=operation("UserLookup"))
        async with open_source() as source:
            with pytest.raises(UserLookupFailed):
                await source.lookup_users([PATMMCCANN])

    async def test_a_failed_request_fails_the_lookup(self, open_source, replay: Replay) -> None:
        replay.fault(
            lambda request: synthetic("server_error"), when=operation("UserLookup"), times=None
        )
        async with open_source() as source:
            with pytest.raises(UserLookupFailed):
                await source.lookup_users([PATMMCCANN])
