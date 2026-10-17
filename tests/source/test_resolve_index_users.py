"""Resolve, index, and user lookup against recorded responses (plan 5.3 steps 1-2, 5.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from github_activity_db.source import Account, ClosingIssue, IndexedPullRequest, documents
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
from tests.source.conftest import all_accounts
from tests.source.recordings import Unit, whole_stamp
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

    async def test_errors_under_the_repository_fail_it(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.errors = [{"type": "SERVICE_UNAVAILABLE", "path": ["labels"], "message": "partial"}]
        replay.add(unit)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="errors under repository"):
                await source.resolve("prebid", "go-gdpr")

    async def test_a_label_page_for_another_repository(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["totalCount"] = 11
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        page = _label_page(unit, "page-2", ["needs triage"], total=11)
        page.data["id"] = "R_another"
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="label page for R_another"):
                await source.resolve("prebid", "go-gdpr")

    async def test_a_label_total_that_changes_between_pages(
        self, open_source, replay: Replay
    ) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["totalCount"] = 11
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        replay.add(_label_page(unit, "page-2", ["needs triage", "another"], total=12))
        async with open_source() as source:
            with pytest.raises(
                RepositoryShapeError, match="label totalCount changed from 11 to 12"
            ):
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
            with pytest.raises(RepositoryShapeError, match="no new cursor"):
                await source.resolve("prebid", "go-gdpr")

    async def test_a_label_page_that_repeats_its_cursor(self, open_source, replay: Replay) -> None:
        unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
        unit.data["labels"]["totalCount"] = 11
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        replay.add(_label_page(unit, "page-2", ["needs triage"], total=11))

        # Once only: a resolver that followed the cursor would read page 2 again and fail on
        # the label count instead of looping.
        def repeat(body: dict[str, Any]) -> None:
            page_info = body["data"]["node"]["labels"]["pageInfo"]
            page_info.update(hasNextPage=True, endCursor="page-2")

        replay.modify(repeat, when=operation("RepositoryLabels"))
        async with open_source() as source:
            with pytest.raises(RepositoryShapeError, match="no new cursor"):
                await source.resolve("prebid", "go-gdpr")


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
    assert repository.label_names == {node["name"] for node in data["labels"]["nodes"]}


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


def _label_page(resolve: Unit, cursor: str, names: list[str], *, total: int) -> Unit:
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
        ],
        ids=["nodes", "entry", "links", "link-entry"],
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

    @pytest.mark.parametrize("repeat", [False, True], ids=["missing", "repeated"])
    async def test_a_next_page_without_a_new_cursor(
        self, open_source, replay: Replay, repeat: bool
    ) -> None:
        first_page = replay.unit("index", owner="prebid", name="prebid-cache-java", after=None)
        first_cursor = first_page.data["pageInfo"]["endCursor"]

        # Once only on the second page: an index that followed a repeated cursor would read
        # the recorded page again and fail on a duplicate instead of looping.
        def stall(body: dict[str, Any]) -> None:
            page_info = body["data"]["repository"]["pullRequests"]["pageInfo"]
            page_info.update(hasNextPage=True, endCursor=first_cursor if repeat else None)

        replay.modify(
            stall,
            when=lambda seen: seen.operation == "PullRequestIndex"
            and seen.variables.get("after") == (first_cursor if repeat else None),
        )
        async with open_source() as source:
            repository = await source.resolve("prebid", "prebid-cache-java")
            with pytest.raises(RepositoryShapeError, match="no new cursor"):
                await source.index_pull_requests(repository)

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
        template = replay.unit("user", id=PATMMCCANN)
        ids = [f"synthetic-{i}" for i in range(150)]
        for number, node_id in enumerate(ids):
            unit = replay.unit("user", id=PATMMCCANN)
            unit.key = {"id": node_id}
            unit.data = {**template.data, "id": node_id, "databaseId": number + 1}
            unit.source = "synthetic: copies of the recorded patmmccann lookup with new ids"
            replay.add(unit)
        async with open_source() as source:
            accounts = await all_accounts(source, ids)
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
                await all_accounts(source, [PATMMCCANN])

    async def test_a_short_answer_fails_the_lookup(self, open_source, replay: Replay) -> None:
        def short(body: dict) -> None:
            body["data"]["nodes"] = body["data"]["nodes"][:1]

        replay.modify(short, when=operation("UserLookup"))
        async with open_source() as source:
            with pytest.raises(UserLookupFailed):
                await all_accounts(source, [PATMMCCANN, DEPENDABOT])

    async def test_an_answer_for_another_id_fails_the_lookup(
        self, open_source, replay: Replay
    ) -> None:
        def swapped(body: dict) -> None:
            body["data"]["nodes"][0]["id"] = DEPENDABOT

        replay.modify(swapped, when=operation("UserLookup"))
        async with open_source() as source:
            with pytest.raises(UserLookupFailed):
                await all_accounts(source, [PATMMCCANN])

    async def test_a_failed_request_fails_the_lookup(self, open_source, replay: Replay) -> None:
        replay.fault(
            lambda request: synthetic("server_error"), when=operation("UserLookup"), times=None
        )
        async with open_source() as source:
            with pytest.raises(UserLookupFailed):
                await all_accounts(source, [PATMMCCANN])
