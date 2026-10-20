"""The scenario model against the recordings, and its changes read through the real source.

Every recorded unit loads into one model that must re-render it exactly; the source must read the
model's responses as it reads the recordings; and each change the model applies must reach the
records the source returns.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
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
from github_activity_db.source.models import ComparePage
from tests.source.conftest import START, FakeClock, all_lookups, all_outcomes
from tests.source.recordings import Unit
from tests.source.replay import Replay, loaded_units, synthetic, unanswered
from tests.source.scenario import (
    RENDERED,
    Repository,
    Scenario,
    ScenarioError,
    iso,
    recorded_units,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def dumped(value: Any) -> str:
    """JSON text, so that True and 1, or 1 and 1.0, differ."""
    return json.dumps(value, sort_keys=True)


@pytest.fixture(scope="module")
def recorded_model() -> Scenario:
    return Scenario.from_recordings(FakeClock())


def test_every_recorded_kind_but_the_rate_limits_is_rendered() -> None:
    kinds = {unit.kind for unit in loaded_units()}
    assert kinds == {*RENDERED, "rate_limit", "rest_rate_limit"}


@pytest.mark.parametrize("unit", list(recorded_units()), ids=lambda unit: str(unit.path()))
def test_each_recording_re_renders_exactly(recorded_model: Scenario, unit: Unit) -> None:
    rendered = recorded_model.unit(unit.kind, unit.key)
    assert rendered is not None
    assert rendered.document_sha256 == unit.document_sha256
    if unit.kind == "compare":
        # Compared as the source reads them: the recorded pages also carry REST fields it never
        # reads, which the model does not hold.
        assert ComparePage.model_validate(rendered.data) == ComparePage.model_validate(unit.data)
    else:
        assert dumped(rendered.data) == dumped(unit.data)
    assert dumped(rendered.errors) == dumped(unit.errors)


def test_the_recordings_join_into_one_model(recorded_model: Scenario) -> None:
    """The round trip renders from joined state, not from stored units: pages are split again,
    an index entry comes from its hydrated node, and labels span the resolve and its label page."""
    go_gdpr = recorded_model.repositories["prebid/go-gdpr"]
    assert go_gdpr.pulls[21].hydratable
    assert go_gdpr.index()["totalCount"] == 37
    assert recorded_model.repositories["prebid/prebid-cache-java"].index()["totalCount"] == 157
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
    assert unanswered(recorded) == unanswered(model.replay) == []
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


def project(scenario: Scenario, pulls: int = 1) -> tuple[Repository, str]:
    """``example/project`` with ``pulls`` open pull requests by one author."""
    author = scenario.add_account(login="alice")
    repository = scenario.add_repository("example", "project")
    for _ in range(pulls):
        scenario.open_pull_request(repository, author)
    return repository, author


async def test_new_objects_read_back_with_the_clock_as_date(scenario: Scenario) -> None:
    repository, author = project(scenario)
    pull = repository.pulls[1]
    label = scenario.add_label(repository, "bug")
    pull.add("labels", label)
    pull.add("comments", scenario.node("comments", by=author))
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
    assert [(entry.number, entry.state) for entry in index.entries] == [(1, "OPEN")]
    record = hydrated(outcome)
    assert record.hydrated_at == scenario.clock.now()
    assert (record.github_id, record.state) == (int(pull.node["fullDatabaseId"]), "OPEN")
    assert record.author is not None and record.author.login == "alice"
    assert [(label.node_id, label.name) for label in record.labels] == [(label["id"], "bug")]
    assert len(record.comments) == 1 and record.comments[0].author == record.author
    assert failed == {}
    found = accounts[author]
    assert found is not None and found.account.login == "alice"


async def test_each_response_dates_from_the_clock_when_it_is_sent(scenario: Scenario) -> None:
    """A fault stacks on the model: the retried batch is answered after the retry's wait."""
    project(scenario)
    scenario.replay.fault(
        lambda request: synthetic("server_error"),
        when=lambda seen: seen.operation == "PullRequestBatch",
    )
    record = hydrated(await hydrate(scenario, "example", "project", 1))
    assert scenario.clock.sleeps
    assert record.hydrated_at == scenario.clock.now() > START


async def test_new_nodes_have_their_own_identity(scenario: Scenario) -> None:
    repository, author = project(scenario)
    pull = repository.pulls[1]
    pull.add("commits", scenario.node("commits", by=author), scenario.node("commits", by=author))
    pull.add("files", scenario.node("files"), scenario.node("files"))
    pull.add("commits", scenario.node("commits"), scenario.node("commits"))
    pull.add("comments", scenario.node("comments"))
    scenario.clock.advance(30)
    pull.add("reviews", scenario.node("reviews", by=author))
    pull.add("assignedActors", scenario.node("assignedActors", by=author))
    pull.add("reviewRequests", scenario.node("reviewRequests", by=author))

    record = hydrated(await hydrate(scenario, "example", "project", 1))
    assert len({commit.sha for commit in record.commits}) == 4
    assert [commit.author.login if commit.author else None for commit in record.commits] == [
        "alice",
        "alice",
        None,
        None,
    ]
    assert len({commit.author_email for commit in record.commits}) == 3
    (review,) = record.reviews
    assert (review.created_at, review.submitted_at) == (scenario.clock.now(), scenario.clock.now())
    assert review.reviewer is not None and review.reviewer.login == "alice"
    assert len({file.path for file in record.files}) == 2
    assert record.comments[0].author is None
    assert [assignee.login for assignee in record.assignees] == ["alice"]
    assert [
        request.account.login if request.account else None for request in record.review_requests
    ] == ["alice"]


def test_nodes_that_come_from_elsewhere_are_refused(scenario: Scenario) -> None:
    author = scenario.add_account()
    for connection in ("labels", "closingIssuesReferences", "userLinkedClosingIssues"):
        with pytest.raises(ScenarioError, match=r"add_label and PullRequest\.link"):
            scenario.node(connection)
    with pytest.raises(ScenarioError, match="an assignee is an account"):
        scenario.node("assignedActors")
    scenario.delete_account(author)
    with pytest.raises(ScenarioError, match="only a commit links it"):
        scenario.node("comments", by=author)


async def test_merge_close_and_reopen(scenario: Scenario) -> None:
    repository, _ = project(scenario, pulls=2)
    merged, closed = repository.pulls[1], repository.pulls[2]
    merger = scenario.add_account(login="merger")
    scenario.clock.advance(3600)
    merged.merge(by=merger)
    closed.close()

    record = hydrated(await hydrate(scenario, "example", "project", 1))
    now = scenario.clock.now()
    assert (record.state, record.merged_at, record.closed_at) == ("MERGED", now, now)
    assert record.merged_by is not None and record.merged_by.login == "merger"
    assert record.merge_commit_oid is not None
    assert record.github_updated_at == now
    record = hydrated(await hydrate(scenario, "example", "project", 2))
    assert (record.state, record.closed_at, record.github_updated_at) == ("CLOSED", now, now)

    scenario.clock.advance(60)
    closed.reopen()
    record = hydrated(await hydrate(scenario, "example", "project", 2))
    assert (record.state, record.closed_at, record.github_updated_at) == (
        "OPEN",
        None,
        scenario.clock.now(),
    )


async def test_set_leaves_updated_at_and_touch_moves_it(scenario: Scenario) -> None:
    repository, author = project(scenario, pulls=2)
    scenario.open_pull_request(repository, author, isDraft=True)
    scenario.clock.advance(60)
    repository.pulls[1].set(authorAssociation="MEMBER")
    repository.pulls[2].set(isDraft=True)  # converted to a draft, which moves updatedAt
    repository.pulls[2].touch()
    repository.pulls[3].set(isDraft=False)  # marked ready, which is not shown to move it
    async with scenario.source() as source:
        index = await source.index_pull_requests(await source.resolve("example", "project"))
    first, second, third = index.entries
    assert (first.author_association, first.updated_at) == ("MEMBER", START)
    assert (second.is_draft, second.updated_at) == (True, scenario.clock.now())
    assert (third.is_draft, third.updated_at) == (False, START)


async def test_the_index_is_in_creation_order(scenario: Scenario) -> None:
    repository, author = project(scenario, pulls=2)
    scenario.open_pull_request(repository, author, createdAt="2026-01-01T00:00:00Z")
    first_id = int(repository.pulls[1].node["fullDatabaseId"])
    repository.pulls[2].set(fullDatabaseId=str(first_id - 1))
    async with scenario.source() as source:
        index = await source.index_pull_requests(await source.resolve("example", "project"))
    # Pull requests 1 and 2 share a creation time; GitHub orders those by id.
    assert [entry.number for entry in index.entries] == [3, 2, 1]


async def test_a_pull_request_gone_during_an_index_scan_is_rescanned(
    scenario: Scenario,
) -> None:
    """The index's cursor names the last entry served, so the next page starts after it and the
    count no longer adds up, as on GitHub (plan 5.3 step 2)."""
    repository, _ = project(scenario, pulls=150)

    def remove() -> None:
        repository.pulls[5].gone = True

    scenario.after("PullRequestIndex", remove)
    async with scenario.source() as source:
        index = await source.index_pull_requests(await source.resolve("example", "project"))
    assert index.scans == 2
    assert [entry.number for entry in index.entries] == [n for n in range(1, 151) if n != 5]


@pytest.mark.parametrize("connection", ["comments", "reviews"])
async def test_a_node_replaced_during_hydration_is_read_again(
    scenario: Scenario, connection: str
) -> None:
    """One node removed and one added in the same second leave the total and ``updatedAt`` as
    they were; the continuation starts after the first page's last node, as GitHub's does, so
    the count disagrees and the source reads the pull request again."""
    repository, author = project(scenario)
    pull = repository.pulls[1]
    pull.add(connection, *(scenario.node(connection, by=author) for _ in range(101)))
    removed = pull.node[connection]["nodes"][5]["id"]

    def replace_one() -> None:
        pull.remove(connection, lambda node: node["id"] == removed)
        pull.add(connection, scenario.node(connection, by=author))

    scenario.after("PullRequestBatch", replace_one)
    record = hydrated(await hydrate(scenario, "example", "project", 1))
    read = record.comments if connection == "comments" else record.reviews
    assert [node.node_id for node in read] == [
        node["id"] for node in pull.node[connection]["nodes"]
    ]
    assert [seen.operation for seen in scenario.replay.seen].count("PullRequestBatch") == 2
    # What makes the cursor the only signal: the change kept the total and the time.
    assert (pull.node[connection]["totalCount"], pull.node["updatedAt"]) == (101, iso(START))


async def test_which_changes_move_updated_at(scenario: Scenario) -> None:
    issues = scenario.add_repository("example", "issues")
    repository, author = project(scenario, pulls=5)
    pulls = repository.pulls
    label = scenario.add_label(repository, "bug")
    pulls[2].add("comments", scenario.node("comments", by=author))
    pulls[4].link(1, issues)
    pulls[5].add("labels", label)
    scenario.clock.advance(60)
    pulls[1].add("comments", scenario.node("comments", by=author))
    pulls[2].remove("comments", lambda node: True)
    pulls[3].link(1, issues)
    pulls[4].unlink(1, issues)
    scenario.rename_label(repository, label["id"], "defect")
    async with scenario.source() as source:
        index = await source.index_pull_requests(await source.resolve("example", "project"))
    now = scenario.clock.now()
    assert [entry.updated_at for entry in index.entries] == [now, now, START, START, START]


async def test_a_pull_request_gone_between_its_pages_is_gone(scenario: Scenario) -> None:
    repository, author = project(scenario)
    pull = repository.pulls[1]
    pull.add("comments", *(scenario.node("comments", by=author) for _ in range(101)))

    def remove() -> None:
        pull.gone = True

    scenario.after("PullRequestBatch", remove)
    assert await hydrate(scenario, "example", "project", 1) == Gone(1)
    assert [seen.operation for seen in scenario.replay.seen].count("Overflow") == 1


async def test_a_renamed_label_keeps_its_id(scenario: Scenario) -> None:
    repository, _ = project(scenario)
    label = scenario.add_label(repository, "bug")
    repository.pulls[1].add("labels", label)
    scenario.clock.advance(300)
    scenario.rename_label(repository, label["id"], "defect")

    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        (outcome,) = await all_outcomes(source, resolved, [1])
    assert [(label.node_id, label.name, label.updated_at) for label in resolved.labels] == [
        (label["id"], "defect", scenario.clock.now())
    ]
    assert [(label.node_id, label.name) for label in hydrated(outcome).labels] == [
        (label["id"], "defect")
    ]


async def test_a_deleted_account(scenario: Scenario) -> None:
    repository, _ = project(scenario)
    leaving = scenario.add_account(login="leaving")
    kept = scenario.add_account(login="kept")
    pull = scenario.open_pull_request(repository, leaving)
    pull.merge(by=leaving)
    pull.add("comments", scenario.node("comments", by=leaving))
    pull.add("commits", scenario.node("commits", by=leaving))
    pull.add("reviewRequests", scenario.node("reviewRequests", by=leaving))
    pull.add(
        "assignedActors",
        scenario.node("assignedActors", by=leaving),
        scenario.node("assignedActors", by=kept),
    )
    scenario.delete_account(leaving)
    # A commit can still link the account a lookup no longer finds (gap 90).
    pull.add("commits", scenario.node("commits", by=leaving))

    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        (outcome,) = await all_outcomes(source, resolved, [pull.number])
        accounts, failed = await all_lookups(source, [leaving])
    record = hydrated(outcome)
    assert (record.author, record.comments[0].author) == (None, None)
    assert (record.head_repository, record.head_repository_owner, record.merged_by) == (None,) * 3
    assert (record.commits[0].author, record.commits[0].author_name) == (None, "leaving")
    assert [request.account for request in record.review_requests] == [None]
    assert [assignee.login for assignee in record.assignees] == ["kept"]
    assert record.totals.assignees == 1
    linked = record.commits[1].author
    assert linked is not None and (linked.node_id, linked.login) == (leaving, "leaving")
    assert (accounts, failed) == ({leaving: None}, {})

    scenario.restore_account(leaving)
    async with scenario.source() as source:
        accounts, _ = await all_lookups(source, [leaving])
    found = accounts[leaving]
    assert found is not None and found.account.login == "leaving"
    assert hydrated(await hydrate(scenario, "example", "project", pull.number)).author is None


async def test_a_deleted_account_leaves_its_compare_commits() -> None:
    model = Scenario.from_recordings(FakeClock())
    commits = model.repositories["prebid/prebid.js"].pulls[8858].compare
    assert commits is not None
    linked = {commit["author"]["node_id"] for commit in commits if commit["author"]}
    leaving = max(
        linked,
        key=lambda node_id: sum(
            1 for commit in commits if commit["author"] and commit["author"]["node_id"] == node_id
        ),
    )
    model.delete_account(leaving)
    record = hydrated(await hydrate(model, "prebid", "Prebid.js", 8858))
    assert record.commits_source == "compare"
    assert all(
        commit.author is None or commit.author.node_id != leaving for commit in record.commits
    )
    assert unanswered(model.replay) == []


async def test_a_pull_request_gone(scenario: Scenario) -> None:
    repository, _ = project(scenario)
    repository.pulls[1].gone = True
    assert await hydrate(scenario, "example", "project", 1) == Gone(1)
    assert await hydrate(scenario, "example", "project", 99) == Gone(99)
    async with scenario.source() as source:
        index = await source.index_pull_requests(await source.resolve("example", "project"))
    assert index.entries == ()


async def test_a_repository_gone(scenario: Scenario) -> None:
    repository, _ = project(scenario)
    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        repository.gone = True
        with pytest.raises(RepositoryNotFound):
            await source.resolve("example", "project")
        with pytest.raises(RepositoryShapeError, match="NOT_FOUND"):
            await source.index_pull_requests(resolved)
        with pytest.raises(RepositoryNotFound):
            await all_outcomes(source, resolved, [1])


async def test_a_repository_gone_between_its_label_pages(scenario: Scenario) -> None:
    repository, _ = project(scenario)
    for position in range(101):
        scenario.add_label(repository, f"label-{position}")

    def remove() -> None:
        repository.gone = True

    scenario.after("Resolve", remove)
    async with scenario.source() as source:
        with pytest.raises(RepositoryShapeError, match="errors on a label page"):
            await source.resolve("example", "project")


async def test_a_renamed_repository_is_followed(scenario: Scenario) -> None:
    repository, author = project(scenario)
    own = scenario.open_pull_request(repository, author, isCrossRepository=False)
    own.set(
        headRepository={
            "databaseId": repository.data["databaseId"],
            "nameWithOwner": "example/project",
        }
    )
    repository.pulls[1].link(7, repository)
    scenario.rename_repository(repository, "renamed")

    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        index = await source.index_pull_requests(resolved)
        outcomes = await all_outcomes(source, resolved, [1, own.number])
        by_new_name = await source.resolve("example", "renamed")
    assert (resolved.full_name, by_new_name.github_id) == ("example/renamed", resolved.github_id)
    assert [entry.number for entry in index.entries] == [1, own.number]
    linked, from_itself = (hydrated(outcome) for outcome in outcomes)
    assert [link.repo_full_name for link in linked.closing_issues] == ["example/renamed"]
    assert from_itself.head_repository is not None
    assert from_itself.head_repository.full_name == "example/renamed"
    names = {
        seen.variables.get("name")
        for seen in scenario.replay.seen
        if seen.operation in ("PullRequestIndex", "PullRequestBatch")
    }
    assert names == {"renamed"}


async def test_closing_links_beyond_the_index_cap(scenario: Scenario) -> None:
    issues = scenario.add_repository("example", "issues")
    other = scenario.add_repository("example", "other")
    repository, _ = project(scenario)
    pull = repository.pulls[1]
    for number in range(1, 11):
        pull.link(number, issues, user_linked=number == 1)
    pull.link(1, other)
    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        (entry,) = (await source.index_pull_requests(resolved)).entries
        (outcome,) = await all_outcomes(source, resolved, [1])
    assert (entry.closing_issues_total, len(entry.closing_issues), entry.updated_at) == (
        11,
        10,
        START,
    )
    assert [(link.number, link.user_linked) for link in hydrated(outcome).closing_issues] == [
        *((number, number == 1) for number in range(1, 11)),
        (1, False),
    ]

    pull.unlink(1, issues)
    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        (entry,) = (await source.index_pull_requests(resolved)).entries
        (outcome,) = await all_outcomes(source, resolved, [1])
    assert (entry.closing_issues_total, len(entry.closing_issues)) == (10, 10)
    links = hydrated(outcome).closing_issues
    assert [(link.repo_full_name, link.number, link.user_linked) for link in links] == [
        *(("example/issues", number, False) for number in range(2, 11)),
        ("example/other", 1, False),
    ]


async def test_a_connection_beyond_one_page(scenario: Scenario) -> None:
    repository, author = project(scenario)
    repository.pulls[1].add("comments", *(scenario.node("comments", by=author) for _ in range(101)))
    record = hydrated(await hydrate(scenario, "example", "project", 1))
    assert len(record.comments) == 101
    assert [seen.operation for seen in scenario.replay.seen].count("Overflow") == 1


# -- refusals ------------------------------------------------------------------------------------
def post(world: Scenario, query: str, variables: dict[str, Any]) -> httpx.Response:
    with httpx.Client(
        transport=world.replay.transport(), base_url="https://api.github.com"
    ) as client:
        return client.post("/graphql", json={"query": query, "variables": variables})


def test_a_document_the_model_was_not_checked_against_is_refused() -> None:
    world = Scenario(FakeClock())
    world.add_repository("example", "project")
    variables = {"owner": "example", "name": "project"}
    changed = documents.RESOLVE.replace("isPrivate", "isPrivate isFork")
    assert changed != documents.RESOLVE
    assert post(world, documents.RESOLVE, variables).status_code == 200
    assert post(world, changed, variables).status_code == 418
    assert world.replay.problems == [
        f"resolve unit {variables} was recorded for another query document: re-record with "
        "scripts/record_fixtures.py"
    ]


def test_a_missing_repository_is_answered_only_for_a_checked_document() -> None:
    world = Scenario(FakeClock())
    variables = {"owner": "example", "name": "absent", "after": None}
    changed = documents.PULL_REQUEST_INDEX.replace("isDraft", "isDraft title")
    answered = post(world, documents.PULL_REQUEST_INDEX, variables)
    assert (answered.status_code, answered.json()["data"]["repository"]) == (200, None)
    assert post(world, changed, variables).status_code == 418
    batch = documents.pull_request_batch(1)
    changed_batch = batch.replace("additions deletions", "additions")
    assert changed_batch != batch
    numbered = {"owner": "example", "name": "absent", "n0": 1}
    assert post(world, batch, numbered).json()["data"]["repository"] is None
    assert post(world, changed_batch, numbered).status_code == 418
    assert world.replay.problems == [
        f"{kind} unit {{'owner': 'example', 'name': 'absent', 'for': '{kind}'}} was recorded for "
        "another query document: re-record with scripts/record_fixtures.py"
        for kind in ("index", "pull_request")
    ]


async def test_a_continuation_no_recording_covers_is_left_unanswered() -> None:
    """No recording continues labels, so the model renders no label continuation: the replay
    reports the request as one to record, and the source stops on its answer."""
    world = Scenario(FakeClock())
    repository, _ = project(world)
    pull = repository.pulls[1]
    for position in range(101):
        pull.add("labels", world.add_label(repository, f"label-{position}"))
    async with world.source() as source:
        resolved = await source.resolve("example", "project")
        with pytest.raises(FatalSourceError):
            await all_outcomes(source, resolved, [1])
    key = {"id": pull.node["id"], "connection": "labels", "after": "MTAw"}
    assert world.replay.problems == [
        f"no overflow unit for {key}: record it with scripts/record_fixtures.py"
    ]


@pytest.mark.parametrize(
    ("connection", "cursor"),
    [("comments", "unserved"), ("comments", "scenario:[1]"), ("commits", "NTA")],
)
async def test_a_cursor_the_model_never_served_is_reported(connection: str, cursor: str) -> None:
    """A cursor in the model's own form, or GitHub's offset form (``NTA``, base64 of 50), that the
    model did not serve names no page; the failure is the replay's problem, not an error inside
    the transport."""
    world = Scenario(FakeClock())
    repository, author = project(world)
    repository.pulls[1].add(connection, *(world.node(connection, by=author) for _ in range(101)))

    def forged(body: dict[str, Any]) -> None:
        body["data"]["repository"]["p0"][connection]["pageInfo"]["endCursor"] = cursor

    world.replay.modify(forged, when=lambda seen: seen.operation == "PullRequestBatch")
    async with world.source() as source:
        resolved = await source.resolve("example", "project")
        with pytest.raises(FatalSourceError):
            await all_outcomes(source, resolved, [1])
    key = {"id": repository.pulls[1].node["id"], "connection": connection, "after": cursor}
    message = f"cursor {cursor!r} names no page the model served"
    assert world.replay.problems == [
        f"the provider failed on overflow {key}: ScenarioError({message!r})"
    ]


def test_unanswered_lists_problems_then_unused_faults() -> None:
    replay = Replay()
    replay.fault(lambda request: synthetic("server_error"))
    replay.problems.append("no unit")
    assert unanswered(replay) == ["no unit", "fault 0: served 0, 1 left"]


async def test_a_bot_commit_links_its_bot_login(scenario: Scenario) -> None:
    """A commit links a bot as ``<login>[bot]`` (Prebid.js#15724's recording), where a lookup
    and an actor name it by its login."""
    repository, _ = project(scenario)
    bot = scenario.add_account("Bot", login="dependabot")
    repository.pulls[1].add("commits", scenario.node("commits", by=bot))
    record = hydrated(await hydrate(scenario, "example", "project", 1))
    (commit,) = record.commits
    assert commit.author is not None and (commit.author.node_id, commit.author.login) == (
        bot,
        "dependabot[bot]",
    )
    databaseId = scenario.account(bot)["databaseId"]
    assert (commit.author_name, commit.author_email) == (
        "dependabot[bot]",
        f"{databaseId}+dependabot[bot]@users.noreply.github.com",
    )
    async with scenario.source() as source:
        accounts, _ = await all_lookups(source, [bot])
    found = accounts[bot]
    assert found is not None and found.account.login == "dependabot"


async def test_a_push_during_hydration_is_read_again(scenario: Scenario) -> None:
    """A file replaced in the same second keeps the total and ``updatedAt``; the push moves the
    head, so the continuation shows the change and the source reads the pull request again."""
    repository, _ = project(scenario)
    pull = repository.pulls[1]
    pull.add("files", *(scenario.node("files") for _ in range(101)))
    head = pull.node["headRefOid"]
    removed = pull.node["files"]["nodes"][5]["path"]

    def replace_one() -> None:
        pull.remove("files", lambda node: node["path"] == removed)
        pull.add("files", scenario.node("files"))

    scenario.after("PullRequestBatch", replace_one)
    record = hydrated(await hydrate(scenario, "example", "project", 1))
    assert [file.path for file in record.files] == [
        node["path"] for node in pull.node["files"]["nodes"]
    ]
    assert record.head_ref_oid == pull.node["headRefOid"] != head


async def test_a_pull_request_above_250_commits_reads_its_compare(scenario: Scenario) -> None:
    repository, author = project(scenario)
    pull = repository.pulls[1]
    pull.add("commits", *(scenario.node("commits", by=author) for _ in range(251)))
    record = hydrated(await hydrate(scenario, "example", "project", 1))
    assert record.commits_source == "compare"
    assert [commit.sha for commit in record.commits] == [
        node["commit"]["oid"] for node in pull.node["commits"]["nodes"]
    ]


async def test_one_issue_keeps_one_id(scenario: Scenario) -> None:
    issues = scenario.add_repository("example", "issues")
    repository, _ = project(scenario, pulls=2)
    first, second = repository.pulls[1], repository.pulls[2]
    first.link(7, issues)
    second.link(7, issues)
    first.unlink(7, issues)
    first.link(7, issues)
    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        outcomes = await all_outcomes(source, resolved, [1, 2])
    ids = {link.github_id for outcome in outcomes for link in hydrated(outcome).closing_issues}
    assert ids == {scenario.issue_id(issues.data["databaseId"], 7)}


async def test_a_clock_in_another_zone_still_dates_in_utc() -> None:
    world = Scenario(FakeClock(START.astimezone(timezone(timedelta(hours=2)))))
    project(world)
    async with world.source() as source:
        resolved = await source.resolve("example", "project")
    assert resolved.resolved_at == START
    assert unanswered(world.replay) == []


async def test_a_repository_gone_between_a_batch_and_its_overflow(scenario: Scenario) -> None:
    repository, author = project(scenario)
    repository.pulls[1].add("comments", *(scenario.node("comments", by=author) for _ in range(101)))

    def remove() -> None:
        repository.gone = True

    scenario.after("PullRequestBatch", remove)
    async with scenario.source() as source:
        resolved = await source.resolve("example", "project")
        with pytest.raises(RepositoryNotFound):
            await all_outcomes(source, resolved, [1])
    assert "Overflow" in [seen.operation for seen in scenario.replay.seen]


async def test_what_the_model_leaves_unanswered() -> None:
    """A pull request known only from an index recording, and the rate-limit readings."""
    model = Scenario.from_recordings(FakeClock())
    async with model.source() as source:
        resolved = await source.resolve("prebid", "prebid-cache-java")
        with pytest.raises(FatalSourceError):
            await all_outcomes(source, resolved, [4])
        with pytest.raises(FatalSourceError):
            await source.rate_limits()
    key = {"owner": "prebid", "name": "prebid-cache-java", "number": 4}
    assert model.replay.problems[0] == (
        f"no pull_request unit for {key}: record it with scripts/record_fixtures.py"
    )
    assert any("rate_limit unit" in problem for problem in model.replay.problems[1:])


def test_an_account_the_model_never_held_is_named_so(scenario: Scenario) -> None:
    repository = scenario.add_repository("example", "project")
    with pytest.raises(ScenarioError, match="no account User_typo: add it with add_account"):
        scenario.open_pull_request(repository, "User_typo")
    with pytest.raises(ScenarioError, match="no account User_typo"):
        scenario.node("commits", by="User_typo")
