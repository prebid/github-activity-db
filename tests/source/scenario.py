"""A scenario model of GitHub for the sync engine's tests (redesign plan Step 3b-2a-2b-1).

The model holds each repository, pull request, and account as the JSON GitHub returns for it, with
every connection's nodes joined across pages. The replay asks it for each unit when a request
arrives (``Replay.provider``), so a response shows the model's state at that moment and carries the
test clock's time as its ``Date``. A test changes the state between requests through the methods
below; anything the model does not hold is answered as GitHub answers it, ``NOT_FOUND``.

The recordings check the model: they load into one model that re-renders each of them exactly
(``tests/source/test_scenario.py``). Rendered units carry the recordings' document stamps, so a
query document that changes is refused by the replay until the recordings are made again and the
model renders them.
"""

from __future__ import annotations

import base64
import binascii
import copy
import itertools
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

from github_activity_db.source import GitHubSource
from github_activity_db.source.documents import CONNECTIONS_BY_KEY
from github_activity_db.source.limits import INDEX_CLOSING_LINKS, PAGE_SIZE
from tests.source.conftest import FakeClock
from tests.source.recordings import Unit
from tests.source.replay import Replay, loaded_units

# The fields of an index entry; its closing links come from the same connection, at most
# INDEX_CLOSING_LINKS of them (documents.PULL_REQUEST_INDEX).
INDEX_FIELDS = (
    "number",
    "fullDatabaseId",
    "state",
    "isDraft",
    "authorAssociation",
    "createdAt",
    "updatedAt",
    "mergedAt",
    "closedAt",
)
# The kinds the model renders. Its stamp for each is the one the recordings of that kind carry.
RENDERED = ("resolve", "repository_labels", "index", "pull_request", "overflow", "user", "compare")
# The recordings new objects are copied from.
REPOSITORY_TEMPLATE = ("prebid", "go-gdpr")
PULL_REQUEST_TEMPLATE = ("prebid", "go-gdpr", 21)


class ScenarioError(Exception):
    """A test asked the model for something it cannot build."""


def iso(moment: datetime) -> str:
    """A time as GitHub writes it: UTC, whole seconds."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def not_found(message: str) -> list[dict[str, Any]]:
    return [{"message": message, "path": [], "type": "NOT_FOUND"}]


def missing_repository(key: dict[str, Any]) -> list[dict[str, Any]]:
    """GitHub's answer for a repository it cannot find; no recording has one, so the text follows
    GitHub's other NOT_FOUND messages."""
    return not_found(
        f"Could not resolve to a Repository with the name '{key['owner']}/{key['name']}'."
    )


@dataclass
class Cursors:
    """Where a connection's pages end: GitHub's cursors where recorded, else the offset form
    GitHub uses for labels, commits, and files among others (base64 of the position). The source
    never decodes a cursor."""

    recorded: dict[int, str] = field(default_factory=dict)

    def at(self, position: int) -> str:
        return self.recorded.get(position) or (
            base64.b64encode(str(position).encode()).decode().rstrip("=")
        )

    def position(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        for position, recorded in self.recorded.items():
            if recorded == cursor:
                return position
        try:
            return int(base64.b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        except (binascii.Error, UnicodeDecodeError, ValueError) as error:
            raise ScenarioError(f"cursor {cursor!r} names no page of this connection") from error


def page(connection: dict[str, Any], start: int, cursors: Cursors) -> dict[str, Any]:
    """The page of up to PAGE_SIZE nodes after ``start``, as GitHub renders it."""
    nodes = connection["nodes"][start : start + PAGE_SIZE]
    end = start + len(nodes)
    return {
        "totalCount": connection["totalCount"],
        "pageInfo": {
            "hasNextPage": end < connection["totalCount"],
            "endCursor": cursors.at(end) if end else None,
        },
        "nodes": copy.deepcopy(nodes),
    }


def joined(connection: dict[str, Any], cursors: Cursors) -> dict[str, Any]:
    """A recorded first page as the model keeps it: every node, no page info."""
    if connection["pageInfo"]["endCursor"] is not None:
        cursors.recorded[len(connection["nodes"])] = connection["pageInfo"]["endCursor"]
    return {"totalCount": connection["totalCount"], "nodes": connection["nodes"]}


def extend(
    connection: dict[str, Any], start: int, recorded: dict[str, Any], cursors: Cursors
) -> None:
    """Append a recorded continuation page at ``start``."""
    if start != len(connection["nodes"]):
        raise ScenarioError(f"a page at {start} continues {len(connection['nodes'])} nodes")
    connection["nodes"].extend(recorded["nodes"])
    connection["totalCount"] = recorded["totalCount"]
    if recorded["pageInfo"]["endCursor"] is not None:
        cursors.recorded[len(connection["nodes"])] = recorded["pageInfo"]["endCursor"]


def forget(value: Any, node_id: str) -> None:
    """Drop every reference to the deleted account ``node_id``: a field that named it is null,
    as GitHub returns a deleted account's references (D28), and a connection lists it no more."""
    if isinstance(value, list):
        for item in value:
            forget(item, node_id)
        return
    if not isinstance(value, dict):
        return
    for key, item in list(value.items()):
        if isinstance(item, dict) and item.get("id") == node_id:
            value[key] = None
        else:
            forget(item, node_id)
    nodes = value.get("nodes")
    if isinstance(nodes, list) and "totalCount" in value:
        kept = [
            node for node in nodes if not (isinstance(node, dict) and node.get("id") == node_id)
        ]
        value["totalCount"] -= len(nodes) - len(kept)
        value["nodes"] = kept


@dataclass
class PullRequest:
    """A pull request: its node with every connection's nodes, or only its index entry when no
    hydration was recorded for it (such a pull request cannot be hydrated)."""

    clock: FakeClock
    node: dict[str, Any]
    cursors: dict[str, Cursors] = field(default_factory=dict)
    # REST compare commits, for a pull request above 250 commits; only the fields the source reads.
    compare: list[dict[str, Any]] | None = None
    gone: bool = False

    @property
    def number(self) -> int:
        number: int = self.node["number"]
        return number

    @property
    def hydratable(self) -> bool:
        return "id" in self.node

    def set(self, **fields: Any) -> None:
        """Change fields as GitHub can without a new ``updatedAt`` (a draft toggle, an author
        association); ``touch`` moves ``updatedAt``."""
        self.node.update(fields)

    def touch(self, at: datetime | None = None) -> None:
        self.node["updatedAt"] = iso(at or self.clock.now())

    def merge(self, by: dict[str, Any], at: datetime | None = None) -> None:
        """Merged by the account ``by`` names (``Scenario.actor``)."""
        moment = iso(at or self.clock.now())
        oid = f"{int(self.node['fullDatabaseId']) + 2:040x}"
        self.node.update(
            state="MERGED",
            mergedAt=moment,
            closedAt=moment,
            mergedBy=by,
            mergeCommit={"oid": oid},
            updatedAt=moment,
        )

    def close(self, at: datetime | None = None) -> None:
        moment = iso(at or self.clock.now())
        self.node.update(state="CLOSED", closedAt=moment, updatedAt=moment)

    def reopen(self, at: datetime | None = None) -> None:
        moment = iso(at or self.clock.now())
        self.node.update(state="OPEN", closedAt=None, updatedAt=moment)

    def add(self, connection: str, *nodes: dict[str, Any]) -> None:
        self.node[connection]["nodes"].extend(nodes)
        self.node[connection]["totalCount"] += len(nodes)

    def remove(self, connection: str, matches: Callable[[dict[str, Any]], bool]) -> None:
        kept = [node for node in self.node[connection]["nodes"] if not matches(node)]
        self.node[connection]["totalCount"] -= len(self.node[connection]["nodes"]) - len(kept)
        self.node[connection]["nodes"] = kept

    def link(
        self, number: int, repository: dict[str, Any], *, user_linked: bool, github_id: int
    ) -> None:
        """Link the issue ``number`` of ``repository`` (its resolve data) as a closing issue;
        a user-linked one is also in ``userLinkedClosingIssues`` (D23)."""
        self.add(
            "closingIssuesReferences",
            {
                "fullDatabaseId": str(github_id),
                "number": number,
                "repository": {
                    "databaseId": repository["databaseId"],
                    "nameWithOwner": repository["nameWithOwner"],
                },
            },
        )
        if user_linked:
            self.add(
                "userLinkedClosingIssues",
                {"number": number, "repository": {"databaseId": repository["databaseId"]}},
            )

    def unlink(self, number: int) -> None:
        for connection in ("closingIssuesReferences", "userLinkedClosingIssues"):
            self.remove(connection, lambda node: node["number"] == number)

    def index_entry(self) -> dict[str, Any]:
        links = self.node["closingIssuesReferences"]
        return {
            **{name: self.node[name] for name in INDEX_FIELDS},
            "closingIssuesReferences": {
                "totalCount": links["totalCount"],
                "nodes": [
                    {"number": link["number"], "repository": copy.deepcopy(link["repository"])}
                    for link in links["nodes"][:INDEX_CLOSING_LINKS]
                ],
            },
        }

    def render(self) -> dict[str, Any]:
        rendered = {
            k: copy.deepcopy(v) for k, v in self.node.items() if k not in CONNECTIONS_BY_KEY
        }
        for key in CONNECTIONS_BY_KEY:
            rendered[key] = page(self.node[key], 0, self.cursors.setdefault(key, Cursors()))
        return rendered


@dataclass
class Repository:
    """A repository: its resolve data with every label, and its pull requests by number."""

    data: dict[str, Any]
    pulls: dict[int, PullRequest] = field(default_factory=dict)
    label_cursors: Cursors = field(default_factory=Cursors)
    index_cursors: Cursors = field(default_factory=Cursors)
    gone: bool = False

    @property
    def full_name(self) -> str:
        name: str = self.data["nameWithOwner"]
        return name

    def indexed(self) -> list[PullRequest]:
        """The pull requests GitHub lists, in the index's order (created, then id)."""
        listed = [pull for pull in self.pulls.values() if not pull.gone]
        return sorted(listed, key=lambda p: (p.node["createdAt"], int(p.node["fullDatabaseId"])))

    def render(self) -> dict[str, Any]:
        rendered = {k: copy.deepcopy(v) for k, v in self.data.items() if k != "labels"}
        rendered["labels"] = page(self.data["labels"], 0, self.label_cursors)
        return rendered


class Scenario:
    """Repositories, pull requests, and accounts, answering the replay at request time."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        # By lower-cased ``owner/name``; a rename keeps the old name (``followRenames``).
        self.repositories: dict[str, Repository] = {}
        # Lookup data by node id; ``None`` is an account GitHub answers NOT_FOUND for.
        self.accounts: dict[str, dict[str, Any] | None] = {}
        self._deleted: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self.replay = Replay(provider=self.unit)

    @asynccontextmanager
    async def source(self, deadline: datetime | None = None) -> AsyncIterator[GitHubSource]:
        """``async with scenario.source() as source``: the real source over this model."""
        source = GitHubSource(
            "test-token",
            http_transport=self.replay.transport(),
            clock=self.clock,
            deadline=deadline,
        )
        async with source:
            yield source

    # -- building ---------------------------------------------------------------------------
    def new_id(self) -> int:
        """A database id no recording uses."""
        return 9_000_000_000_000 + next(self._ids)

    def add_account(self, typename: str = "User", login: str | None = None, **fields: Any) -> str:
        """An account copied from a recorded one of ``typename``; returns its node id."""
        number = self.new_id()
        data = copy.deepcopy(_templates().accounts[typename])
        data.update(
            id=f"{typename}_scenario{number}",
            databaseId=number,
            login=login or f"account{number}",
            createdAt=iso(self.clock.now()),
            updatedAt=iso(self.clock.now()),
        )
        data.update(fields)
        self.accounts[data["id"]] = data
        node_id: str = data["id"]
        return node_id

    def actor(self, node_id: str) -> dict[str, Any]:
        """How a pull request names an account (the ``Actor`` fragment)."""
        data = self.accounts.get(node_id) or self._deleted[node_id]
        return {k: data[k] for k in ("__typename", "login", "id", "databaseId")}

    def add_repository(self, owner: str, name: str, **fields: Any) -> Repository:
        """A repository copied from a recorded one, with no labels and no pull requests."""
        number = self.new_id()
        data = copy.deepcopy(_templates().repository)
        data.update(
            id=f"R_scenario{number}",
            databaseId=number,
            name=name,
            owner={"login": owner},
            nameWithOwner=f"{owner}/{name}",
            labels={"totalCount": 0, "nodes": []},
        )
        data.update(fields)
        repository = Repository(data)
        self.repositories[repository.full_name.lower()] = repository
        return repository

    def add_label(self, repository: Repository, name: str) -> dict[str, Any]:
        """A repository label; a pull request carries it as ``{id, name}``."""
        moment = iso(self.clock.now())
        label = {
            "id": f"LA_scenario{self.new_id()}",
            "name": name,
            "color": "ededed",
            "description": None,
            "isDefault": False,
            "createdAt": moment,
            "updatedAt": moment,
        }
        repository.data["labels"]["nodes"].append(label)
        repository.data["labels"]["totalCount"] += 1
        return label

    def open_pull_request(
        self, repository: Repository, author: str, number: int | None = None, **fields: Any
    ) -> PullRequest:
        """An open pull request by ``author`` (a node id), copied from a recorded one, with every
        connection empty; ``fields`` replace any of its fields."""
        moment = iso(self.clock.now())
        github_id = self.new_id()
        actor = self.actor(author)
        node = copy.deepcopy(_templates().pull_request)
        node.update(
            id=f"PR_scenario{github_id}",
            fullDatabaseId=str(github_id),
            number=number or max([*repository.pulls, 0]) + 1,
            state="OPEN",
            isDraft=False,
            title=f"Change {github_id}",
            body="",
            createdAt=moment,
            updatedAt=moment,
            publishedAt=moment,
            mergedAt=None,
            closedAt=None,
            lastEditedAt=None,
            editor=None,
            mergedBy=None,
            mergeCommit=None,
            author=actor,
            authorAssociation="CONTRIBUTOR",
            baseRefName=repository.data["defaultBranchRef"]["name"],
            baseRefOid=f"{github_id:040x}",
            headRefOid=f"{github_id + 1:040x}",
            headRefName=f"change-{github_id}",
            isCrossRepository=True,
            headRepository={
                "databaseId": self.new_id(),
                "nameWithOwner": f"{actor['login']}/{repository.data['name']}",
            },
            headRepositoryOwner={"id": actor["id"], "login": actor["login"]},
            reviewDecision=None,
            totalCommentsCount=0,
            additions=0,
            deletions=0,
            changedFiles=0,
            milestone=None,
            stack=None,
            stackEntry=None,
            potentialMergeCommit=None,
        )
        for key in CONNECTIONS_BY_KEY:
            node[key] = {"totalCount": 0, "nodes": []}
        node.update(fields)
        pull = PullRequest(self.clock, node)
        repository.pulls[pull.number] = pull
        return pull

    def node(self, connection: str, **fields: Any) -> dict[str, Any]:
        """A new node of ``connection`` copied from a recorded one, with fresh ids."""
        node = copy.deepcopy(_templates().nodes[connection])
        number = self.new_id()
        if "id" in node:
            node["id"] = f"N_scenario{number}"
        if "fullDatabaseId" in node:
            node["fullDatabaseId"] = str(number)
        if "databaseId" in node:
            node["databaseId"] = number
        node.update(fields)
        return node

    # -- changes ----------------------------------------------------------------------------
    def rename_repository(self, repository: Repository, owner: str, name: str) -> None:
        """Rename; the old name still resolves to it, and links to its issues show the new name."""
        repository.data.update(name=name, owner={"login": owner}, nameWithOwner=f"{owner}/{name}")
        self.repositories[repository.full_name.lower()] = repository
        for other in self.repositories.values():
            for pull in other.pulls.values():
                for link in pull.node.get("closingIssuesReferences", {}).get("nodes", []):
                    if link["repository"]["databaseId"] == repository.data["databaseId"]:
                        link["repository"]["nameWithOwner"] = repository.full_name

    def rename_label(self, repository: Repository, label_id: str, name: str) -> None:
        """A label keeps its id when renamed, on the repository and on every pull request."""
        for label in repository.data["labels"]["nodes"]:
            if label["id"] == label_id:
                label.update(name=name, updatedAt=iso(self.clock.now()))
        for pull in repository.pulls.values():
            for label in pull.node.get("labels", {}).get("nodes", []):
                if label["id"] == label_id:
                    label["name"] = name

    def delete_account(self, node_id: str) -> None:
        """Lookups answer NOT_FOUND, and nothing GitHub renders names the account any more."""
        data = self.accounts.get(node_id)
        if data is not None:
            self._deleted[node_id] = data
        self.accounts[node_id] = None
        for repository in self.repositories.values():
            for pull in repository.pulls.values():
                forget(pull.node, node_id)

    def restore_account(self, node_id: str) -> None:
        """Lookups find the account again; references it lost stay lost."""
        self.accounts[node_id] = self._deleted.pop(node_id)

    # -- answering the replay ---------------------------------------------------------------
    def repository(self, owner: str, name: str) -> Repository | None:
        repository = self.repositories.get(f"{owner}/{name}".lower())
        return None if repository is None or repository.gone else repository

    def pull_request(self, node_id: str) -> PullRequest | None:
        for repository in self.repositories.values():
            for pull in repository.pulls.values():
                if pull.node.get("id") == node_id and not pull.gone and not repository.gone:
                    return pull
        return None

    def unit(self, kind: str, key: dict[str, Any]) -> Unit | None:
        """The unit the replay serves for ``kind`` and ``key``; ``None`` leaves the request to
        the recorded units (the rate-limit readings)."""
        data: Any = None
        errors: list[dict[str, Any]] = []
        match kind:
            case "repository":
                # Asked before an index or batch: a unit only for a repository GitHub cannot find.
                if self.repository(key["owner"], key["name"]) is not None:
                    return None
                errors = missing_repository(key)
            case "resolve":
                repository = self.repository(key["owner"], key["name"])
                if repository is not None:
                    data = repository.render()
                else:
                    errors = missing_repository(key)
            case "repository_labels":
                found = [
                    r
                    for r in self.repositories.values()
                    if r.data.get("id") == key["id"] and not r.gone
                ]
                if not found:
                    errors = not_found(
                        f"Could not resolve to a node with the global id of '{key['id']}'."
                    )
                else:
                    owner = found[0]
                    start = owner.label_cursors.position(key["after"])
                    data = {
                        "id": key["id"],
                        "labels": page(owner.data["labels"], start, owner.label_cursors),
                    }
            case "index":
                repository = self.repository(key["owner"], key["name"])
                if repository is None:
                    return None
                listed = repository.indexed()
                entries = {
                    "totalCount": len(listed),
                    "nodes": [pull.index_entry() for pull in listed],
                }
                data = page(
                    entries,
                    repository.index_cursors.position(key["after"]),
                    repository.index_cursors,
                )
            case "pull_request":
                repository = self.repository(key["owner"], key["name"])
                pull = repository.pulls.get(key["number"]) if repository is not None else None
                if pull is not None and not pull.gone and not pull.hydratable:
                    return None
                if pull is None or pull.gone:
                    errors = not_found(
                        f"Could not resolve to a PullRequest with the number of {key['number']}."
                    )
                else:
                    data = pull.render()
            case "overflow":
                pull = self.pull_request(key["id"])
                if pull is None:
                    errors = not_found(
                        f"Could not resolve to a node with the global id of '{key['id']}'."
                    )
                else:
                    connection = key["connection"]
                    cursors = pull.cursors.setdefault(connection, Cursors())
                    data = {
                        "headRefOid": pull.node["headRefOid"],
                        "updatedAt": pull.node["updatedAt"],
                        connection: page(
                            pull.node[connection], cursors.position(key["after"]), cursors
                        ),
                    }
            case "user":
                account = self.accounts.get(key["id"])
                if account is None:
                    errors = not_found(
                        f"Could not resolve to a node with the global id of '{key['id']}'."
                    )
                else:
                    data = copy.deepcopy(account)
            case "compare":
                data = self._compare(key)
                if data is None:
                    return None
            case _:
                return None
        return self._unit(kind, key, data, errors)

    def _compare(self, key: dict[str, Any]) -> dict[str, Any] | None:
        repository = self.repository(key["owner"], key["name"])
        if repository is None:
            return None
        for pull in repository.pulls.values():
            node = pull.node
            if pull.compare is not None and (node["baseRefOid"], node["headRefOid"]) == (
                key["base"],
                key["head"],
            ):
                start = (key["page"] - 1) * key["per_page"]
                return {
                    "total_commits": len(pull.compare),
                    "commits": copy.deepcopy(pull.compare[start : start + key["per_page"]]),
                }
        return None

    def _unit(
        self, kind: str, key: dict[str, Any], data: Any, errors: list[dict[str, Any]]
    ) -> Unit | None:
        """A unit stamped as the recordings of its kind are; none when no recording checks the
        model's rendering of that kind (the label and closing-issue continuations), so the
        replay reports the request as one to record."""
        stamp = _templates().stamps.get(
            (kind, key.get("connection") if kind == "overflow" else None)
        )
        if stamp is None and kind != "repository":
            return None
        now = self.clock.now()
        return Unit(
            kind=kind,
            key=dict(key),
            document_sha256=stamp or "",
            recorded_at=iso(now),
            headers={"date": format_datetime(now, usegmt=True)},
            data=data,
            errors=errors,
            source="scenario",
        )

    # -- the recordings ---------------------------------------------------------------------
    @classmethod
    def from_recordings(cls, clock: FakeClock) -> Scenario:
        """Every recorded unit in one model, which re-renders each of them."""
        world = cls(clock)
        by_kind: dict[str, list[Unit]] = {}
        for unit in loaded_units():
            by_kind.setdefault(unit.kind, []).append(copy.deepcopy(unit))
        for unit in by_kind.get("resolve", []):
            labels = Cursors()
            data = {**unit.data, "labels": joined(unit.data["labels"], labels)}
            repository = Repository(data, label_cursors=labels)
            world.repositories[repository.full_name.lower()] = repository
            world.repositories[f"{unit.key['owner']}/{unit.key['name']}".lower()] = repository
        for unit in by_kind.get("repository_labels", []):
            labels_of = next(
                r for r in world.repositories.values() if r.data.get("id") == unit.key["id"]
            )
            start = labels_of.label_cursors.position(unit.key["after"])
            extend(labels_of.data["labels"], start, unit.data["labels"], labels_of.label_cursors)
        for unit in sorted(by_kind.get("index", []), key=lambda u: u.key["after"] is not None):
            indexed = world._recorded_repository(unit.key["owner"], unit.key["name"])
            start = indexed.index_cursors.position(unit.key["after"])
            if start != len(indexed.pulls):
                raise ScenarioError(f"index page at {start} after {len(indexed.pulls)} entries")
            for entry in unit.data["nodes"]:
                indexed.pulls[entry["number"]] = PullRequest(clock, entry)
            if unit.data["pageInfo"]["endCursor"] is not None:
                indexed.index_cursors.recorded[len(indexed.pulls)] = unit.data["pageInfo"][
                    "endCursor"
                ]
        for unit in by_kind.get("pull_request", []):
            hydrated = world._recorded_repository(unit.key["owner"], unit.key["name"])
            if unit.data is None:
                hydrated.pulls[unit.key["number"]] = PullRequest(
                    clock, {"number": unit.key["number"]}, gone=True
                )
                continue
            pull = PullRequest(clock, unit.data)
            for key in CONNECTIONS_BY_KEY:
                pull.cursors[key] = Cursors()
                pull.node[key] = joined(unit.data[key], pull.cursors[key])
            hydrated.pulls[pull.number] = pull
        for unit in by_kind.get("overflow", []):
            continued = world.pull_request(unit.key["id"])
            if continued is None:
                raise ScenarioError(f"overflow {unit.key} continues no recorded pull request")
            connection = unit.key["connection"]
            cursors = continued.cursors[connection]
            extend(
                continued.node[connection],
                cursors.position(unit.key["after"]),
                unit.data[connection],
                cursors,
            )
        for unit in sorted(by_kind.get("compare", []), key=lambda u: u.key["page"]):
            compared = world._recorded_repository(unit.key["owner"], unit.key["name"])
            for pull in compared.pulls.values():
                if (pull.node.get("baseRefOid"), pull.node.get("headRefOid")) == (
                    unit.key["base"],
                    unit.key["head"],
                ):
                    pull.compare = (pull.compare or []) + [
                        read_commit(c) for c in unit.data["commits"]
                    ]
        for unit in by_kind.get("user", []):
            world.accounts[unit.key["id"]] = unit.data
        return world

    def _recorded_repository(self, owner: str, name: str) -> Repository:
        """The repository of a recorded unit, as a bare placeholder when no resolve was recorded."""
        key = f"{owner}/{name}".lower()
        if key not in self.repositories:
            self.repositories[key] = Repository({"nameWithOwner": f"{owner}/{name}"})
        return self.repositories[key]


def read_commit(commit: dict[str, Any]) -> dict[str, Any]:
    """A REST compare commit, reduced to the fields the source reads (``models.CompareCommit``)."""
    author = commit.get("author")
    return {
        "sha": commit["sha"],
        "commit": {"author": {k: commit["commit"]["author"][k] for k in ("name", "email", "date")}},
        "author": author if not author else {k: author[k] for k in ("id", "node_id", "login")},
    }


@dataclass(frozen=True)
class _Templates:
    stamps: dict[tuple[str, str | None], str]
    repository: dict[str, Any]
    pull_request: dict[str, Any]
    nodes: dict[str, dict[str, Any]]
    accounts: dict[str, dict[str, Any]]


_TEMPLATES: list[_Templates] = []


def _templates() -> _Templates:
    """What the model copies from the recordings, read once: each kind's stamp, the objects new
    ones are copied from, and the first recorded node of each connection."""
    if not _TEMPLATES:
        stamps: dict[tuple[str, str | None], set[str]] = {}
        repository: dict[str, Any] = {}
        pull_request: dict[str, Any] = {}
        nodes: dict[str, dict[str, Any]] = {}
        accounts: dict[str, dict[str, Any]] = {}
        for unit in loaded_units():
            if unit.kind not in RENDERED:
                continue
            slot = (unit.kind, unit.key["connection"] if unit.kind == "overflow" else None)
            stamps.setdefault(slot, set()).add(unit.document_sha256)
            if (
                unit.kind == "resolve"
                and (unit.key["owner"], unit.key["name"]) == REPOSITORY_TEMPLATE
            ):
                repository = unit.data
            if unit.kind == "pull_request" and unit.data is not None:
                if (
                    unit.key["owner"],
                    unit.key["name"],
                    unit.key["number"],
                ) == PULL_REQUEST_TEMPLATE:
                    pull_request = unit.data
                for key in CONNECTIONS_BY_KEY:
                    if unit.data[key]["nodes"] and key not in nodes:
                        nodes[key] = unit.data[key]["nodes"][0]
            if unit.kind == "user" and unit.data is not None:
                accounts.setdefault(unit.data["__typename"], unit.data)
        mixed = {slot: found for slot, found in stamps.items() if len(found) != 1}
        if mixed:
            raise ScenarioError(f"recordings of one kind carry different document stamps: {mixed}")
        _TEMPLATES.append(
            _Templates(
                stamps={slot: found.pop() for slot, found in stamps.items()},
                repository=repository,
                pull_request=pull_request,
                nodes=nodes,
                accounts=accounts,
            )
        )
    return _TEMPLATES[0]


def recorded_units() -> Iterator[Unit]:
    """The recordings the model renders: every unit but the rate-limit readings."""
    return (unit for unit in loaded_units() if unit.kind in RENDERED)
