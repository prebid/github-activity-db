"""The recording format: split and replay agree, and every unit matches the current documents."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from github_activity_db.source import documents
from tests.source.recordings import (
    RECORDED,
    Unit,
    expected_overflow_stamp,
    expected_pull_request_stamp,
    overflow_stamp,
    pull_request_stamp,
    split_graphql,
    whole_stamp,
)
from tests.source.replay import Replay

UNITS = sorted(RECORDED.rglob("*.json"))
WHOLE_DOCUMENTS = {
    "resolve": documents.RESOLVE,
    "repository_labels": documents.REPOSITORY_LABELS,
    "index": documents.PULL_REQUEST_INDEX,
    "user": documents.USER_LOOKUP,
}


def test_fixtures_were_recorded() -> None:
    kinds = {Unit.load(path).kind for path in UNITS}
    assert {"resolve", "index", "pull_request", "overflow", "compare", "user"} <= kinds


@pytest.mark.parametrize("path", UNITS, ids=lambda path: str(path.relative_to(RECORDED)))
def test_unit_stamp_matches_current_documents(path: Path) -> None:
    unit = Unit.load(path)
    match unit.kind:
        case "pull_request":
            expected = expected_pull_request_stamp()
        case "overflow":
            expected = expected_overflow_stamp(unit.key["connection"])
        case "compare":
            return
        case kind:
            expected = whole_stamp(WHOLE_DOCUMENTS[kind])
    assert unit.document_sha256 == expected, f"re-record {path}: scripts/record_fixtures.py"


@pytest.mark.parametrize("path", UNITS, ids=lambda path: str(path.relative_to(RECORDED)))
def test_unit_keeps_no_request_headers(path: Path) -> None:
    unit = Unit.load(path)
    assert set(unit.headers) <= {"date", "retry-after"} | {
        name for name in unit.headers if name.startswith("x-ratelimit-")
    }
    text = path.read_text().lower()
    assert "authorization" not in text
    assert "ghp_" not in text
    assert "github_pat_" not in text


def test_stamps_are_hashed_from_the_request_text() -> None:
    batch = documents.pull_request_batch(3)
    assert pull_request_stamp(batch) == expected_pull_request_stamp()
    for connection in documents.PULL_REQUEST_CONNECTIONS:
        query = documents.overflow_continuation([documents.LABELS, connection])
        assert overflow_stamp(query, 1, connection.field) == expected_overflow_stamp(
            connection.field
        )


def test_a_changed_document_changes_the_stamp() -> None:
    changed = documents.pull_request_batch(1).replace(
        "additions deletions changedFiles", "additions"
    )
    assert pull_request_stamp(changed) != expected_pull_request_stamp()


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
