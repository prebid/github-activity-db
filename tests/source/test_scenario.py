"""The scenario model against the recordings, and its changes read through the real source.

Every recorded unit loads into one model that must re-render it exactly; the source must read the
model's responses as it reads the recordings; and each change the model applies must reach the
records the source returns.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from github_activity_db.source import (
    GitHubSource,
    Gone,
    Hydrated,
    PullRequestOutcome,
    PullRequestRecord,
    documents,
)
from github_activity_db.source.errors import (
    FatalSourceError,
    RepositoryNotFound,
    RepositoryShapeError,
)
from tests.source.conftest import FakeClock, all_lookups, all_outcomes
from tests.source.recordings import Unit
from tests.source.replay import Replay, unused_faults
from tests.source.scenario import Scenario, read_commit, recorded_units

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def dumped(value: Any) -> str:
    """JSON text, so that True and 1, or 1 and 1.0, differ."""
    return json.dumps(value, sort_keys=True)


@pytest.fixture(scope="module")
def recorded_model() -> Scenario:
    return Scenario.from_recordings(FakeClock())


@pytest.mark.parametrize("unit", list(recorded_units()), ids=lambda unit: str(unit.path()))
def test_each_recording_re_renders_exactly(recorded_model: Scenario, unit: Unit) -> None:
    rendered = recorded_model.unit(unit.kind, unit.key)
    assert rendered is not None
    assert rendered.document_sha256 == unit.document_sha256
    if unit.kind == "compare":
        # Compared on what the source reads: the recorded pages also carry REST fields it never
        # reads, which the model does not hold.
        assert rendered.data["total_commits"] == unit.data["total_commits"]
        assert dumped(rendered.data["commits"]) == dumped(
            [read_commit(commit) for commit in unit.data["commits"]]
        )
    else:
        assert dumped(rendered.data) == dumped(unit.data)
    assert dumped(rendered.errors) == dumped(unit.errors)


def test_the_recordings_join_into_one_model(recorded_model: Scenario) -> None:
    """The round trip renders from joined state, not from stored units: pages are split again,
    an index entry comes from its hydrated node, and labels span the resolve and its label page."""
    go_gdpr = recorded_model.repositories["prebid/go-gdpr"]
    assert go_gdpr.pulls[21].hydratable
    assert len(go_gdpr.indexed()) == 37
    assert len(recorded_model.repositories["prebid/prebid-cache-java"].indexed()) == 157
    server = recorded_model.repositories["prebid/prebid-server"].pulls[4343].node
    assert (server["comments"]["totalCount"], len(server["comments"]["nodes"])) == (109, 109)
    assert len(recorded_model.repositories["golang/go"].data["labels"]["nodes"]) == 144
    prebid = recorded_model.repositories["prebid/prebid.js"].pulls
    assert prebid[8858].compare is not None and len(prebid[8858].compare) == 254
    assert prebid[999999].gone
    assert recorded_model.accounts["MDQ6VXNlcjk5OTk5OTk5OTk="] is None


# The repositories whose resolve, index, and pull requests are recorded.
RESOLVED = sorted(
    {(u.key["owner"], u.key["name"]) for u in recorded_units() if u.kind == "resolve"}
)
INDEXED = sorted({(u.key["owner"], u.key["name"]) for u in recorded_units() if u.kind == "index"})
NUMBERS = {
    repository: sorted(
        u.key["number"]
        for u in recorded_units()
        if u.kind == "pull_request" and (u.key["owner"], u.key["name"]) == repository
    )
    for repository in RESOLVED
}
USERS = sorted(u.key["id"] for u in recorded_units() if u.kind == "user")


def timeless(outcome: PullRequestOutcome) -> PullRequestOutcome:
    if isinstance(outcome, Hydrated):
        return Hydrated(replace(outcome.record, hydrated_at=EPOCH))
    return outcome


async def read_everything(replay: Replay, clock: FakeClock) -> list[Any]:
    """What the source reads from every recorded surface, without the times taken from ``Date``."""
    read: list[Any] = []
    async with GitHubSource("test-token", http_transport=replay.transport(), clock=clock) as source:
        for owner, name in RESOLVED:
            repository = await source.resolve(owner, name)
            read.append(replace(repository, resolved_at=EPOCH))
            if (owner, name) in INDEXED:
                index = await source.index_pull_requests(repository)
                read.append(replace(index, started_at=EPOCH))
            if NUMBERS[(owner, name)]:
                outcomes = await all_outcomes(source, repository, NUMBERS[(owner, name)])
                read.extend(timeless(outcome) for outcome in outcomes)
        read.append(await all_lookups(source, USERS))
    return read


async def test_the_source_reads_the_model_as_it_reads_the_recordings() -> None:
    recorded = Replay.from_fixtures()
    model = Scenario.from_recordings(FakeClock())
    expected = await read_everything(recorded, FakeClock())
    assert await read_everything(model.replay, model.clock) == expected
    for replay in (recorded, model.replay):
        assert replay.problems == []
        assert unused_faults(replay) == []
    assert any(isinstance(item, Gone) for item in expected)


# -- changes ------------------------------------------------------------------------------------
async def hydrate(scenario: Scenario, owner: str, name: str, number: int) -> PullRequestOutcome:
    async with scenario.source() as source:
        repository = await source.resolve(owner, name)
        (outcome,) = await all_outcomes(source, repository, [number])
    return outcome


def hydrated(outcome: PullRequestOutcome) -> PullRequestRecord:
    assert isinstance(outcome, Hydrated), outcome
    return outcome.record


async def test_new_objects_read_back_with_the_clock_as_date(scenario: Scenario) -> None:
    author = scenario.add_account(login="alice")
    repository = scenario.add_repository("example", "project")
    label = scenario.add_label(repository, "bug")
    pull = scenario.open_pull_request(repository, author)
    pull.add("labels", {"id": label["id"], "name": label["name"]})
    pull.add("comments", scenario.node("comments", author=scenario.actor(author), editor=None))
    scenario.clock.advance(90)

    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        index = await source.index_pull_requests(resolved)
        (outcome,) = await all_outcomes(source, resolved, [pull.number])
        accounts, failed = await all_lookups(source, [author])

    assert (resolved.full_name, resolved.github_id) == (
        "example/project",
        repository.data["databaseId"],
    )
    assert resolved.resolved_at == scenario.clock.now()
    assert [label.name for label in resolved.labels] == ["bug"]
    assert [(entry.number, entry.state) for entry in index.entries] == [(pull.number, "OPEN")]
    record = hydrated(outcome)
    assert record.hydrated_at == scenario.clock.now()
    assert (record.github_id, record.state) == (int(pull.node["fullDatabaseId"]), "OPEN")
    assert record.author is not None and record.author.login == "alice"
    assert [(label.node_id, label.name) for label in record.labels] == [(label["id"], "bug")]
    assert len(record.comments) == 1 and record.comments[0].author == record.author
    assert failed == {}
    found = accounts[author]
    assert found is not None and found.account.login == "alice"


async def test_merge_close_and_reopen(scenario: Scenario) -> None:
    author = scenario.add_account()
    merger = scenario.add_account(login="merger")
    repository = scenario.add_repository("example", "project")
    merged = scenario.open_pull_request(repository, author)
    closed = scenario.open_pull_request(repository, author)
    scenario.clock.advance(3600)
    merged.merge(by=scenario.actor(merger))
    closed.close()

    record = hydrated(await hydrate(scenario, "example", "project", merged.number))
    assert (record.state, record.merged_at, record.closed_at) == (
        "MERGED",
        scenario.clock.now(),
        scenario.clock.now(),
    )
    assert record.merged_by is not None and record.merged_by.login == "merger"
    assert record.github_updated_at == scenario.clock.now()
    record = hydrated(await hydrate(scenario, "example", "project", closed.number))
    assert (record.state, record.closed_at) == ("CLOSED", scenario.clock.now())

    scenario.clock.advance(60)
    closed.reopen()
    record = hydrated(await hydrate(scenario, "example", "project", closed.number))
    assert (record.state, record.closed_at) == ("OPEN", None)


async def test_index_only_changes_leave_updated_at(scenario: Scenario) -> None:
    repository = scenario.add_repository("example", "project")
    pull = scenario.open_pull_request(repository, scenario.add_account(), isDraft=True)
    before = pull.node["updatedAt"]
    scenario.clock.advance(60)
    pull.set(isDraft=False, authorAssociation="MEMBER")
    async with scenario.source() as source:
        index = await source.index_pull_requests(await source.resolve("example", "project"))
    (entry,) = index.entries
    assert (entry.is_draft, entry.author_association) == (False, "MEMBER")
    assert pull.node["updatedAt"] == before


async def test_a_renamed_label_keeps_its_id(scenario: Scenario) -> None:
    repository = scenario.add_repository("example", "project")
    label = scenario.add_label(repository, "bug")
    pull = scenario.open_pull_request(repository, scenario.add_account())
    pull.add("labels", {"id": label["id"], "name": label["name"]})
    scenario.rename_label(repository, label["id"], "defect")

    record = hydrated(await hydrate(scenario, "example", "project", pull.number))
    assert [(label.node_id, label.name) for label in record.labels] == [(label["id"], "defect")]


async def test_a_deleted_account_and_one_seen_again(scenario: Scenario) -> None:
    author = scenario.add_account(login="leaving")
    repository = scenario.add_repository("example", "project")
    pull = scenario.open_pull_request(repository, author)
    pull.add("comments", scenario.node("comments", author=scenario.actor(author), editor=None))
    scenario.delete_account(author)

    record = hydrated(await hydrate(scenario, "example", "project", pull.number))
    assert record.author is None and record.comments[0].author is None
    async with scenario.source() as source:
        accounts, failed = await all_lookups(source, [author])
    assert (accounts, failed) == ({author: None}, {})

    scenario.restore_account(author)
    async with scenario.source() as source:
        accounts, _ = await all_lookups(source, [author])
    found = accounts[author]
    assert found is not None and found.account.login == "leaving"
    assert pull.node["author"] is None


async def test_a_pull_request_gone(scenario: Scenario) -> None:
    repository = scenario.add_repository("example", "project")
    pull = scenario.open_pull_request(repository, scenario.add_account())
    pull.gone = True
    assert await hydrate(scenario, "example", "project", pull.number) == Gone(pull.number)
    async with scenario.source() as source:
        index = await source.index_pull_requests(await source.resolve("example", "project"))
    assert index.entries == ()


async def test_a_repository_gone(scenario: Scenario) -> None:
    repository = scenario.add_repository("example", "project")
    pull = scenario.open_pull_request(repository, scenario.add_account())
    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        repository.gone = True
        with pytest.raises(RepositoryNotFound):
            await source.resolve("example", "project")
        with pytest.raises(RepositoryShapeError, match="NOT_FOUND"):
            await source.index_pull_requests(resolved)
        with pytest.raises(RepositoryNotFound):
            await all_outcomes(source, resolved, [pull.number])


async def test_a_renamed_repository_resolves_by_its_old_name(scenario: Scenario) -> None:
    issues = scenario.add_repository("example", "issues")
    repository = scenario.add_repository("example", "project")
    pull = scenario.open_pull_request(repository, scenario.add_account())
    pull.link(7, issues.data, user_linked=False, github_id=scenario.new_id())
    scenario.rename_repository(issues, "example", "tracker")

    async with scenario.source() as source:
        resolved = await source.resolve("example", "issues")
        (outcome,) = await all_outcomes(
            source, await source.resolve("example", "project"), [pull.number]
        )
    assert (resolved.full_name, resolved.github_id) == (
        "example/tracker",
        issues.data["databaseId"],
    )
    assert [link.repo_full_name for link in hydrated(outcome).closing_issues] == ["example/tracker"]


async def test_closing_links_beyond_the_index_cap(scenario: Scenario) -> None:
    issues = scenario.add_repository("example", "issues")
    repository = scenario.add_repository("example", "project")
    pull = scenario.open_pull_request(repository, scenario.add_account())
    for number in range(1, 12):
        pull.link(number, issues.data, user_linked=number == 1, github_id=scenario.new_id())

    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        index = await source.index_pull_requests(resolved)
        (outcome,) = await all_outcomes(source, resolved, [pull.number])
    (entry,) = index.entries
    assert (entry.closing_issues_total, len(entry.closing_issues)) == (11, 10)
    links = hydrated(outcome).closing_issues
    assert [(link.number, link.user_linked) for link in links] == [
        (number, number == 1) for number in range(1, 12)
    ]
    pull.unlink(1)
    assert pull.node["userLinkedClosingIssues"]["totalCount"] == 0


async def test_a_connection_beyond_one_page(scenario: Scenario) -> None:
    author = scenario.add_account()
    repository = scenario.add_repository("example", "project")
    pull = scenario.open_pull_request(repository, author)
    pull.add(
        "comments",
        *(
            scenario.node("comments", author=scenario.actor(author), editor=None)
            for _ in range(101)
        ),
    )
    record = hydrated(await hydrate(scenario, "example", "project", pull.number))
    assert len(record.comments) == 101
    assert [seen.operation for seen in scenario.replay.seen].count("Overflow") == 1


def test_a_document_the_model_was_not_checked_against_is_refused() -> None:
    world = Scenario(FakeClock())
    world.add_repository("example", "project")
    changed = documents.RESOLVE.replace("isPrivate", "isPrivate isFork")
    assert changed != documents.RESOLVE
    variables = {"owner": "example", "name": "project"}
    with httpx.Client(
        transport=world.replay.transport(), base_url="https://api.github.com"
    ) as client:
        answered = client.post(
            "/graphql", json={"query": documents.RESOLVE, "variables": variables}
        )
        refused = client.post("/graphql", json={"query": changed, "variables": variables})
    assert (answered.status_code, refused.status_code) == (200, 418)
    assert world.replay.problems == [
        f"resolve unit {variables} was recorded for another query document: re-record with "
        "scripts/record_fixtures.py"
    ]


async def test_a_continuation_no_recording_covers_is_left_unanswered() -> None:
    """No recording continues labels, so the model renders no label continuation: the replay
    reports the request as one to record, and the source stops on its answer."""
    world = Scenario(FakeClock())
    repository = world.add_repository("example", "project")
    pull = world.open_pull_request(repository, world.add_account())
    for position in range(101):
        label = world.add_label(repository, f"label-{position}")
        pull.add("labels", {"id": label["id"], "name": label["name"]})
    async with world.source() as source:
        resolved = await source.resolve("example", "project")
        with pytest.raises(FatalSourceError):
            await all_outcomes(source, resolved, [pull.number])
    key = {"id": pull.node["id"], "connection": "labels", "after": "MTAw"}
    assert world.replay.problems == [
        f"no overflow unit for {key}: record it with scripts/record_fixtures.py"
    ]
