"""A scenario model of GitHub for the sync engine's tests (redesign plan Step 3b-2a-2b-1).

The model holds each repository, pull request, and account as the JSON GitHub returns for it, with
every connection's nodes joined across pages. The replay asks it for each unit when a request
arrives (``Replay.provider``), so a response shows the model's state at that moment and carries the
test clock's time as its ``Date``. A test changes the state between requests, or during a run with
``Scenario.after``, through the methods below.

Pages follow GitHub's cursors: the index's, comments', reviews', and review requests' name the
page's last node, so the next page starts after it even when nodes before it come or go; the other
connections' name a position.

The recordings check the model: they load into one model that re-renders each of them exactly
(``tests/source/test_scenario.py``). Rendered units carry the recordings' document stamps, so a
query document that changes is refused by the replay until the recordings are made again and the
model renders them.
"""

from __future__ import annotations

import base64
import copy
import functools
import itertools
import json
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

Node = dict[str, Any]

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


def _index_order(node: Node) -> tuple[Any, ...]:
    return (node["createdAt"], int(node["fullDatabaseId"]))


# The connections whose cursors name a node: the order every recorded list of them is in, and the
# key a cursor names the page's last node by. GitHub's recorded cursors name it by a key too
# (``cursor:v2:`` and the key; a review's holds two times, equal in every recording).
ORDERS: dict[str, Callable[[Node], tuple[Any, ...]]] = {
    "comments": lambda node: (int(node["fullDatabaseId"]),),
    "reviews": lambda node: (node["createdAt"], int(node["fullDatabaseId"])),
    "reviewRequests": lambda node: (node["databaseId"],),
}
# The connections a push changes: changing them moves ``headRefOid``.
PUSHED = ("commits", "files")
# The kinds the model renders; its stamp for each is the one the recordings of that kind carry.
RENDERED = ("resolve", "repository_labels", "index", "pull_request", "overflow", "user", "compare")
# The recordings new objects are copied from.
REPOSITORY_TEMPLATE = ("prebid", "go-gdpr")
PULL_REQUEST_TEMPLATE = ("prebid", "go-gdpr", 21)


class ScenarioError(Exception):
    """A test asked the model for something GitHub would not give it."""


def iso(moment: datetime) -> str:
    """A time as GitHub writes it: UTC, whole seconds."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def not_found(message: str) -> list[dict[str, Any]]:
    return [{"message": message, "path": [], "type": "NOT_FOUND"}]


def missing_node(node_id: str) -> list[dict[str, Any]]:
    return not_found(f"Could not resolve to a node with the global id of '{node_id}'.")


def missing_repository(owner: str, name: str) -> list[dict[str, Any]]:
    """No recording holds GitHub's answer for a missing repository; the text follows its others."""
    return not_found(f"Could not resolve to a Repository with the name '{owner}/{name}'.")


def dicts(value: Any) -> Iterator[Node]:
    """Every object inside a JSON value."""
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from dicts(item)


def names(value: Any, node_id: str) -> bool:
    """Whether a JSON object is a reference to the account ``node_id`` (GraphQL or REST)."""
    return isinstance(value, dict) and node_id in (value.get("id"), value.get("node_id"))


def forget(value: Any, node_id: str) -> None:
    """Drop every reference to the account ``node_id``: a field that named it is null, as GitHub
    returns a deleted account's references (D28), and a connection lists it no more."""
    for obj in list(dicts(value)):
        for key, item in list(obj.items()):
            if names(item, node_id):
                obj[key] = None
        nodes = obj.get("nodes")
        if isinstance(nodes, list) and "totalCount" in obj:
            kept = [node for node in nodes if not names(node, node_id)]
            obj["totalCount"] -= len(nodes) - len(kept)
            obj["nodes"] = kept


def read_commit(commit: Node) -> Node:
    """A REST compare commit, reduced to the fields the source reads (``models.CompareCommit``)."""
    author = commit.get("author")
    return {
        "sha": commit["sha"],
        "commit": {"author": {k: commit["commit"]["author"][k] for k in ("name", "email", "date")}},
        "author": author if not author else {k: author[k] for k in ("id", "node_id", "login")},
    }


def rest_commit(node: Node) -> Node:
    """A commit node of the pull request as REST compare lists it."""
    commit = node["commit"]
    user = commit["author"]["user"]
    return {
        "sha": commit["oid"],
        "commit": {
            "author": {
                "name": commit["author"]["name"],
                "email": commit["author"]["email"],
                "date": commit["authoredDate"],
            }
        },
        "author": user
        and {"id": user["databaseId"], "node_id": user["id"], "login": user["login"]},
    }


@dataclass
class Cursors:
    """A connection's page cursors: each names a node (by ``order``) or a position. A cursor is
    one GitHub recorded or one the model served; any other names no page. The model's own are
    ``scenario:`` and the node's key, or a position in GitHub's offset form (base64 of the
    number); the source never decodes a cursor."""

    order: Callable[[Node], tuple[Any, ...]] | None = None
    known: dict[str, Any] = field(default_factory=dict)  # cursor -> the key or position it names

    def mark(self, nodes: list[Node], end: int) -> Any:
        return self.order(nodes[end - 1]) if self.order is not None else end

    def record(self, nodes: list[Node], cursor: str | None) -> None:
        """Keep a recorded page's ``endCursor``: the page ended at the last of ``nodes``."""
        if cursor is not None:
            self.known[cursor] = self.mark(nodes, len(nodes))

    def end(self, nodes: list[Node], end: int) -> str | None:
        if end == 0:
            return None
        mark = self.mark(nodes, end)
        for cursor, known in self.known.items():
            if known == mark:
                return cursor
        if self.order is not None:
            cursor = "scenario:" + json.dumps(list(mark))
        else:
            cursor = base64.b64encode(str(mark).encode()).decode().rstrip("=")
        self.known[cursor] = mark
        return cursor

    def start(self, nodes: list[Node], cursor: str | None) -> int:
        """Where the page after ``cursor`` starts in ``nodes`` as they are now."""
        if cursor is None:
            return 0
        if cursor not in self.known:
            raise ScenarioError(f"cursor {cursor!r} names no page the model served")
        mark = self.known[cursor]
        if self.order is None:
            position: int = mark
            return position
        order = self.order
        return next((i for i, node in enumerate(nodes) if order(node) > mark), len(nodes))


def page(connection: Node, cursors: Cursors, after: str | None = None) -> Node:
    """The page of up to PAGE_SIZE nodes after ``after``, as GitHub renders it."""
    nodes = connection["nodes"]
    start = cursors.start(nodes, after)
    end = start + len(nodes[start : start + PAGE_SIZE])
    return {
        "totalCount": connection["totalCount"],
        "pageInfo": {
            "hasNextPage": end < connection["totalCount"],
            "endCursor": cursors.end(nodes, end),
        },
        "nodes": copy.deepcopy(nodes[start:end]),
    }


def joined(recorded: Node, cursors: Cursors, connection: Node | None = None) -> Node:
    """A recorded page added to ``connection`` (a new one for a first page): its nodes, its
    total, and its ``endCursor``."""
    connection = connection if connection is not None else {"totalCount": 0, "nodes": []}
    connection["nodes"].extend(recorded["nodes"])
    connection["totalCount"] = recorded["totalCount"]
    cursors.record(connection["nodes"], recorded["pageInfo"]["endCursor"])
    return connection


@dataclass
class PullRequest:
    """A pull request: its node with every connection's nodes, or only its index entry when no
    hydration was recorded for it (such a pull request cannot be hydrated)."""

    world: Scenario
    node: Node
    cursors: dict[str, Cursors] = field(default_factory=dict)
    # REST compare's commits as recorded, for a pull request above 250 commits; without a
    # recording they are made from the commit nodes.
    compare: list[Node] | None = None
    # The compare each earlier head still answers, by (base, head): commits never change.
    heads: dict[tuple[str, str], list[Node]] = field(default_factory=dict)
    gone: bool = False

    def __post_init__(self) -> None:
        for key in CONNECTIONS_BY_KEY:
            self.cursors.setdefault(key, Cursors(ORDERS.get(key)))

    @property
    def number(self) -> int:
        number: int = self.node["number"]
        return number

    @property
    def hydratable(self) -> bool:
        return "id" in self.node

    def now(self, at: datetime | None) -> str:
        return iso(at or self.world.clock.now())

    def set(self, **fields: Any) -> None:
        """Change fields without a new ``updatedAt``, as marking a draft ready for review (0 of
        151, plan 5.3 step 3) and an author-association change do (plan section 7, "Change
        signal"). Converting to a draft moves it, so follow that with ``touch``."""
        self.node.update(fields)

    def touch(self, at: datetime | None = None) -> None:
        self.node["updatedAt"] = self.now(at)

    def merge(self, by: str | None, at: datetime | None = None) -> None:
        """Merged by the account ``by`` (a node id), or by none GitHub still names (D28)."""
        moment = self.now(at)
        self.node.update(
            state="MERGED",
            mergedAt=moment,
            closedAt=moment,
            mergedBy=self.world.actor(by) if by is not None else None,
            mergeCommit={"oid": self.world.new_oid()},
            updatedAt=moment,
        )

    def close(self, at: datetime | None = None) -> None:
        moment = self.now(at)
        self.node.update(state="CLOSED", closedAt=moment, updatedAt=moment)

    def reopen(self, at: datetime | None = None) -> None:
        moment = self.now(at)
        self.node.update(state="OPEN", closedAt=None, updatedAt=moment)

    def add(self, connection: str, *nodes: Node, at: datetime | None = None) -> None:
        """Add nodes, which moves ``updatedAt`` as a comment, a label, or a review request does;
        commits and files come with a push (``_push``)."""

        def added() -> None:
            self._add(connection, nodes)
            if connection == "commits" and self.compare is not None:
                self.compare.extend(rest_commit(node) for node in nodes)

        self._change(connection, added, at)

    def remove(
        self, connection: str, matches: Callable[[Node], bool], at: datetime | None = None
    ) -> None:
        """Remove the nodes ``matches`` selects, which moves ``updatedAt``; commits and files
        go with a push (``_push``)."""

        def removed() -> None:
            if connection == "commits" and self.compare is not None:
                oids = {n["commit"]["oid"] for n in self.node["commits"]["nodes"] if matches(n)}
                self.compare = [c for c in self.compare if c["sha"] not in oids]
            self._remove(connection, matches)

        self._change(connection, removed, at)

    def link(self, number: int, repository: Repository, *, user_linked: bool = False) -> None:
        """Link issue ``number`` of ``repository`` as a closing issue, without a new
        ``updatedAt`` (plan section 7, "Change signal"); a user-linked one is also in
        ``userLinkedClosingIssues`` (D23)."""
        target = {
            "databaseId": repository.data["databaseId"],
            "nameWithOwner": repository.full_name,
        }
        github_id = self.world.issue_id(target["databaseId"], number)
        self._add(
            "closingIssuesReferences",
            [{"fullDatabaseId": str(github_id), "number": number, "repository": target}],
        )
        if user_linked:
            self._add(
                "userLinkedClosingIssues",
                [{"number": number, "repository": {"databaseId": target["databaseId"]}}],
            )

    def unlink(self, number: int, repository: Repository) -> None:
        def matches(node: Node) -> bool:
            target = node["repository"]["databaseId"]
            return (node["number"], target) == (number, repository.data["databaseId"])

        self._remove("closingIssuesReferences", matches)
        self._remove("userLinkedClosingIssues", matches)

    def compare_commits(self) -> list[Node]:
        """What REST compare lists for this pull request's head."""
        if self.compare is not None:
            return self.compare
        return [rest_commit(node) for node in self.node["commits"]["nodes"]]

    def _change(self, connection: str, apply: Callable[[], None], at: datetime | None) -> None:
        """Apply a change and move ``updatedAt``. A change to commits or files is a push: the
        head moves to the last commit (to a new oid when only files changed), and the old head's
        compare still answers, since commits never change."""
        pushed = connection in PUSHED
        if pushed:
            old = (self.node["baseRefOid"], self.node["headRefOid"])
            self.heads[old] = copy.deepcopy(self.compare_commits())
        apply()
        self.touch(at)
        if pushed:
            commits = self.compare_commits()
            last = commits[-1]["sha"] if connection == "commits" and commits else None
            self.node["headRefOid"] = last or self.world.new_oid()

    def _add(self, connection: str, nodes: Any) -> None:
        listed = self.node[connection]
        listed["nodes"].extend(nodes)
        listed["totalCount"] += len(nodes)
        order = ORDERS.get(connection)
        if order is not None:
            listed["nodes"].sort(key=order)

    def _remove(self, connection: str, matches: Callable[[Node], bool]) -> None:
        listed = self.node[connection]
        kept = [node for node in listed["nodes"] if not matches(node)]
        listed["totalCount"] -= len(listed["nodes"]) - len(kept)
        listed["nodes"] = kept

    def index_entry(self) -> Node:
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

    def render(self) -> Node:
        rendered = {
            k: copy.deepcopy(v) for k, v in self.node.items() if k not in CONNECTIONS_BY_KEY
        }
        for key in CONNECTIONS_BY_KEY:
            rendered[key] = page(self.node[key], self.cursors[key])
        return rendered

    def continuation(self, connection: str, after: str) -> Node:
        return {
            "headRefOid": self.node["headRefOid"],
            "updatedAt": self.node["updatedAt"],
            connection: page(self.node[connection], self.cursors[connection], after),
        }


@dataclass
class Repository:
    """A repository: its resolve data with every label, and its pull requests by number."""

    data: Node
    pulls: dict[int, PullRequest] = field(default_factory=dict)
    label_cursors: Cursors = field(default_factory=Cursors)
    index_cursors: Cursors = field(default_factory=lambda: Cursors(_index_order))
    gone: bool = False

    @property
    def full_name(self) -> str:
        name: str = self.data["nameWithOwner"]
        return name

    def index(self) -> Node:
        """The index connection: the pull requests GitHub lists, in its order."""
        listed = [pull for pull in self.pulls.values() if not pull.gone]
        listed.sort(key=lambda pull: _index_order(pull.node))
        return {"totalCount": len(listed), "nodes": [pull.index_entry() for pull in listed]}

    def render(self) -> Node:
        rendered = {k: copy.deepcopy(v) for k, v in self.data.items() if k != "labels"}
        rendered["labels"] = page(self.data["labels"], self.label_cursors)
        return rendered


class Scenario:
    """Repositories, pull requests, and accounts, answering the replay at request time.

    Any repository, pull request, node, or account the model does not hold is answered
    ``NOT_FOUND``. It serves no rate-limit reading: every GraphQL response carries the replay's
    fixed ``rateLimit`` reading (``replay.RATE_LIMIT``), so budget waits come only from faults.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        # By lower-cased ``owner/name``; a rename keeps the old name (``followRenames``).
        self.repositories: dict[str, Repository] = {}
        # Lookup data by node id; ``None`` is an account GitHub answers NOT_FOUND for.
        self.accounts: dict[str, Node | None] = {}
        self._deleted: dict[str, Node] = {}
        # One id per issue, by its repository's databaseId and its number.
        self._issues: dict[tuple[int, int], int] = {}
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

    def after(self, operation: str, change: Callable[[], None], times: int = 1) -> None:
        """Apply ``change`` right after the replay answers the next ``times`` requests of
        ``operation``: a one-shot fault (``Replay.modify``), so a change that never runs fails the
        test like an unused fault, and one that raises is the replay's problem. The first
        matching fault serves a request, so a change meant to follow a faulted response belongs
        in that fault's ``respond``."""

        def changed(body: Node) -> None:
            try:
                change()
            except Exception as error:
                self.replay.problems.append(f"the change after {operation} failed: {error!r}")
                raise

        self.replay.modify(changed, when=lambda seen: seen.operation == operation, times=times)

    # -- building ---------------------------------------------------------------------------
    def new_id(self) -> int:
        """A database id no recording uses."""
        return 9_000_000_000_000 + next(self._ids)

    def new_oid(self) -> str:
        """A git object id no recording uses."""
        return f"5ce4a1{next(self._ids):034x}"

    def issue_id(self, repository_id: int, number: int) -> int:
        """The id of issue ``number`` of the repository ``repository_id``, the same each time."""
        return self._issues.setdefault((repository_id, number), self.new_id())

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

    def account(self, node_id: str, *, deleted: bool = False) -> Node:
        """An account's lookup data; a deleted one's only when ``deleted`` (a commit link)."""
        data = self.accounts.get(node_id)
        if data is not None:
            return data
        if node_id in self._deleted:
            if deleted:
                return self._deleted[node_id]
            raise ScenarioError(f"{node_id} is deleted: only a commit links it (gap 90)")
        if node_id in self.accounts:
            raise ScenarioError(f"lookups answer NOT_FOUND for {node_id}: no account to name")
        raise ScenarioError(f"no account {node_id}: add it with add_account")

    def actor(self, node_id: str) -> Node:
        """How a pull request names an account (the ``Actor`` fragment)."""
        data = self.account(node_id)
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

    def add_label(self, repository: Repository, name: str) -> Node:
        """A repository label; returns it as a pull request carries it, ``{id, name}``."""
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
        return {"id": label["id"], "name": name}

    def open_pull_request(
        self, repository: Repository, author: str, number: int | None = None, **fields: Any
    ) -> PullRequest:
        """An open pull request by ``author`` (a node id) from a fork, copied from a recorded one,
        with every connection empty; ``fields`` replace any of its fields."""
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
            baseRefOid=self.new_oid(),
            headRefOid=self.new_oid(),
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
        pull = PullRequest(self, node)
        repository.pulls[pull.number] = pull
        return pull

    def node(self, connection: str, by: str | None = None, **fields: Any) -> Node:
        """A new node of ``connection``, made now and copied from a recorded one with its own
        identity: fresh ids, a commit's own oid and git author, a file's own path, and the
        account ``by`` (a node id) wherever the node names one, or none. An assignee is the
        account ``by`` itself. A label comes from ``add_label`` and a closing link from
        ``PullRequest.link``."""
        if connection in ("labels", "closingIssuesReferences", "userLinkedClosingIssues"):
            raise ScenarioError(f"{connection} come from add_label and PullRequest.link")
        if connection == "assignedActors":
            if by is None:
                raise ScenarioError("an assignee is an account: pass by")
            return {**self.actor(by), **fields}
        node = copy.deepcopy(_templates().nodes[connection])
        number = self.new_id()
        moment = iso(self.clock.now())
        fresh = {"id": f"N_scenario{number}", "fullDatabaseId": str(number), "databaseId": number}
        node.update({key: value for key, value in fresh.items() if key in node})
        match connection:
            case "comments" | "reviews":
                actor = self.actor(by) if by is not None else None
                node.update(author=actor, editor=None, lastEditedAt=None)
                times = ("createdAt", "updatedAt", "publishedAt", "submittedAt")
                node.update({key: moment for key in times if key in node})
            case "commits":
                node["commit"].update(oid=self.new_oid(), authoredDate=moment)
                node["commit"]["author"] = self._git_author(by, number)
            case "files":
                node["path"] = f"file-{number}"
            case "reviewRequests":
                node["requestedReviewer"] = self.actor(by) if by is not None else None
        node.update(fields)
        return node

    def _git_author(self, by: str | None, number: int) -> Node:
        """A commit's git author and the account GitHub links it to. A commit can link an
        account a lookup no longer finds (gap 90), and links a bot as ``<login>[bot]``, as the
        recordings show (plan 5.2)."""
        if by is None:
            return {
                "name": f"author {number}",
                "email": f"author-{number}@example.com",
                "user": None,
            }
        data = self.account(by, deleted=True)
        login = data["login"]
        email = f"{login}@example.com"
        if data["__typename"] == "Bot":
            login = f"{login}[bot]"
            email = f"{data['databaseId']}+{login}@users.noreply.github.com"
        user = {"id": data["id"], "databaseId": data["databaseId"], "login": login}
        return {"name": login, "email": email, "user": user}

    # -- changes ----------------------------------------------------------------------------
    def rename_repository(self, repository: Repository, name: str) -> None:
        """Rename within its owner; the old name still resolves to it, and every link to it and
        every pull request from it show the new name."""
        owner = repository.data["owner"]["login"]
        repository.data.update(name=name, nameWithOwner=f"{owner}/{name}")
        self.repositories[repository.full_name.lower()] = repository
        for pull in self.pulls():
            for obj in dicts(pull.node):
                if (
                    obj.get("databaseId") == repository.data["databaseId"]
                    and "nameWithOwner" in obj
                ):
                    obj["nameWithOwner"] = repository.full_name

    def rename_label(self, repository: Repository, label_id: str, name: str) -> None:
        """A label keeps its id when renamed, on the repository and on every pull request,
        without a new ``updatedAt`` for them (plan section 7, "Change signal")."""
        for label in repository.data["labels"]["nodes"]:
            if label["id"] == label_id:
                label.update(name=name, updatedAt=iso(self.clock.now()))
        for pull in repository.pulls.values():
            for label in pull.node.get("labels", {}).get("nodes", []):
                if label["id"] == label_id:
                    label["name"] = name

    def delete_account(self, node_id: str) -> None:
        """Lookups answer NOT_FOUND, and every reference to the account is null or gone, its
        REST compare commits' included, and so are its forks as head repositories. Only a commit
        added afterwards can name it (gap 90)."""
        data = self.accounts.get(node_id)
        if data is not None:
            self._deleted[node_id] = data
        self.accounts[node_id] = None
        for pull in self.pulls():
            if names(pull.node.get("headRepositoryOwner"), node_id):
                pull.node["headRepository"] = None
            forget(pull.node, node_id)
            forget(pull.compare, node_id)
            forget(pull.heads, node_id)

    def restore_account(self, node_id: str) -> None:
        """Lookups find the account again; references it lost stay lost."""
        self.accounts[node_id] = self._deleted.pop(node_id)

    # -- answering the replay ---------------------------------------------------------------
    def pulls(self) -> Iterator[PullRequest]:
        """Every pull request once (a renamed repository is listed under both names)."""
        seen: set[int] = set()
        for repository in self.repositories.values():
            if id(repository) not in seen:
                seen.add(id(repository))
                yield from repository.pulls.values()

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
        """The unit the replay serves for ``kind`` and ``key``. ``None`` leaves the request
        unanswered, which the replay reports: a kind the model does not render, a compare of a
        head no pull request has, or a pull request known only from the index."""
        data: Any = None
        errors: list[dict[str, Any]] = []
        match kind:
            case "repository":
                # Asked before an index or batch request (``for`` names it): a unit only for a
                # repository GitHub cannot find, stamped as that request's units are.
                if self.repository(key["owner"], key["name"]) is not None:
                    return None
                errors = missing_repository(key["owner"], key["name"])
                return self._unit(key["for"], key, data, errors)
            case "resolve":
                repository = self.repository(key["owner"], key["name"])
                if repository is None:
                    errors = missing_repository(key["owner"], key["name"])
                else:
                    data = repository.render()
            case "repository_labels":
                labelled = next(
                    (r for r in self.repositories.values() if r.data["id"] == key["id"]), None
                )
                if labelled is None or labelled.gone:
                    errors = missing_node(key["id"])
                else:
                    labels = page(labelled.data["labels"], labelled.label_cursors, key["after"])
                    data = {"id": key["id"], "labels": labels}
            case "index":
                indexed = self.repositories[f"{key['owner']}/{key['name']}".lower()]
                data = page(indexed.index(), indexed.index_cursors, key["after"])
            case "pull_request":
                held = self.repositories[f"{key['owner']}/{key['name']}".lower()]
                pull = held.pulls.get(key["number"])
                if pull is None or pull.gone:
                    errors = not_found(
                        f"Could not resolve to a PullRequest with the number of {key['number']}."
                    )
                elif not pull.hydratable:
                    return None
                else:
                    data = pull.render()
            case "overflow":
                continued = self.pull_request(key["id"])
                if continued is None:
                    errors = missing_node(key["id"])
                else:
                    data = continued.continuation(key["connection"], key["after"])
            case "user":
                account = self.accounts.get(key["id"])
                if account is None:
                    errors = missing_node(key["id"])
                else:
                    data = copy.deepcopy(account)
            case "compare":
                data = self._compare(key)
                if data is None:
                    return None
            case _:
                return None
        return self._unit(kind, key, data, errors)

    def _compare(self, key: dict[str, Any]) -> Node | None:
        repository = self.repository(key["owner"], key["name"])
        asked = (key["base"], key["head"])
        for pull in repository.pulls.values() if repository is not None else ():
            if not pull.hydratable:
                continue
            if asked == (pull.node["baseRefOid"], pull.node["headRefOid"]):
                listed = pull.compare_commits()
            elif asked in pull.heads:
                listed = pull.heads[asked]
            else:
                continue
            start = (key["page"] - 1) * key["per_page"]
            commits = listed[start : start + key["per_page"]]
            return {"total_commits": len(listed), "commits": copy.deepcopy(commits)}
        return None

    def _unit(
        self, kind: str, key: dict[str, Any], data: Any, errors: list[dict[str, Any]]
    ) -> Unit | None:
        """A unit stamped as the recordings of its kind are; none when no recording checks the
        model's rendering of it: a continuation of labels, closing issues, user-linked closing
        issues, review requests, or assignees."""
        connection = key.get("connection") if kind == "overflow" else None
        stamp = _templates().stamps.get((kind, connection))
        if stamp is None:
            return None
        now = self.clock.now().astimezone(UTC)
        return Unit(
            kind=kind,
            key=dict(key),
            document_sha256=stamp,
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

        def repository_of(unit: Unit) -> Repository:
            return world.repositories[f"{unit.key['owner']}/{unit.key['name']}".lower()]

        for unit in by_kind["resolve"]:
            repository = Repository(unit.data)
            repository.data["labels"] = joined(unit.data["labels"], repository.label_cursors)
            world.repositories[repository.full_name.lower()] = repository
            world.repositories[f"{unit.key['owner']}/{unit.key['name']}".lower()] = repository
        for unit in by_kind["repository_labels"]:
            labelled = next(
                r for r in world.repositories.values() if r.data["id"] == unit.key["id"]
            )
            joined(unit.data["labels"], labelled.label_cursors, labelled.data["labels"])
        for unit in sorted(by_kind["index"], key=lambda u: u.key["after"] is not None):
            indexed = repository_of(unit)
            for entry in unit.data["nodes"]:
                indexed.pulls[entry["number"]] = PullRequest(world, entry)
            indexed.index_cursors.record(unit.data["nodes"], unit.data["pageInfo"]["endCursor"])
        for unit in by_kind["pull_request"]:
            hydrated = repository_of(unit)
            if unit.data is None:
                gone = PullRequest(world, {"number": unit.key["number"]}, gone=True)
                hydrated.pulls[gone.number] = gone
                continue
            pull = PullRequest(world, unit.data)
            for key in CONNECTIONS_BY_KEY:
                pull.node[key] = joined(unit.data[key], pull.cursors[key])
            hydrated.pulls[pull.number] = pull
            for link in pull.node["closingIssuesReferences"]["nodes"]:
                issue = (link["repository"]["databaseId"], link["number"])
                world._issues[issue] = int(link["fullDatabaseId"])
        for unit in by_kind["overflow"]:
            continued = world.pull_request(unit.key["id"])
            assert continued is not None, f"overflow {unit.key} continues no recorded pull request"
            connection = unit.key["connection"]
            joined(unit.data[connection], continued.cursors[connection], continued.node[connection])
        for unit in sorted(by_kind["compare"], key=lambda u: u.key["page"]):
            refs = (unit.key["base"], unit.key["head"])
            for pull in repository_of(unit).pulls.values():
                if (pull.node.get("baseRefOid"), pull.node.get("headRefOid")) == refs:
                    commits = [read_commit(commit) for commit in unit.data["commits"]]
                    pull.compare = (pull.compare or []) + commits
        for unit in by_kind["user"]:
            world.accounts[unit.key["id"]] = unit.data
        return world


@dataclass
class _Templates:
    stamps: dict[tuple[str, str | None], str] = field(default_factory=dict)
    repository: Node = field(default_factory=dict)
    pull_request: Node = field(default_factory=dict)
    nodes: dict[str, Node] = field(default_factory=dict)
    accounts: dict[str, Node] = field(default_factory=dict)


@functools.cache
def _templates() -> _Templates:
    """What the model copies from the recordings, read once: each kind's stamp (a kind recorded
    under two stamps fails the round trip), the objects new ones are copied from, and the first
    recorded node of each connection."""
    found = _Templates()
    for unit in recorded_units():
        key = unit.key
        found.stamps[(unit.kind, key.get("connection"))] = unit.document_sha256
        if unit.kind == "resolve" and (key["owner"], key["name"]) == REPOSITORY_TEMPLATE:
            found.repository.update(unit.data)
        if unit.kind == "pull_request" and unit.data is not None:
            if (key["owner"], key["name"], key["number"]) == PULL_REQUEST_TEMPLATE:
                found.pull_request.update(unit.data)
            for connection in CONNECTIONS_BY_KEY:
                if unit.data[connection]["nodes"]:
                    found.nodes.setdefault(connection, unit.data[connection]["nodes"][0])
        if unit.kind == "user" and unit.data is not None:
            found.accounts.setdefault(unit.data["__typename"], unit.data)
    return found


def recorded_units() -> Iterator[Unit]:
    """The recordings the model renders: every unit but the rate-limit readings."""
    return (unit for unit in loaded_units() if unit.kind in RENDERED)
