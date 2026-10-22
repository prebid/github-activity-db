"""The recording format: split and replay agree, and every unit matches the current documents."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_activity_db.source import GitHubSource, Hydrated, PullRequestOutcome, documents
from github_activity_db.source.errors import UnexpectedResponse
from github_activity_db.source.limits import OVERFLOW_ALIASES_PER_REQUEST, PR_BATCH_SIZE
from tests.source import recordings
from tests.source.conftest import FakeClock, all_outcomes
from tests.source.recordings import (
    COMPARE_STAMP,
    RECORDED,
    REST_RATE_LIMIT_STAMP,
    Unit,
    canonical_batch,
    canonical_overflow,
    compare_key,
    expected_overflow_stamp,
    expected_pull_request_stamp,
    overflow_stamp,
    pull_request_stamp,
    split_graphql,
    whole_stamp,
)
from tests.source.replay import Replay, unused_faults
from tests.source.test_hydrate import SECOND_PAGES, copied, copies, with_second_pages

UNITS = sorted(RECORDED.rglob("*.json"))
WHOLE_DOCUMENTS = {
    "resolve": documents.RESOLVE,
    "repository_labels": documents.REPOSITORY_LABELS,
    "index": documents.PULL_REQUEST_INDEX,
    "user": documents.USER_LOOKUP,
    "rate_limit": documents.RATE_LIMIT_QUERY,
}
# Units the recorder wrote at Step 2 (c1f474b), for the documents of hydration_version 1,
# committed unchanged (tests/fixtures/history/README.md).
STEP_2 = (
    Path(__file__).resolve().parents[1] / "fixtures/history/c1f474b/tests/fixtures/source/recorded"
)


def test_fixtures_were_recorded() -> None:
    kinds = {Unit.load(path).kind for path in UNITS}
    # Every kind of unit is recorded. Within overflow, the labels and closing-issue continuations
    # have only synthetic pages: no tracked PR has more than 100 of either (docs/testing.md).
    assert kinds == {
        "resolve",
        "repository_labels",
        "index",
        "pull_request",
        "overflow",
        "compare",
        "user",
        "rate_limit",
        "rest_rate_limit",
    }


@pytest.mark.parametrize("path", UNITS, ids=lambda path: str(path.relative_to(RECORDED)))
def test_unit_stamp_matches_current_documents(path: Path) -> None:
    unit = Unit.load(path)
    match unit.kind:
        case "pull_request":
            expected = expected_pull_request_stamp()
        case "overflow":
            expected = expected_overflow_stamp(unit.key["connection"])
        case "compare":
            expected = whole_stamp(COMPARE_STAMP)
        case "rest_rate_limit":
            expected = whole_stamp(REST_RATE_LIMIT_STAMP)
        case kind:
            expected = whole_stamp(WHOLE_DOCUMENTS[kind])
    assert unit.document_sha256 == expected, f"re-record {path}: scripts/record_fixtures.py"


@pytest.mark.parametrize("path", UNITS, ids=lambda path: str(path.relative_to(RECORDED)))
def test_unit_keeps_no_request_headers(path: Path) -> None:
    unit = Unit.load(path)
    assert set(unit.headers) <= {"date", "retry-after"} | {
        name for name in unit.headers if name.startswith("x-ratelimit-")
    }
    text = path.read_text()
    assert "authorization" not in text.lower()
    assert not re.search(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}", text)


@pytest.mark.parametrize("size", range(1, PR_BATCH_SIZE + 1))
def test_a_batch_stamp_does_not_depend_on_its_size(size: int) -> None:
    """At every size the hydrator sends, the stamp hashes the whole one-alias document, its
    repository wrapper and rateLimit included."""
    batch = documents.pull_request_batch(size)
    canonical = canonical_batch(batch)
    assert canonical == documents.normalize(documents.pull_request_batch(1))
    assert "repository(owner: $owner, name: $name)" in canonical
    assert documents.normalize(documents.RATE_LIMIT) in canonical
    assert pull_request_stamp(batch) == expected_pull_request_stamp()


@pytest.mark.parametrize("connection", documents.PULL_REQUEST_CONNECTIONS, ids=lambda c: c.key)
def test_a_continuation_stamp_does_not_depend_on_its_neighbours(connection) -> None:
    """The stamp hashes the one-alias continuation: its block, the rateLimit it selects, and the
    fragment it uses."""
    query = documents.overflow_continuation([documents.LABELS, documents.REVIEWS, connection])
    alone = documents.overflow_continuation([connection])
    assert canonical_overflow(query, 2, connection.key) == documents.normalize(alone)
    assert overflow_stamp(query, 2, connection.key) == expected_overflow_stamp(connection.key)


async def _hydrate(
    replay: Replay, owner: str, name: str, numbers: list[int]
) -> list[PullRequestOutcome]:
    """The real source over ``replay``: resolve ``owner/name``, then hydrate ``numbers``."""
    source = GitHubSource("test-token", http_transport=replay.transport(), clock=FakeClock())
    async with source:
        repository = await source.resolve(owner, name)
        return await all_outcomes(source, repository, numbers)


@pytest.mark.parametrize(
    ("path", "lookup", "expected"),
    [
        pytest.param(
            "pull_request/prebid__Prebid.js/13481.json",
            {"owner": "prebid", "name": "Prebid.js", "number": 13481},
            expected_pull_request_stamp(),
            id="pull_request",
        ),
        pytest.param(
            "overflow/PR_kwDOBUaT8s6WsAjJ/comments/0e3c0d53c5b8c0eb.json",
            {
                "id": "PR_kwDOBUaT8s6WsAjJ",
                "connection": "comments",
                "after": "Y3Vyc29yOnYyOpHPAAAAAU7JU7k=",
            },
            expected_overflow_stamp("comments"),
            id="overflow",
        ),
        pytest.param(
            "resolve/prebid__go-gdpr.json",
            {"owner": "prebid", "name": "go-gdpr"},
            whole_stamp(documents.RESOLVE),
            id="resolve",
        ),
    ],
)
async def test_a_unit_recorded_for_an_earlier_document_is_refused(
    path: str, lookup: dict[str, Any], expected: str
) -> None:
    """A unit the recorder wrote for Step 2's documents, in place of the current recording: the
    replay refuses it, and the source stops on the answer, so a document changed without
    re-recording fails the suite."""
    earlier = Unit.load(STEP_2 / path)
    current = Unit.load(RECORDED / path)
    assert (earlier.kind, earlier.key) == (current.kind, current.key)
    assert current.document_sha256 == expected != earlier.document_sha256
    replay = Replay.from_fixtures()
    replay.add(earlier)
    with pytest.raises(UnexpectedResponse) as raised:
        match earlier.kind:
            case "resolve":
                await _hydrate(replay, lookup["owner"], lookup["name"], [])
            case "pull_request":
                await _hydrate(replay, "prebid", "Prebid.js", [lookup["number"]])
            case _:
                await _hydrate(replay, "prebid", "prebid-server", [4343])  # 109 comments
    assert raised.value.status == 418
    assert replay.problems == [
        f"{earlier.kind} unit {lookup} was recorded for another query document: re-record with "
        "scripts/record_fixtures.py"
    ]


def _refused(replay: Replay, payload: dict) -> list[str]:
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    response = replay.transport().handle_request(request)
    assert response.status_code == 418
    return replay.problems


def _first_refused(replay: Replay, payload: dict) -> str:
    """The first problem the replay records for a request it answers 418."""
    return _refused(replay, payload)[0]


def test_the_replay_refuses_a_request_it_has_no_unit_for() -> None:
    key = {"owner": "prebid", "name": "Prebid.js", "number": 1}
    payload = {
        "query": documents.pull_request_batch(1),
        "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 1},
    }
    assert _refused(Replay.from_fixtures(), payload) == [
        f"no pull_request unit for {key}: record it with scripts/record_fixtures.py"
    ]


@pytest.mark.parametrize("size", range(1, PR_BATCH_SIZE + 1))
async def test_each_batch_the_hydrator_sends_binds_its_own_numbers(size: int) -> None:
    """Alias ``p<i>`` reads ``$n<i>``, in order, and the document declares exactly the numbers
    sent: the replay's check passes every batch size the hydrator sends."""
    replay = Replay.from_fixtures()
    numbers = copies(replay, size)
    outcomes = await _hydrate(replay, "prebid", "Prebid.js", numbers)
    assert replay.problems == []
    assert [o.number for o in outcomes if isinstance(o, Hydrated)] == numbers
    batches = [seen for seen in replay.seen if seen.operation == "PullRequestBatch"]
    assert [seen.numbers for seen in batches] == [numbers]
    assert [len(seen.variables) for seen in batches] == [size + 2]  # and owner and name


@pytest.mark.parametrize("aliases", range(1, OVERFLOW_ALIASES_PER_REQUEST + 1))
async def test_each_continuation_request_binds_its_own_variables(aliases: int) -> None:
    """Alias ``o<i>_*`` reads ``$id<i>`` and ``$after<i>`` only, each declared once and sent: the
    replay's check passes every continuation request size, mixing connections."""
    replay = Replay.from_fixtures()
    numbers = [900_001 + i for i in range(aliases)]
    connections = [list(SECOND_PAGES)[i % len(SECOND_PAGES)] for i in range(aliases)]
    for number, connection in zip(numbers, connections, strict=True):
        with_second_pages(replay, copied(replay, 12975, number), (connection,))
    outcomes = await _hydrate(replay, "prebid", "Prebid.js", numbers)
    assert replay.problems == []
    records = [outcome.record for outcome in outcomes if isinstance(outcome, Hydrated)]
    assert [record.number for record in records] == numbers
    # Each item's second page reached it: one more entry than the recorded first page holds.
    assert [
        len(record.labels if connection == "labels" else record.closing_issues)
        for record, connection in zip(records, connections, strict=True)
    ] == [2] * aliases
    overflow = [seen for seen in replay.seen if seen.operation == "Overflow"]
    assert [len(seen.variables) for seen in overflow] == [2 * aliases]


async def test_the_compare_requests_the_source_sends_find_their_units() -> None:
    """A compare request carries exactly ``page`` and ``per_page``, the parameters
    ``COMPARE_STAMP`` names, so each one the source sends finds its unit."""
    replay = Replay.from_fixtures()
    (outcome,) = await _hydrate(replay, "prebid", "Prebid.js", [8858])  # 254 commits
    assert isinstance(outcome, Hydrated)
    assert outcome.record.commits_source == "compare"
    requested = [compare_key(seen.path) for seen in replay.seen if seen.operation == "rest"]
    recorded = [unit.key for unit in replay.units.values() if unit.kind == "compare"]
    assert sorted(requested, key=json.dumps) == sorted(recorded, key=json.dumps)
    assert len(requested) == len(recorded)
    assert replay.problems == []


def test_a_fault_never_served_or_not_used_up_is_reported() -> None:
    replay = Replay.from_fixtures()
    replay.fault(lambda request: httpx.Response(500), when=lambda seen: seen.operation == "Resolve")
    replay.fault(lambda request: httpx.Response(500), times=2)
    assert unused_faults(replay) == ["fault 0: served 0, 1 left", "fault 1: served 0, 2 left"]
    payload = {"query": documents.RESOLVE, "variables": {"owner": "prebid", "name": "go-gdpr"}}
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    replay.transport().handle_request(request)
    assert unused_faults(replay) == ["fault 1: served 0, 2 left"]
    replay.transport().handle_request(request)
    replay.transport().handle_request(request)
    assert unused_faults(replay) == []


def test_splitting_a_replayed_batch_gives_back_its_units() -> None:
    replay = Replay.from_fixtures()
    numbers = [15724, 999999, 13481]
    payload = {
        "query": documents.pull_request_batch(len(numbers)),
        "variables": {
            "owner": "prebid",
            "name": "Prebid.js",
            **{f"n{i}": number for i, number in enumerate(numbers)},
        },
    }
    transport = replay.transport()
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    response = transport.handle_request(request)
    body = json.loads(response.read())
    units = split_graphql(payload, body, dict(response.headers), "now")
    for unit in units:
        original = replay.units[("pull_request", json.dumps(unit.key, sort_keys=True))]
        assert unit.data == original.data
        assert [
            {key: value for key, value in error.items() if key != "locations"}
            for error in unit.errors
        ] == [
            {key: value for key, value in error.items() if key != "locations"}
            for error in original.errors
        ]
        assert unit.document_sha256 == original.document_sha256
    assert replay.problems == []


@pytest.mark.parametrize(
    ("document", "edit"),
    [
        (
            documents.pull_request_batch(2),
            ("repository(owner: $owner, name: $name)", "repository(name: $name, owner: $owner)"),
        ),
        (documents.pull_request_batch(2), ("rateLimit { cost remaining resetAt }", "")),
        (documents.pull_request_batch(2), ("additions deletions changedFiles", "additions")),
    ],
    ids=["batch-wrapper", "batch-rate-limit", "batch-fields"],
)
def test_a_changed_batch_document_changes_the_stamp(document: str, edit: tuple[str, str]) -> None:
    assert edit[0] in document
    assert pull_request_stamp(document.replace(*edit)) != expected_pull_request_stamp()


@pytest.mark.parametrize(
    "edit",
    [("headRefOid updatedAt ", ""), ("rateLimit { cost remaining resetAt }", "")],
    ids=["continuation-head", "continuation-rate-limit"],
)
def test_a_changed_continuation_document_changes_the_stamp(edit: tuple[str, str]) -> None:
    query = documents.overflow_continuation([documents.FILES])
    assert edit[0] in query
    assert overflow_stamp(query.replace(*edit), 0, "files") != expected_overflow_stamp("files")


def test_the_replay_refuses_a_request_for_another_document() -> None:
    replay = Replay.from_fixtures()
    query = documents.pull_request_batch(1).replace("additions deletions changedFiles", "additions")
    payload = {"query": query, "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481}}
    request = httpx.Request("POST", "https://api.github.com/graphql", json=payload)
    response = replay.transport().handle_request(request)
    assert response.status_code == 418
    assert "re-record" in replay.problems[0]


def test_the_replay_refuses_a_batch_alias_bound_to_another_number() -> None:
    query = documents.pull_request_batch(2).replace(
        "p1: pullRequest(number: $n1)", "p1: pullRequest(number: $n0)"
    )
    assert "pullRequest(number: $n0)" in query
    payload = {
        "query": query,
        "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481, "n1": 15724},
    }
    assert "bind" in _first_refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_bound_to_another_cursor() -> None:
    connections = [documents.CONNECTIONS_BY_KEY["commits"]] * 2
    query = documents.overflow_continuation(connections)
    block = recordings.extract_block(query, "o1_commits: node(")
    query = query.replace(block, block.replace("$after1", "$after0"))
    payload = {
        "query": query,
        "variables": {"id0": "PR_a", "after0": "x", "id1": "PR_b", "after1": "y"},
    }
    assert "binds" in _first_refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_batch_declaring_an_unsent_number() -> None:
    query = documents.pull_request_batch(1).replace("$n0: Int!", "$n0: Int! $n1: Int!")
    payload = {"query": query, "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481}}
    assert "declares" in _first_refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_with_an_unbound_variable() -> None:
    query = documents.overflow_continuation([documents.CONNECTIONS_BY_KEY["commits"]])
    payload = {"query": query, "variables": {"id0": "PR_a", "after0": "x", "id1": "PR_b"}}
    assert "variables" in _first_refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_batch_aliases_out_of_order() -> None:
    query = documents.pull_request_batch(2)
    first = "p0: pullRequest(number: $n0) { ...PullRequestFields }"
    second = "p1: pullRequest(number: $n1) { ...PullRequestFields }"
    query = query.replace(f"{first} {second}", f"{second} {first}")
    assert query.index("p1:") < query.index("p0:")
    payload = {
        "query": query,
        "variables": {"owner": "prebid", "name": "Prebid.js", "n0": 13481, "n1": 15724},
    }
    assert "bind" in _first_refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_reading_another_alias_id() -> None:
    connections = [documents.CONNECTIONS_BY_KEY["commits"]] * 2
    query = documents.overflow_continuation(connections).replace(
        "o1_commits: node(id: $id1)", "o1_commits: node(id: $id0)"
    )
    payload = {
        "query": query,
        "variables": {"id0": "PR_a", "after0": "x", "id1": "PR_b", "after1": "y"},
    }
    assert "binds" in _first_refused(Replay.from_fixtures(), payload)


def test_the_replay_refuses_a_continuation_declaring_an_unused_alias() -> None:
    query = documents.overflow_continuation([documents.CONNECTIONS_BY_KEY["commits"]]).replace(
        "$id0: ID! $after0: String!", "$id0: ID! $after0: String! $id1: ID! $after1: String!"
    )
    payload = {"query": query, "variables": {"id0": "PR_a", "after0": "x"}}
    assert "declarations" in _first_refused(Replay.from_fixtures(), payload)


@pytest.mark.parametrize(
    "query",
    ["per_page=100&page=1&sha=main", "per_page=100", "per_page=100&page=1&page=2"],
    ids=["extra-parameter", "missing-page", "repeated-page"],
)
def test_a_compare_request_with_other_parameters_finds_no_unit(query: str) -> None:
    base = "/repos/prebid/Prebid.js/compare/" + "a" * 40 + "..." + "b" * 40
    assert compare_key(f"https://api.github.com{base}?{query}") is None
    assert compare_key(f"https://api.github.com{base}?per_page=100&page=1") is not None
