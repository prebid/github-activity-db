"""Resolve, index, and user lookup against recorded responses (plan 5.3 steps 1-2, 5.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from github_activity_db.source import Account, ClosingIssue, IndexedPullRequest
from github_activity_db.source.errors import (
    IndexCountMismatch,
    IndexOrderViolation,
    LabelCountMismatch,
    LookupShapeError,
    RepositoryNotFound,
    RepositoryRequestFailed,
    RepositoryShapeError,
    RepositoryWithoutDefaultBranch,
    ServerError,
)
from github_activity_db.source.limits import INDEX_BOUNDARY_MARGIN, SERVER_RETRY_DELAYS
from tests.source.conftest import FakeClock, all_accounts, all_lookups
from tests.source.recordings import Unit
from tests.source.replay import Replay, synthetic

DEPENDABOT = "MDM6Qm90NDk2OTkzMzM="
PATMMCCANN = "MDQ6VXNlcjE2ODMxNzU="
NO_SUCH_USER = "MDQ6VXNlcjk5OTk5OTk5OTk="


def operation(name: str):
    return lambda seen: seen.operation == name


def parsed_date(unit: Unit) -> datetime:
    return datetime.strptime(unit.headers["date"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)


def recorded(kind: str) -> list[Unit]:
    return [unit for unit in Replay.from_fixtures().units.values() if unit.kind == kind]


def labelled(unit: Unit) -> str:
    return "/".join(str(value) for value in unit.key.values())


GO = ("golang", "go")


def label_page(replay: Replay) -> Unit:
    """golang/go's recorded second label page, to modify and add back."""
    resolve = replay.unit("resolve", owner="golang", name="go").data
    return replay.unit(
        "repository_labels", id=resolve["id"], after=resolve["labels"]["pageInfo"]["endCursor"]
    )


class TestResolve:
    async def test_resolves_names_ids_and_labels(self, open_source, replay: Replay) -> None:
        async with open_source() as source:
            repository = await source.resolve("prebid", "Prebid.js")
        assert repository.full_name == "prebid/Prebid.js"
        assert repository.github_id == 39321023
        assert repository.default_branch == "master"
        assert repository.is_private is False
        assert "pmc-review" in repository.label_names
        recorded = replay.unit("resolve", owner="prebid", name="Prebid.js").data["labels"]
        assert len(repository.label_names) == recorded["totalCount"] == len(recorded["nodes"])

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

    async def test_errors_under_the_repository_fail_it(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.errors = [{"type": "SERVICE_UNAVAILABLE", "path": ["labels"], "message": "partial"}]
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="errors under repository"):
                await source.resolve("prebid", "go-gdpr")

    async def test_a_null_label_entry(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["nodes"][0] = None
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="null label entry"):
                await source.resolve("prebid", "go-gdpr")

    async def test_label_count_without_pages_must_match(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["totalCount"] = 11
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(LabelCountMismatch):
                await source.resolve("prebid", "go-gdpr")

    async def test_a_first_label_page_without_a_cursor(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": None}
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="no progress"):
                await source.resolve("prebid", "go-gdpr")

    async def test_a_null_repository_without_not_found(self, open_source, replay: Replay) -> None:
        def nothing(body: dict[str, Any]) -> None:
            body["data"]["repository"] = None

        replay.modify(nothing, when=operation("Resolve"))
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="repository is null: no errors"):
                await source.resolve("prebid", "go-gdpr")


class TestLabelPages:
    """Labels past the first 100, on golang/go's recorded second page (144 labels)."""

    async def test_labels_page_past_100(self, open_source, replay: Replay) -> None:
        first = replay.unit("resolve", owner="golang", name="go").data["labels"]
        second = label_page(replay).data["labels"]
        async with open_source() as source:
            repository = await source.resolve(*GO)
        names = [node["name"] for node in first["nodes"] + second["nodes"]]
        assert repository.label_names == frozenset(names)
        assert len(names) == first["totalCount"] == 144
        (request,) = [seen for seen in replay.seen if seen.operation == "RepositoryLabels"]
        assert request.variables["after"] == first["pageInfo"]["endCursor"]

    async def test_label_pages_must_add_up(self, open_source, replay: Replay) -> None:
        resolve = replay.unit("resolve", owner="golang", name="go")
        resolve.data["labels"]["totalCount"] = 145
        replay.add(resolve)
        page = label_page(replay)
        page.data["labels"]["totalCount"] = 145
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(LabelCountMismatch) as raised:
                await source.resolve(*GO)
        assert (raised.value.fetched, raised.value.total_count) == (144, 145)

    async def test_a_label_page_for_another_repository(self, open_source, replay: Replay) -> None:
        page = label_page(replay)
        page.data["id"] = "R_another"
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="label page for R_another"):
                await source.resolve(*GO)

    async def test_a_label_total_that_changes_between_pages(
        self, open_source, replay: Replay
    ) -> None:
        page = label_page(replay)
        page.data["labels"]["totalCount"] = 145
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(
                RepositoryShapeError, match="label totalCount changed from 144 to 145"
            ):
                await source.resolve(*GO)

    @pytest.mark.parametrize("where", ["nodes", "entry"])
    async def test_a_null_on_a_later_page(self, open_source, replay: Replay, where: str) -> None:
        page = label_page(replay)
        if where == "nodes":
            page.data["labels"]["nodes"] = None
        else:
            page.data["labels"]["nodes"][0] = None
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="null label entry"):
                await source.resolve(*GO)

    async def test_a_label_page_the_models_reject(self, open_source, replay: Replay) -> None:
        page = label_page(replay)
        page.data = None
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="unexpected label page"):
                await source.resolve(*GO)

    @pytest.mark.parametrize("cursor", [None, "MTAw"], ids=["missing", "repeated"])
    async def test_a_later_page_without_a_new_cursor(
        self, open_source, replay: Replay, cursor: str | None
    ) -> None:
        # Once only: a resolver that followed a repeated cursor would read the page again and
        # fail on the label count instead of looping.
        def stall(body: dict[str, Any]) -> None:
            body["data"]["node"]["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": cursor}

        replay.modify(stall, when=operation("RepositoryLabels"))
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="no progress"):
                await source.resolve(*GO)

    async def test_a_later_page_with_a_next_page_but_no_labels(
        self, open_source, replay: Replay
    ) -> None:
        # Once only, as above; a resolver following empty pages with fresh cursors never ends.
        def empty(body: dict[str, Any]) -> None:
            labels = body["data"]["node"]["labels"]
            labels["nodes"] = []
            labels["pageInfo"] = {"hasNextPage": True, "endCursor": "fresh"}

        replay.modify(empty, when=operation("RepositoryLabels"))
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="no progress"):
                await source.resolve(*GO)

    async def test_errors_on_a_label_page_fail_the_repository(
        self, open_source, replay: Replay
    ) -> None:
        def error(body: dict[str, Any]) -> None:
            body["errors"] = [
                {"type": "SERVICE_UNAVAILABLE", "path": ["node", "labels"], "message": "x"}
            ]

        replay.modify(error, when=operation("RepositoryLabels"))
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="errors on a label page"):
                await source.resolve(*GO)

    async def test_more_labels_than_the_total_stop_paging(
        self, open_source, replay: Replay
    ) -> None:
        # One label over the total and a further page: the resolver stops instead of reading
        # a page the replay does not have.
        page = label_page(replay)
        labels = page.data["labels"]
        labels["nodes"].append({"name": "one too many"})
        labels["pageInfo"] = {"hasNextPage": True, "endCursor": "MTQ1"}
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(LabelCountMismatch) as raised:
                await source.resolve(*GO)
        assert (raised.value.fetched, raised.value.total_count) == (145, 144)


@pytest.mark.parametrize("unit", recorded("resolve"), ids=labelled)
async def test_resolve_maps_every_field_from_its_wire_name(open_source, unit: Unit) -> None:
    data = unit.data
    async with open_source() as source:
        repository = await source.resolve(unit.key["owner"], unit.key["name"])
    assert (
        repository.github_id,
        repository.node_id,
        repository.owner,
        repository.name,
        repository.full_name,
        repository.is_private,
        repository.is_archived,
        repository.default_branch,
    ) == (
        data["databaseId"],
        data["id"],
        data["owner"]["login"],
        data["name"],
        data["nameWithOwner"],
        data["isPrivate"],
        data["isArchived"],
        data["defaultBranchRef"]["name"],
    )
    names = [node["name"] for node in data["labels"]["nodes"]]
    names += [
        node["name"]
        for page in recorded("repository_labels")
        if page.key["id"] == data["id"]
        for node in page.data["labels"]["nodes"]
    ]
    assert repository.label_names == frozenset(names)
    assert len(names) == data["labels"]["totalCount"]


@pytest.mark.parametrize(("field", "value"), [("isPrivate", True), ("isArchived", True)])
async def test_resolve_reports_private_and_archived(
    open_source, replay: Replay, field: str, value: bool
) -> None:
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    unit.data[field] = value
    replay.add(unit)
    async with open_source() as source:
        repository = await source.resolve("prebid", "go-gdpr")
    assert (repository.is_private, repository.is_archived) == (
        field == "isPrivate",
        field == "isArchived",
    )


class TestIndex:
    async def test_one_page(self, open_source, replay: Replay) -> None:
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            index = await source.index_pull_requests(repository)
        first_page = replay.unit("index", owner="prebid", name="go-gdpr", after=None)
        assert index.total_count == len(index.entries) == first_page.data["totalCount"]
        assert index.scans == 1
        assert index.started_at == parsed_date(first_page) - INDEX_BOUNDARY_MARGIN
        numbers = [entry.number for entry in index.entries]
        assert len(set(numbers)) == len(numbers)
        assert all(entry.created_at.tzinfo is not None for entry in index.entries)

    async def test_two_pages(self, open_source, replay: Replay) -> None:
        async with open_source() as source:
            repository = await source.resolve("prebid", "prebid-cache-java")
            index = await source.index_pull_requests(repository)
        first_page = replay.unit("index", owner="prebid", name="prebid-cache-java", after=None)
        assert 100 < index.total_count == len(index.entries)
        assert index.started_at == parsed_date(first_page) - INDEX_BOUNDARY_MARGIN
        created = [entry.created_at for entry in index.entries]
        assert created == sorted(created)
        assert all(entry.github_id > 0 for entry in index.entries)

    async def test_a_mismatch_rescans_once(self, open_source, replay: Replay) -> None:
        def inflate(body: dict) -> None:
            body["data"]["repository"]["pullRequests"]["totalCount"] += 1

        first_page = replay.unit("index", owner="prebid", name="go-gdpr", after=None)
        # The failed scan's page answered earlier; the boundary comes from the completed scan.
        earlier = format_datetime(parsed_date(first_page) - timedelta(minutes=5), usegmt=True)
        replay.modify(inflate, when=operation("PullRequestIndex"), headers={"date": earlier})
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            index = await source.index_pull_requests(repository)
        assert index.scans == 2
        assert index.total_count == len(index.entries)
        assert index.started_at == parsed_date(first_page) - INDEX_BOUNDARY_MARGIN

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

    @pytest.mark.parametrize(
        "change",
        [
            lambda page: page.update(nodes=None),
            lambda page: page["nodes"].__setitem__(0, None),
            lambda page: page["nodes"][0].update(closingIssuesReferences=None),
            lambda page: page["nodes"][0].update(
                closingIssuesReferences={"totalCount": 1, "nodes": [None]}
            ),
            lambda page: page["nodes"][0].update(
                closingIssuesReferences={"totalCount": 1, "nodes": None}
            ),
        ],
        ids=["nodes", "entry", "links", "link-entry", "link-nodes"],
    )
    async def test_a_null_entry_fails_the_repository(
        self, open_source, replay: Replay, change
    ) -> None:
        replay.modify(
            lambda body: change(body["data"]["repository"]["pullRequests"]),
            when=operation("PullRequestIndex"),
        )
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

    @pytest.mark.parametrize(
        ("page", "next_cursor"),
        [("first", None), ("second", None), ("second", "first")],
        ids=["first-page-missing", "second-page-missing", "second-page-repeated"],
    )
    async def test_a_next_page_without_a_new_cursor(
        self, open_source, replay: Replay, page: str, next_cursor: str | None
    ) -> None:
        first_page = replay.unit("index", owner="prebid", name="prebid-cache-java", after=None)
        first_cursor = first_page.data["pageInfo"]["endCursor"]
        after = None if page == "first" else first_cursor
        stalled = first_cursor if next_cursor == "first" else None

        # Once only: an index that followed a repeated cursor would read the recorded page
        # again and fail on a duplicate instead of looping.
        def stall(body: dict[str, Any]) -> None:
            page_info = body["data"]["repository"]["pullRequests"]["pageInfo"]
            page_info.update(hasNextPage=True, endCursor=stalled)

        replay.modify(
            stall,
            when=lambda seen: seen.operation == "PullRequestIndex"
            and seen.variables.get("after") == after,
        )
        async with open_source() as source:
            repository = await source.resolve("prebid", "prebid-cache-java")
            with pytest.raises(RepositoryShapeError, match="no progress"):
                await source.index_pull_requests(repository)

    async def test_a_next_page_with_no_entries(self, open_source, replay: Replay) -> None:
        # Once only: an index following empty pages with fresh cursors would never end.
        def empty(body: dict[str, Any]) -> None:
            page = body["data"]["repository"]["pullRequests"]
            page["nodes"] = []
            page["pageInfo"] = {"hasNextPage": True, "endCursor": "fresh"}

        replay.modify(empty, when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "prebid-cache-java")
            with pytest.raises(RepositoryShapeError, match="no progress"):
                await source.index_pull_requests(repository)

    @pytest.mark.parametrize(
        ("field", "value"),
        [("state", "QUEUED"), ("fullDatabaseId", 187290288), ("isDraft", "no")],
    )
    async def test_an_index_page_the_models_reject(
        self, open_source, replay: Replay, field: str, value: object
    ) -> None:
        def change(body: dict[str, Any]) -> None:
            body["data"]["repository"]["pullRequests"]["nodes"][0][field] = value

        replay.modify(change, when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            with pytest.raises(RepositoryShapeError, match="unexpected index page"):
                await source.index_pull_requests(repository)

    async def test_an_index_timeout_is_retried(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("bad_gateway"), when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            index = await source.index_pull_requests(repository)
        assert index.total_count == len(index.entries)
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]

    async def test_a_duplicate_closing_link_makes_the_entry_incomplete(
        self, open_source, replay: Replay
    ) -> None:
        # Kept once, so the entry is shorter than its total and the PR is hydrated, where the
        # duplicate fails the item; a duplicate never passes for a complete list.
        def duplicate(body: dict[str, Any]) -> None:
            for node in body["data"]["repository"]["pullRequests"]["nodes"]:
                links = node["closingIssuesReferences"]
                if links["nodes"]:
                    first = links["nodes"][0]
                    repository = {**first["repository"], "nameWithOwner": "prebid/renamed"}
                    links["nodes"].append({**first, "repository": repository})
                    links["totalCount"] += 1
                    return
            raise AssertionError("no indexed PR with a closing link")

        replay.modify(duplicate, when=operation("PullRequestIndex"))
        async with open_source() as source:
            index = await source.index_pull_requests(await source.resolve("prebid", "go-gdpr"))
        (entry,) = [entry for entry in index.entries if entry.closing_issues_total == 2]
        assert len(entry.closing_issues) == 1
        assert entry.closing_issues[0].repo_full_name != "prebid/renamed"

    async def test_errors_in_an_index_page_fail_the_repository(
        self, open_source, replay: Replay
    ) -> None:
        def error(body: dict) -> None:
            body["errors"] = [
                {
                    "type": "SERVICE_UNAVAILABLE",
                    "path": ["repository", "pullRequests"],
                    "message": "x",
                }
            ]

        replay.modify(error, when=operation("PullRequestIndex"))
        async with open_source() as source:
            repository = await source.resolve("prebid", "go-gdpr")
            with pytest.raises(RepositoryShapeError, match="index errors"):
                await source.index_pull_requests(repository)


@pytest.mark.parametrize(
    "repository", sorted({(unit.key["owner"], unit.key["name"]) for unit in recorded("index")})
)
async def test_index_maps_every_field_from_its_wire_name(
    open_source, replay: Replay, repository: tuple[str, str]
) -> None:
    owner, name = repository
    pages = [unit for unit in replay.units.values() if unit.kind == "index"]
    nodes = [
        node
        for unit in pages
        if (unit.key["owner"], unit.key["name"]) == repository
        for node in unit.data["nodes"]
    ]
    async with open_source() as source:
        index = await source.index_pull_requests(await source.resolve(owner, name))
    expected = sorted(
        (
            IndexedPullRequest(
                number=node["number"],
                github_id=int(node["fullDatabaseId"]),
                state=node["state"],
                is_draft=node["isDraft"],
                author_association=node["authorAssociation"],
                created_at=datetime.fromisoformat(node["createdAt"]),
                updated_at=datetime.fromisoformat(node["updatedAt"]),
                merged_at=datetime.fromisoformat(node["mergedAt"]) if node["mergedAt"] else None,
                closing_issues=tuple(
                    ClosingIssue(
                        repo_github_id=link["repository"]["databaseId"],
                        repo_full_name=link["repository"]["nameWithOwner"],
                        number=link["number"],
                    )
                    for link in node["closingIssuesReferences"]["nodes"]
                ),
                closing_issues_total=node["closingIssuesReferences"]["totalCount"],
            )
            for node in nodes
        ),
        key=lambda entry: entry.number,
    )
    assert sorted(index.entries, key=lambda entry: entry.number) == expected
    assert index.repository == f"{owner}/{name}"


def synthetic_users(replay: Replay, count: int) -> list[str]:
    """``count`` accounts served under new ids, copied from the recorded patmmccann lookup."""
    template = replay.unit("user", id=PATMMCCANN)
    ids = [f"synthetic-{i}" for i in range(count)]
    for number, node_id in enumerate(ids):
        unit = replay.unit("user", id=PATMMCCANN)
        unit.key = {"id": node_id}
        unit.data = {**template.data, "id": node_id, "databaseId": number + 1}
        unit.source = "synthetic: copies of the recorded patmmccann lookup with new ids"
        replay.add(unit)
    return ids


class TestUserLookup:
    async def test_a_commit_linked_bot_gets_its_real_type(
        self, open_source, replay: Replay
    ) -> None:
        async with open_source() as source:
            accounts = await all_accounts(
                source, [DEPENDABOT, PATMMCCANN, NO_SUCH_USER, DEPENDABOT]
            )
        assert accounts[DEPENDABOT] == Account(
            github_id=49699333, node_id=DEPENDABOT, login="dependabot", type="Bot"
        )
        patmmccann = accounts[PATMMCCANN]
        assert patmmccann is not None
        assert patmmccann.type == "User"
        assert accounts[NO_SUCH_USER] is None
        assert len(accounts) == 3
        (request,) = [seen for seen in replay.seen if seen.operation == "UserLookup"]
        assert request.variables["ids"] == [DEPENDABOT, PATMMCCANN, NO_SUCH_USER]

    async def test_batches_of_100(self, open_source, replay: Replay) -> None:
        ids = synthetic_users(replay, 150)
        async with open_source() as source:
            accounts = await all_accounts(source, ids)
        assert len(accounts) == 150
        sizes = [
            len(seen.variables["ids"]) for seen in replay.seen if seen.operation == "UserLookup"
        ]
        assert sizes == [100, 50]

    async def test_an_error_on_one_node_fails_that_id_only(
        self, open_source, replay: Replay
    ) -> None:
        def forbid(body: dict[str, Any]) -> None:
            body["data"]["nodes"][0] = None
            body["errors"] = [{"type": "FORBIDDEN", "path": ["nodes", 0], "message": "no"}]

        replay.modify(forbid, when=operation("UserLookup"))
        async with open_source() as source:
            accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
        assert list(failed) == [PATMMCCANN]
        assert isinstance(failed[PATMMCCANN].cause, LookupShapeError)
        assert list(accounts) == [DEPENDABOT]

    @pytest.mark.parametrize(
        "change",
        [
            lambda body: body.setdefault("errors", []).append(
                {"type": "SERVICE_UNAVAILABLE", "path": ["nodes", 0, "login"], "message": "x"}
            ),
            lambda body: body["data"]["nodes"].__setitem__(
                0, {"__typename": "PullRequest", "id": PATMMCCANN}
            ),
            lambda body: body["data"]["nodes"][0].update(id="MDQ6VXNlcjE="),
        ],
        ids=["partial-data", "not-an-actor", "another-id"],
    )
    async def test_a_bad_answer_for_one_node_fails_that_id_only(
        self, open_source, replay: Replay, change
    ) -> None:
        replay.modify(change, when=operation("UserLookup"))
        async with open_source() as source:
            accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
        assert list(failed) == [PATMMCCANN]
        assert list(accounts) == [DEPENDABOT]

    async def test_a_null_node_without_an_error_fails_that_id(
        self, open_source, replay: Replay
    ) -> None:
        # Only NOT_FOUND means deleted (D28); an unexplained null is not proof of deletion.
        def unexplained(body: dict[str, Any]) -> None:
            body["data"]["nodes"][0] = None

        replay.modify(unexplained, when=operation("UserLookup"))
        async with open_source() as source:
            accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
        assert list(failed) == [PATMMCCANN]
        assert list(accounts) == [DEPENDABOT]

    @pytest.mark.parametrize(
        "path",
        [["nodes", 0, None], ["nodes", 0, {}], ["nodes", 0, 1.5], ["nodes", True]],
        ids=["null", "object", "float", "bool-index"],
    )
    async def test_a_not_found_at_a_malformed_path_is_not_deleted(
        self, open_source, replay: Replay, path: list[Any]
    ) -> None:
        # Trimmed, these would read as ["nodes", 0], NOT_FOUND on the node: a deleted account.
        def malformed(body: dict[str, Any]) -> None:
            body["data"]["nodes"][0] = None
            body["errors"] = [{"type": "NOT_FOUND", "path": path, "message": "Missing resource"}]

        replay.modify(malformed, when=operation("UserLookup"), times=None)
        async with open_source() as source:
            accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
        assert accounts == {}
        assert set(failed) == {PATMMCCANN, DEPENDABOT}
        assert all(isinstance(error.cause, ServerError) for error in failed.values())

    async def test_a_not_found_below_a_null_node_is_not_deleted(
        self, open_source, replay: Replay
    ) -> None:
        # Deleted needs NOT_FOUND on the node itself, as Gone does for a pull request.
        def deeper(body: dict[str, Any]) -> None:
            body["data"]["nodes"][0] = None
            body["errors"] = [{"type": "NOT_FOUND", "path": ["nodes", 0, "login"], "message": "x"}]

        replay.modify(deeper, when=operation("UserLookup"))
        async with open_source() as source:
            accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
        assert list(failed) == [PATMMCCANN]
        assert list(accounts) == [DEPENDABOT]

    @pytest.mark.parametrize(
        "path",
        [["elsewhere"], ["nodes"], ["nodes", 7], ["nodes", "x"]],
        ids=["outside-nodes", "the-list", "past-the-batch", "not-an-index"],
    )
    async def test_an_error_outside_every_node_fails_the_batch(
        self, open_source, replay: Replay, path: list[Any]
    ) -> None:
        def stray(body: dict[str, Any]) -> None:
            body["errors"] = [{"type": "INTERNAL", "path": path, "message": "x"}]

        replay.modify(stray, when=operation("UserLookup"))
        async with open_source() as source:
            accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
        assert accounts == {}
        assert set(failed) == {PATMMCCANN, DEPENDABOT}

    @pytest.mark.parametrize(
        "change",
        [
            lambda body: body["data"].update(nodes=body["data"]["nodes"][:1]),
            lambda body: body["data"].update(nodes={"0": None}),
        ],
        ids=["short", "not-a-list"],
    )
    async def test_an_answer_that_does_not_line_up_fails_the_batch(
        self, open_source, replay: Replay, change
    ) -> None:
        replay.modify(change, when=operation("UserLookup"))
        async with open_source() as source:
            accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
        assert accounts == {}
        assert set(failed) == {PATMMCCANN, DEPENDABOT}
        assert all(isinstance(error.cause, LookupShapeError) for error in failed.values())

    async def test_a_failed_request_fails_its_batch_and_later_batches_continue(
        self, open_source, replay: Replay
    ) -> None:
        ids = synthetic_users(replay, 150)
        replay.fault(
            lambda request: synthetic("server_error"),
            when=lambda seen: seen.operation == "UserLookup" and len(seen.variables["ids"]) == 100,
            times=None,
        )
        async with open_source() as source:
            accounts, failed = await all_lookups(source, ids)
        assert list(failed) == ids[:100]
        assert all(isinstance(error.cause, ServerError) for error in failed.values())
        assert list(accounts) == ids[100:]

    async def test_a_lookup_timeout_is_retried(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("bad_gateway"), when=operation("UserLookup"))
        async with open_source() as source:
            accounts = await all_accounts(source, [PATMMCCANN])
        assert accounts[PATMMCCANN] is not None
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]
