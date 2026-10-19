"""Resolve, index, and user lookup against recorded responses (plan 5.3 steps 1-2, 5.2)."""

from __future__ import annotations

import dataclasses
import itertools
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from github_activity_db.source import (
    Account,
    ClosingIssue,
    IndexedPullRequest,
    LookedUpAccount,
    OrganizationProfile,
    RepositoryLabel,
    RepositoryRecord,
    UserProfile,
)
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
        labels["nodes"].append(
            {**labels["nodes"][0], "id": "LA_one_too_many", "name": "one too many"}
        )
        labels["pageInfo"] = {"hasNextPage": True, "endCursor": "MTQ1"}
        replay.add(page)
        async with open_source() as source:
            with pytest.raises(LabelCountMismatch) as raised:
                await source.resolve(*GO)
        assert (raised.value.fetched, raised.value.total_count) == (145, 144)


def wire_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def wire_label(node: dict[str, Any]) -> RepositoryLabel:
    return RepositoryLabel(
        node_id=node["id"],
        name=node["name"],
        color=node["color"],
        description=node["description"],
        is_default=node["isDefault"],
        created_at=wire_time(node["createdAt"]),
        updated_at=wire_time(node["updatedAt"]),
    )


@pytest.mark.parametrize("unit", recorded("resolve"), ids=labelled)
async def test_resolve_maps_every_field_from_its_wire_name(open_source, unit: Unit) -> None:
    data = unit.data
    async with open_source() as source:
        repository = await source.resolve(unit.key["owner"], unit.key["name"])
    plan = data["planFeatures"]
    assert dataclasses.replace(repository, labels=()) == RepositoryRecord(
        github_id=data["databaseId"],
        node_id=data["id"],
        owner=data["owner"]["login"],
        name=data["name"],
        full_name=data["nameWithOwner"],
        is_private=data["isPrivate"],
        is_archived=data["isArchived"],
        default_branch=data["defaultBranchRef"]["name"],
        labels=(),
        labels_total=data["labels"]["totalCount"],
        is_security_policy_enabled=data["isSecurityPolicyEnabled"],
        visibility=data["visibility"],
        description=data["description"],
        homepage_url=data["homepageUrl"],
        created_at=datetime.fromisoformat(data["createdAt"]),
        updated_at=datetime.fromisoformat(data["updatedAt"]),
        pushed_at=wire_time(data["pushedAt"]),
        archived_at=wire_time(data["archivedAt"]),
        stargazer_count=data["stargazerCount"],
        fork_count=data["forkCount"],
        disk_usage=data["diskUsage"],
        forking_allowed=data["forkingAllowed"],
        allow_update_branch=data["allowUpdateBranch"],
        web_commit_signoff_required=data["webCommitSignoffRequired"],
        has_wiki_enabled=data["hasWikiEnabled"],
        has_discussions_enabled=data["hasDiscussionsEnabled"],
        has_projects_enabled=data["hasProjectsEnabled"],
        has_vulnerability_alerts_enabled=data["hasVulnerabilityAlertsEnabled"],
        uses_custom_open_graph_image=data["usesCustomOpenGraphImage"],
        merge_commit_allowed=data["mergeCommitAllowed"],
        squash_merge_allowed=data["squashMergeAllowed"],
        rebase_merge_allowed=data["rebaseMergeAllowed"],
        delete_branch_on_merge=data["deleteBranchOnMerge"],
        merge_commit_title=data["mergeCommitTitle"],
        merge_commit_message=data["mergeCommitMessage"],
        squash_merge_commit_title=data["squashMergeCommitTitle"],
        squash_merge_commit_message=data["squashMergeCommitMessage"],
        primary_language=(data["primaryLanguage"] or {}).get("name"),
        license_spdx_id=(data["licenseInfo"] or {}).get("spdxId"),
        plan_features_codeowners=plan["codeowners"],
        plan_features_team_review_requests=plan["teamReviewRequests"],
        plan_features_maximum_manual_review_requests=plan["maximumManualReviewRequests"],
        resolved_at=parsed_date(unit),
    )
    labels = [wire_label(node) for node in data["labels"]["nodes"]]
    labels += [
        wire_label(node)
        for page in recorded("repository_labels")
        if page.key["id"] == data["id"]
        for node in page.data["labels"]["nodes"]
    ]
    assert list(repository.labels) == labels
    assert len(labels) == data["labels"]["totalCount"]
    assert repository.label_names == frozenset(label.name for label in labels)


def test_the_resolve_recordings_hold_a_value_for_each_added_field() -> None:
    """Some recording gives each added field a value that is neither null nor false, so a field
    mapped to the wrong record field cannot pass the mapping test on nulls alone."""
    units = [unit.data for unit in recorded("resolve")]
    labels = [node for data in units for node in data["labels"]["nodes"]]
    fields = [
        "isSecurityPolicyEnabled",
        "description",
        "homepageUrl",
        "pushedAt",
        "diskUsage",
        "forkingAllowed",
        "allowUpdateBranch",
        "hasWikiEnabled",
        "hasDiscussionsEnabled",
        "hasProjectsEnabled",
        "hasVulnerabilityAlertsEnabled",
        "usesCustomOpenGraphImage",
        "mergeCommitAllowed",
        "rebaseMergeAllowed",
        "deleteBranchOnMerge",
        "primaryLanguage",
        "licenseInfo",
    ]
    empty = [field for field in fields if not any(data[field] for data in units)]
    empty += [
        f"label {field}"
        for field in ("description", "isDefault", "createdAt", "updatedAt")
        if not any(node[field] for node in labels)
    ]
    assert empty == []


# Every true-or-false setting the resolve reads, in RepositoryRecord's order.
RESOLVE_SETTINGS = (
    "isPrivate",
    "isArchived",
    "isSecurityPolicyEnabled",
    "forkingAllowed",
    "allowUpdateBranch",
    "webCommitSignoffRequired",
    "hasWikiEnabled",
    "hasDiscussionsEnabled",
    "hasProjectsEnabled",
    "hasVulnerabilityAlertsEnabled",
    "usesCustomOpenGraphImage",
    "mergeCommitAllowed",
    "squashMergeAllowed",
    "rebaseMergeAllowed",
    "deleteBranchOnMerge",
    "planFeatures.codeowners",
    "planFeatures.teamReviewRequests",
)
SETTING_BITS = range((len(RESOLVE_SETTINGS) - 1).bit_length())


def settings_variant(bit: int) -> dict[str, bool]:
    """Setting i takes bit ``bit`` of i, so any two settings differ in some variant."""
    return {name: bool(i >> bit & 1) for i, name in enumerate(RESOLVE_SETTINGS)}


def test_the_settings_variants_tell_every_two_settings_apart() -> None:
    flags = [
        field.name
        for field in dataclasses.fields(RepositoryRecord)
        if field.type in ("bool", "bool | None")
    ]
    assert len(flags) == len(RESOLVE_SETTINGS)
    variants = [settings_variant(bit) for bit in SETTING_BITS]
    alike = [
        (first, second)
        for first, second in itertools.combinations(RESOLVE_SETTINGS, 2)
        if all(variant[first] == variant[second] for variant in variants)
    ]
    assert alike == []


@pytest.mark.parametrize("bit", SETTING_BITS)
async def test_every_two_resolve_settings_map_apart(open_source, replay: Replay, bit: int) -> None:
    """Several settings hold one value in every recording (forkingAllowed and
    squashMergeAllowed among them); in these variants no two are equal throughout."""
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    for name, value in settings_variant(bit).items():
        *parents, leaf = name.split(".")
        target = unit.data
        for parent in parents:
            target = target[parent]
        target[leaf] = value
    unit.source = f"synthetic: go-gdpr's resolve, setting i set to bit {bit} of i"
    replay.add(unit)
    await test_resolve_maps_every_field_from_its_wire_name(open_source, unit)


async def test_an_archived_at_time_maps(open_source, replay: Replay) -> None:
    """archivedAt is null in every recording; here it holds a time pushedAt does not."""
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    unit.data.update(archivedAt="2026-02-01T00:00:00Z", pushedAt="2026-01-01T00:00:00Z")
    unit.data["planFeatures"]["maximumManualReviewRequests"] = 3
    unit.source = "synthetic: go-gdpr's resolve with an archivedAt time"
    replay.add(unit)
    await test_resolve_maps_every_field_from_its_wire_name(open_source, unit)


async def test_organization_fields_equal_in_the_recording_map_apart(
    open_source, replay: Replay
) -> None:
    """The recorded organization has isVerified and hasSponsorsListing both false and no
    twitterUsername or archivedAt; here each has a value of its own."""
    unit = replay.unit("user", id="MDEyOk9yZ2FuaXphdGlvbjEzMjQyNzYw")
    unit.data.update(
        isVerified=True,
        hasSponsorsListing=False,
        twitterUsername="prebid_org",
        archivedAt="2026-03-01T00:00:00Z",
        email="info@example.org",
        websiteUrl="https://example.org",
    )
    unit.source = "synthetic: prebid's lookup with profile fields that differ from each other"
    replay.add(unit)
    assert unit.data["isVerified"] != unit.data["hasSponsorsListing"]
    assert unit.data["email"] != unit.data["websiteUrl"]
    await test_a_lookup_maps_every_field_from_its_wire_name(open_source, unit)


async def test_a_label_id_listed_twice_fails_the_repository(open_source, replay: Replay) -> None:
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    nodes = unit.data["labels"]["nodes"]
    nodes[1] = {**nodes[1], "id": nodes[0]["id"]}
    replay.add(unit)
    async with open_source() as source:
        with pytest.raises(RepositoryShapeError, match="listed twice"):
            await source.resolve("prebid", "go-gdpr")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("visibility", None),
        ("forkingAllowed", "true"),
        ("diskUsage", "1"),
        ("pushedAt", "1700000000"),
        ("planFeatures", None),
    ],
)
async def test_a_resolve_field_the_models_reject(
    open_source, replay: Replay, field: str, value: object
) -> None:
    unit = replay.unit("resolve", owner="prebid", name="go-gdpr")
    unit.data[field] = value
    replay.add(unit)
    async with open_source() as source:
        with pytest.raises(RepositoryShapeError, match="unexpected repository shape"):
            await source.resolve("prebid", "go-gdpr")


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

    async def test_a_state_its_dates_disagree_with_passes_through(
        self, open_source, replay: Replay
    ) -> None:
        # The index does not fail the repository for one entry: the planner records such an
        # entry as an item failure and keeps its stored row (plan section 3).
        disagreeing: dict[str, dict[str, str | None]] = {
            "CLOSED": {"mergedAt": None, "closedAt": None},
            "MERGED": {"mergedAt": None, "closedAt": "2024-05-01T00:00:00Z"},
            "OPEN": {"mergedAt": "2024-05-01T00:00:00Z", "closedAt": None},
        }

        def disagree(body: dict[str, Any]) -> None:
            nodes = body["data"]["repository"]["pullRequests"]["nodes"]
            for node, (state, dates) in zip(nodes, disagreeing.items(), strict=False):
                node.update(state=state, **dates)

        replay.modify(disagree, when=operation("PullRequestIndex"))
        async with open_source() as source:
            index = await source.index_pull_requests(await source.resolve("prebid", "go-gdpr"))
        assert [(entry.state, entry.merged_at, entry.closed_at) for entry in index.entries[:3]] == [
            (state, wire_time(dates["mergedAt"]), wire_time(dates["closedAt"]))
            for state, dates in disagreeing.items()
        ]

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
                closed_at=datetime.fromisoformat(node["closedAt"]) if node["closedAt"] else None,
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


def test_the_index_recordings_hold_a_closed_pull_request() -> None:
    nodes = [node for unit in recorded("index") for node in unit.data["nodes"]]
    assert any(node["state"] == "CLOSED" and node["closedAt"] for node in nodes)


def wire_looked_up(node: dict[str, Any]) -> LookedUpAccount:
    """A lookup node as the source layer reads it, by wire name (plan 5.8, D31)."""
    typename = node["__typename"]
    profile: UserProfile | OrganizationProfile | None = None
    if typename == "User":
        profile = UserProfile(
            company=node["company"], location=node["location"], website_url=node["websiteUrl"]
        )
    elif typename == "Organization":
        profile = OrganizationProfile(
            description=node["description"],
            email=node["email"],
            location=node["location"],
            website_url=node["websiteUrl"],
            twitter_username=node["twitterUsername"],
            is_verified=node["isVerified"],
            archived_at=wire_time(node["archivedAt"]),
            has_sponsors_listing=node["hasSponsorsListing"],
            pinned_items_remaining=node["pinnedItemsRemaining"],
        )
    return LookedUpAccount(
        account=Account(
            github_id=node.get("databaseId"),
            node_id=node["id"],
            login=node["login"],
            type=typename,
            linked_user_github_id=(node.get("user") or {}).get("databaseId"),
        ),
        name=node.get("name"),
        created_at=datetime.fromisoformat(node["createdAt"]),
        updated_at=datetime.fromisoformat(node["updatedAt"]),
        claimant_github_id=(node.get("claimant") or {}).get("databaseId"),
        profile=profile,
    )


@pytest.mark.parametrize("unit", [u for u in recorded("user") if u.data], ids=labelled)
async def test_a_lookup_maps_every_field_from_its_wire_name(open_source, unit: Unit) -> None:
    async with open_source() as source:
        lookups = [lookup async for lookup in source.lookup_users([unit.key["id"]])]
    ((only,),) = [list(lookup.accounts.values()) for lookup in lookups]
    assert only == wire_looked_up(unit.data)
    assert lookups[0].looked_up_at == parsed_date(unit)


def test_the_lookup_recordings_hold_each_profile_kind() -> None:
    nodes = [unit.data for unit in recorded("user") if unit.data]
    users = [node for node in nodes if node["__typename"] == "User"]
    assert {node["__typename"] for node in nodes} == {"User", "Bot", "Organization"}
    assert all(
        any(node[field] for node in users) for field in ("company", "location", "websiteUrl")
    )
    (organization,) = [node for node in nodes if node["__typename"] == "Organization"]
    assert all(
        organization[field]
        for field in ("description", "email", "location", "websiteUrl", "pinnedItemsRemaining")
    )


MANNEQUIN: dict[str, Any] = {
    "__typename": "Mannequin",
    "id": "MQ_1",
    "databaseId": 501,
    "login": "imported-someone",
    "name": "Someone",
    "createdAt": "2020-01-01T00:00:00Z",
    "updatedAt": "2020-01-02T00:00:00Z",
    "claimant": {"databaseId": 1683175},
}
ENTERPRISE_USER: dict[str, Any] = {
    "__typename": "EnterpriseUserAccount",
    "id": "MQ_1",
    "login": "someone_corp",
    "name": "Someone",
    "createdAt": "2020-01-01T00:00:00Z",
    "updatedAt": "2020-01-02T00:00:00Z",
    "user": {"databaseId": 81847},
}


@pytest.mark.parametrize(
    "node",
    [
        MANNEQUIN,
        {
            "__typename": "Mannequin",
            "id": "MQ_1",
            "databaseId": 501,
            "login": "imported-someone",
            "name": None,
            "createdAt": "2020-01-01T00:00:00Z",
            "updatedAt": "2020-01-02T00:00:00Z",
            "claimant": None,
        },
        ENTERPRISE_USER,
        {**MANNEQUIN, "claimant": {"databaseId": None}},
        {**ENTERPRISE_USER, "user": {"databaseId": None}},
    ],
    ids=[
        "claimed-mannequin",
        "unclaimed-mannequin",
        "enterprise-user",
        "claimant-without-database-id",
        "enterprise-user-without-database-id",
    ],
)
async def test_a_lookup_of_a_type_without_a_profile(
    open_source, replay: Replay, node: dict[str, Any]
) -> None:
    # Synthetic: no tracked repository refers to a mannequin or an enterprise account.
    unit = replay.unit("user", id=PATMMCCANN)
    unit.key = {"id": "MQ_1"}
    unit.data = node
    unit.source = "synthetic: a lookup of a type no recording holds"
    replay.add(unit)
    async with open_source() as source:
        lookups = [lookup async for lookup in source.lookup_users(["MQ_1"])]
    assert lookups[0].accounts == {"MQ_1": wire_looked_up(node)}
    assert lookups[0].accounts["MQ_1"] is not None
    assert lookups[0].accounts["MQ_1"].profile is None


@pytest.mark.parametrize(
    ("node", "field", "link"),
    [
        (MANNEQUIN, "claimant", {"databaseId": "1683175"}),
        (MANNEQUIN, "claimant", {}),
        (ENTERPRISE_USER, "user", {"databaseId": "81847"}),
        (ENTERPRISE_USER, "user", {}),
    ],
    ids=["claimant-id-a-string", "claimant-id-missing", "user-id-a-string", "user-id-missing"],
)
async def test_a_lookup_with_a_malformed_link_fails_that_id(
    open_source, replay: Replay, node: dict[str, Any], field: str, link: dict[str, object]
) -> None:
    # A null databaseId on the link is GitHub's (User.databaseId is nullable); another type,
    # or the selected field missing, is not.
    unit = replay.unit("user", id=PATMMCCANN)
    unit.key = {"id": "MQ_1"}
    unit.data = {**node, field: link}
    unit.source = "synthetic: a lookup whose linked user is malformed"
    replay.add(unit)
    async with open_source() as source:
        accounts, failed = await all_lookups(source, ["MQ_1"])
    assert accounts == {}
    assert list(failed) == ["MQ_1"]
    assert isinstance(failed["MQ_1"].cause, LookupShapeError)
    assert "databaseId" in str(failed["MQ_1"].cause)


@pytest.mark.parametrize(
    ("field", "value"),
    [("company", 7), ("createdAt", None), ("websiteUrl", False), ("__typename", "Team")],
)
async def test_a_lookup_field_the_models_reject_fails_that_id(
    open_source, replay: Replay, field: str, value: object
) -> None:
    unit = replay.unit("user", id=PATMMCCANN)
    unit.data[field] = value
    replay.add(unit)
    async with open_source() as source:
        accounts, failed = await all_lookups(source, [PATMMCCANN, DEPENDABOT])
    assert list(failed) == [PATMMCCANN]
    assert isinstance(failed[PATMMCCANN].cause, LookupShapeError)
    assert accounts[DEPENDABOT] is not None


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
        [
            ["nodes", 0, None],
            ["nodes", 0, {}],
            ["nodes", 0, 1.5],
            ["nodes", 0.0],
            ["nodes", True],
        ],
        ids=["null", "object", "float", "integral-float-index", "bool-index"],
    )
    async def test_a_not_found_at_a_malformed_path_is_not_deleted(
        self, open_source, replay: Replay, path: list[Any]
    ) -> None:
        # Trimmed, the first three would read as ["nodes", 0], NOT_FOUND on the node: a deleted
        # account; and 0.0 equals 0 in Python. A bool is no index either (True equals 1).
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
            lookups = [lookup async for lookup in source.lookup_users([PATMMCCANN, DEPENDABOT])]
        (lookup,) = lookups
        assert lookup.accounts == {}
        assert set(lookup.failed) == {PATMMCCANN, DEPENDABOT}
        assert all(isinstance(error.cause, LookupShapeError) for error in lookup.failed.values())
        # A response came, so it has a time: the first unit's Date header the replay served.
        assert lookup.looked_up_at == parsed_date(replay.unit("user", id=PATMMCCANN))

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
            lookups = [lookup async for lookup in source.lookup_users(ids)]
        first, second = lookups
        assert list(first.failed) == ids[:100]
        assert all(isinstance(error.cause, ServerError) for error in first.failed.values())
        assert first.accounts == {}
        # The failed request has no response, so no response time; the next batch has one.
        assert first.looked_up_at is None
        assert list(second.accounts) == ids[100:]
        assert second.failed == {}
        assert second.looked_up_at is not None

    async def test_a_lookup_timeout_is_retried(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("bad_gateway"), when=operation("UserLookup"))
        async with open_source() as source:
            accounts = await all_accounts(source, [PATMMCCANN])
        assert accounts[PATMMCCANN] is not None
        assert clock.sleeps == [SERVER_RETRY_DELAYS[0]]
