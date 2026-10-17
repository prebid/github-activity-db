"""Recorded GitHub responses: one file per unit, shared by the recorder and the replay.

A unit is the smallest piece of a response that a request can be rebuilt from: one resolve, one
label page, one index page, one pull request alias, one overflow continuation, one user, one
compare page, one GraphQL rate-limit reading, one REST ``/rate_limit`` body. The recorder
(``scripts/record_fixtures.py``) splits each live response into units; the replay transport
assembles any batch, split, or retry from them, so a change of batch size needs no new recording.

Every unit is stamped with the SHA-256 of the normalized query document it answers. The replay
hashes the document in each request it receives and refuses a unit whose stamp differs, so a
query that changes without re-recording fails the suite (redesign plan Step 2).

Units keep the response body and the response headers the source layer reads (``date``,
``retry-after``, ``x-ratelimit-*``), never request headers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from github_activity_db.source import documents

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "source"
RECORDED = FIXTURES / "recorded"
SYNTHETIC = FIXTURES / "synthetic"

KEPT_HEADERS = ("date", "retry-after")
KEPT_HEADER_PREFIX = "x-ratelimit-"

# The REST request compare units answer. Compare has no query document; edit this text when the
# compare request changes, and the replay refuses units recorded for the old request.
COMPARE_STAMP = "rest:compare/{base}...{head}?per_page&page"
# The same for REST /rate_limit, which ``ghactivity github rate-limit`` reads without parameters.
REST_RATE_LIMIT_STAMP = "rest:rate_limit"

_COMPARE_PATH = re.compile(
    r"^/repos/(?P<owner>[^/]+)/(?P<name>[^/]+)/compare/(?P<base>[0-9a-f]+)\.\.\.(?P<head>[0-9a-f]+)$"
)


@dataclass
class Unit:
    """One recorded piece of a response."""

    kind: str
    key: dict[str, Any]
    document_sha256: str
    recorded_at: str
    headers: dict[str, str]
    data: Any
    errors: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    source: str = "live"

    def path(self) -> Path:
        return unit_path(self.kind, self.key)

    def dumps(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def load(cls, path: Path) -> Unit:
        return cls(**json.loads(path.read_text()))


def unit_path(kind: str, key: dict[str, Any]) -> Path:
    """Where a unit lives, relative to RECORDED or SYNTHETIC."""
    match kind:
        case "resolve":
            return Path("resolve") / f"{key['owner']}__{key['name']}.json"
        case "repository_labels":
            return Path("repository_labels") / key["id"] / f"{_short(key['after'])}.json"
        case "index":
            return Path("index") / f"{key['owner']}__{key['name']}" / f"{_short(key['after'])}.json"
        case "pull_request":
            return Path("pull_request") / f"{key['owner']}__{key['name']}" / f"{key['number']}.json"
        case "overflow":
            return Path("overflow") / key["id"] / key["connection"] / f"{_short(key['after'])}.json"
        case "user":
            return Path("user") / f"{key['id']}.json"
        case "rate_limit":
            return Path("rate_limit") / "graphql.json"
        case "rest_rate_limit":
            return Path("rate_limit") / "rest.json"
        case "compare":
            return (
                Path("compare")
                / f"{key['owner']}__{key['name']}"
                / f"{key['base'][:12]}...{key['head'][:12]}"
                / f"page-{key['page']}.json"
            )
    raise ValueError(f"unknown unit kind {kind!r}")


def _short(cursor: str | None) -> str:
    if cursor is None:
        return "first"
    return hashlib.sha256(cursor.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------------
# Document stamps: hashed from the text a request actually carries
# --------------------------------------------------------------------------------------------
def extract_block(text: str, start: str) -> str:
    """The block of ``text`` beginning at ``start`` through its matching closing brace."""
    begin = text.index(start)
    depth = 0
    for position in range(text.index("{", begin), len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[begin : position + 1]
    raise ValueError(f"unbalanced block at {start!r}")


def canonical_batch(query: str) -> str:
    """A batch request with its aliases collapsed to one, so any batch size compares equal."""
    text = documents.normalize(query)
    text = re.sub(r"\$n\d+", "$n0", text)
    text = re.sub(r"\bp\d+:", "p0:", text)
    text = re.sub(r"\$n0: Int!(?: \$n0: Int!)+", "$n0: Int!", text)
    alias = re.escape("p0: pullRequest(number: $n0) { ...PullRequestFields }")
    return re.sub(
        rf"{alias}(?: {alias})+", "p0: pullRequest(number: $n0) { ...PullRequestFields }", text
    )


def pull_request_stamp(query: str) -> str:
    """Stamp of a batch request: the whole document, wrapper included, at any batch size."""
    return documents.document_hash(canonical_batch(query))


def canonical_overflow(query: str, index: int, connection: str) -> str:
    """One continuation of a request, as the one-alias document it would be on its own."""
    text = documents.normalize(query)
    block = extract_block(text, f"o{index}_{connection}: node(id: $id{index})")
    block = (
        block.replace(f"o{index}_", "o0_", 1)
        .replace(f"$id{index}", "$id0")
        .replace(f"$after{index}", "$after0")
    )
    rate_limit = documents.normalize(documents.RATE_LIMIT)
    selected_rate_limit = f" {rate_limit}" if rate_limit in text else ""
    fragment = (
        f" {extract_block(text, 'fragment Actor on Actor')}"
        if "...Actor" in block and "fragment Actor on Actor" in text
        else ""
    )
    return (
        f"query Overflow($id0: ID! $after0: String!) {{ {block}{selected_rate_limit} }}{fragment}"
    )


def overflow_stamp(query: str, index: int, connection: str) -> str:
    """Stamp of one continuation: its alias block, the rateLimit it selects, its fragment."""
    return documents.document_hash(canonical_overflow(query, index, connection))


def expected_overflow_stamp(connection: str) -> str:
    """What a current continuation of ``connection`` hashes to."""
    return documents.document_hash(
        documents.overflow_continuation([documents.CONNECTIONS_BY_FIELD[connection]])
    )


def expected_pull_request_stamp() -> str:
    return documents.document_hash(documents.pull_request_batch(1))


def whole_stamp(query: str) -> str:
    return documents.document_hash(query)


# --------------------------------------------------------------------------------------------
# Splitting live responses into units (the recorder)
# --------------------------------------------------------------------------------------------
def kept_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in KEPT_HEADERS or name.lower().startswith(KEPT_HEADER_PREFIX)
    }


def relative_errors(errors: list[dict[str, Any]], prefix: list[Any]) -> list[dict[str, Any]]:
    """Errors under ``prefix``, their paths made relative to it."""
    relative = []
    for error in errors:
        path = error.get("path") or []
        if path[: len(prefix)] == prefix:
            kept = {key: value for key, value in error.items() if key != "locations"}
            relative.append({**kept, "path": path[len(prefix) :]})
    return relative


def split_graphql(
    payload: dict[str, Any], body: dict[str, Any], headers: dict[str, str], recorded_at: str
) -> list[Unit]:
    """Units of one GraphQL request/response pair, by operation."""
    query: str = payload["query"]
    variables: dict[str, Any] = payload.get("variables") or {}
    operation = re.match(r"\s*query (\w+)", query)
    if operation is None:
        raise ValueError("query without an operation name")
    data = body.get("data") or {}
    errors = body.get("errors") or []
    kept = kept_headers(headers)

    def unit(
        kind: str, key: dict[str, Any], stamp: str, data: Any, prefix: list[Any], **extra: Any
    ) -> Unit:
        return Unit(
            kind=kind,
            key=key,
            document_sha256=stamp,
            recorded_at=recorded_at,
            headers=kept,
            data=data,
            errors=relative_errors(errors, prefix),
            extra=extra,
        )

    name = operation.group(1)
    if name == "Resolve":
        key = {"owner": variables["owner"], "name": variables["name"]}
        return [unit("resolve", key, whole_stamp(query), data.get("repository"), ["repository"])]
    if name == "RepositoryLabels":
        key = {"id": variables["id"], "after": variables["after"]}
        return [unit("repository_labels", key, whole_stamp(query), data.get("node"), ["node"])]
    if name == "PullRequestIndex":
        key = {
            "owner": variables["owner"],
            "name": variables["name"],
            "after": variables.get("after"),
        }
        repository = data.get("repository") or {}
        return [
            unit("index", key, whole_stamp(query), repository.get("pullRequests"), ["repository"])
        ]
    if name == "PullRequestBatch":
        repository = data.get("repository") or {}
        stamp = pull_request_stamp(query)
        units = []
        for variable, number in sorted(variables.items()):
            if not re.fullmatch(r"n\d+", variable):
                continue
            alias = f"p{variable[1:]}"
            key = {"owner": variables["owner"], "name": variables["name"], "number": number}
            units.append(
                unit(
                    "pull_request",
                    key,
                    stamp,
                    repository.get(alias),
                    ["repository", alias],
                )
            )
        return units
    if name == "Overflow":
        units = []
        for alias in re.findall(r"\b(o\d+_\w+): node\(", query):
            index_text, connection = alias[1:].split("_", 1)
            index = int(index_text)
            key = {
                "id": variables[f"id{index}"],
                "connection": connection,
                "after": variables[f"after{index}"],
            }
            stamp = overflow_stamp(query, index, connection)
            units.append(unit("overflow", key, stamp, data.get(alias), [alias]))
        return units
    if name == "RateLimit":
        return [unit("rate_limit", {}, whole_stamp(query), data.get("rateLimit"), ["rateLimit"])]
    if name == "UserLookup":
        nodes = data.get("nodes") or []
        return [
            unit("user", {"id": node_id}, whole_stamp(query), node, ["nodes", position])
            for position, (node_id, node) in enumerate(zip(variables["ids"], nodes, strict=True))
        ]
    raise ValueError(f"no unit split for operation {name}")


def split_rest(url: str, body: Any, headers: dict[str, str], recorded_at: str) -> list[Unit]:
    """The unit of one REST response: a compare page or ``/rate_limit``."""
    if is_rest_rate_limit(url):
        kind, key, stamp = "rest_rate_limit", {}, REST_RATE_LIMIT_STAMP
    else:
        compare = compare_key(url)
        if compare is None:
            raise ValueError(f"no unit split for REST {url}")
        kind, key, stamp = "compare", compare, COMPARE_STAMP
    return [
        Unit(
            kind=kind,
            key=key,
            document_sha256=whole_stamp(stamp),
            recorded_at=recorded_at,
            headers=kept_headers(headers),
            data=body,
        )
    ]


def is_rest_rate_limit(url: str) -> bool:
    """Whether a REST request is ``GET /rate_limit`` with no parameters."""
    parts = urlsplit(url)
    return parts.path == "/rate_limit" and not parts.query


def compare_key(url: str) -> dict[str, Any] | None:
    """The compare unit key of a REST request URL, or None for another request.

    The request must carry exactly ``page`` and ``per_page``, the parameters ``COMPARE_STAMP``
    names; any other parameter could change GitHub's answer, so it finds no unit.
    """
    parts = urlsplit(url)
    match = _COMPARE_PATH.match(parts.path)
    if match is None:
        return None
    query = parse_qs(parts.query)
    if set(query) != {"page", "per_page"} or any(len(values) != 1 for values in query.values()):
        return None
    return {
        **match.groupdict(),
        "page": int(query["page"][0]),
        "per_page": int(query["per_page"][0]),
    }
