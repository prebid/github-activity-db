"""GraphQL documents the source layer sends (redesign plan 5.8).

Every connection's node selection is defined once and used both in the first page and in its
overflow continuation, so the two can never select different fields.

A kind's ``hydration_version`` changes whenever its documents do: ``tests/query_versions.json``
maps each ``<kind>:<version>`` to the SHA-256 of the normalized documents, and a test fails when
they change without a new entry (plan Step 2).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from .limits import INDEX_CLOSING_LINKS, PAGE_SIZE

PULL_REQUEST_KIND: Final = "pull_request"
PULL_REQUEST_HYDRATION_VERSION: Final = 1

RATE_LIMIT: Final = "rateLimit { cost remaining resetAt }"

ACTOR_FRAGMENT: Final = """
fragment Actor on Actor {
  __typename
  login
  ... on User { id databaseId }
  ... on Bot { id databaseId }
  ... on Organization { id databaseId }
  ... on Mannequin { id databaseId }
}
"""


@dataclass(frozen=True)
class Connection:
    """A pull request connection fetched in full: first page, then overflow continuations."""

    field: str
    arguments: str
    nodes: str

    @property
    def uses_actor(self) -> bool:
        """Whether the node selection spreads the ``Actor`` fragment.

        GitHub rejects a document that defines a fragment it does not use.
        """
        return "...Actor" in self.nodes

    def selection(self, after_variable: str | None = None) -> str:
        """The connection's selection, continuing after ``$<after_variable>`` when given."""
        arguments = [f"first: {PAGE_SIZE}"]
        if after_variable is not None:
            arguments.append(f"after: ${after_variable}")
        if self.arguments:
            arguments.append(self.arguments)
        return (
            f"{self.field}({', '.join(arguments)}) {{ totalCount "
            f"pageInfo {{ hasNextPage endCursor }} nodes {{ {self.nodes} }} }}"
        )


LABELS: Final = Connection("labels", "", "name")
CLOSING_ISSUES: Final = Connection(
    "closingIssuesReferences", "", "number repository { databaseId nameWithOwner }"
)
# Requested without PENDING, so totalCount counts exactly the reviews that are stored (D4).
REVIEWS: Final = Connection(
    "reviews",
    "states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]",
    "fullDatabaseId state author { ...Actor }",
)
COMMENTS: Final = Connection("comments", "", "author { ...Actor }")
# No connection inside `commits`: GraphQL charges for every possible nested node (D15).
COMMITS: Final = Connection(
    "commits",
    "",
    "commit { oid authoredDate author { name email user { id databaseId login } } }",
)
FILES: Final = Connection("files", "", "path changeType additions deletions")

PULL_REQUEST_CONNECTIONS: Final = (LABELS, CLOSING_ISSUES, REVIEWS, COMMENTS, COMMITS, FILES)
CONNECTIONS_BY_FIELD: Final = {
    connection.field: connection for connection in PULL_REQUEST_CONNECTIONS
}

PULL_REQUEST_FIELDS: Final = f"""
fragment PullRequestFields on PullRequest {{
  id number fullDatabaseId state isDraft title body createdAt updatedAt mergedAt
  baseRefName baseRefOid headRefOid isCrossRepository authorAssociation
  additions deletions changedFiles
  author {{ ...Actor }} mergedBy {{ ...Actor }}
  {" ".join(connection.selection() for connection in PULL_REQUEST_CONNECTIONS)}
}}
"""

RESOLVE: Final = f"""
query Resolve($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{
    id databaseId nameWithOwner name owner {{ login }} isPrivate isArchived
    defaultBranchRef {{ name }}
    labels(first: {PAGE_SIZE}) {{
      totalCount pageInfo {{ hasNextPage endCursor }} nodes {{ name }}
    }}
  }}
  {RATE_LIMIT}
}}
"""

REPOSITORY_LABELS: Final = f"""
query RepositoryLabels($id: ID!, $after: String!) {{
  node(id: $id) {{
    ... on Repository {{
      id
      labels(first: {PAGE_SIZE}, after: $after) {{
        totalCount pageInfo {{ hasNextPage endCursor }} nodes {{ name }}
      }}
    }}
  }}
  {RATE_LIMIT}
}}
"""

PULL_REQUEST_INDEX: Final = f"""
query PullRequestIndex($owner: String!, $name: String!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(
      first: {PAGE_SIZE}, after: $after, orderBy: {{field: CREATED_AT, direction: ASC}}
    ) {{
      totalCount
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        number fullDatabaseId state isDraft authorAssociation createdAt updatedAt mergedAt
        closingIssuesReferences(first: {INDEX_CLOSING_LINKS}) {{
          totalCount nodes {{ number repository {{ databaseId nameWithOwner }} }}
        }}
      }}
    }}
  }}
  {RATE_LIMIT}
}}
"""

USER_LOOKUP: Final = f"""
query UserLookup($ids: [ID!]!) {{
  nodes(ids: $ids) {{
    __typename id
    ... on User {{ databaseId login }}
    ... on Bot {{ databaseId login }}
    ... on Organization {{ databaseId login }}
    ... on Mannequin {{ databaseId login }}
  }}
  {RATE_LIMIT}
}}
"""


def pull_request_alias(index: int) -> str:
    """Alias of the ``index``-th pull request in a batch."""
    return f"p{index}"


def pull_request_batch(count: int) -> str:
    """A query reading ``count`` pull requests of one repository, numbers in ``$n0``..."""
    variables = " ".join(f"$n{i}: Int!" for i in range(count))
    aliases = " ".join(
        f"{pull_request_alias(i)}: pullRequest(number: $n{i}) {{ ...PullRequestFields }}"
        for i in range(count)
    )
    return (
        f"query PullRequestBatch($owner: String!, $name: String!, {variables}) {{ "
        f"repository(owner: $owner, name: $name) {{ databaseId {aliases} }} {RATE_LIMIT} }}"
        f"{PULL_REQUEST_FIELDS}{ACTOR_FRAGMENT}"
    )


def overflow_alias(index: int, connection: Connection) -> str:
    """Alias of the ``index``-th continuation, named after its connection."""
    return f"o{index}_{connection.field}"


def overflow_continuation(connections: list[Connection]) -> str:
    """A query continuing one connection per alias: node ids ``$id<i>``, cursors ``$after<i>``.

    Each alias also reads ``headRefOid`` and ``updatedAt``, so a change between pages is seen.
    """
    variables = " ".join(f"$id{i}: ID! $after{i}: String!" for i in range(len(connections)))
    aliases = " ".join(
        f"{overflow_alias(i, connection)}: node(id: $id{i}) {{ ... on PullRequest {{ "
        f"headRefOid updatedAt {connection.selection(f'after{i}')} }} }}"
        for i, connection in enumerate(connections)
    )
    fragments = ACTOR_FRAGMENT if any(connection.uses_actor for connection in connections) else ""
    return f"query Overflow({variables}) {{ {aliases} {RATE_LIMIT} }}{fragments}"


_COMMENT = re.compile(r"#[^\n]*")
_WHITESPACE = re.compile(r"\s+")


def normalize(document: str) -> str:
    """Document text without comments and with each whitespace run collapsed to one space."""
    return _WHITESPACE.sub(" ", _COMMENT.sub("", document)).strip()


def document_hash(*documents: str) -> str:
    """SHA-256 of the normalized documents, joined by newlines."""
    joined = "\n".join(normalize(document) for document in documents)
    return hashlib.sha256(joined.encode()).hexdigest()


def pull_request_documents() -> tuple[str, ...]:
    """What the pull request kind's ``hydration_version`` covers: every document it sends.

    A one-alias batch (its wrapper, the field and actor fragments) and a one-alias continuation of
    each connection (its head fields and selection).
    """
    continuations = tuple(
        overflow_continuation([connection]) for connection in PULL_REQUEST_CONNECTIONS
    )
    return (pull_request_batch(1), *continuations)
