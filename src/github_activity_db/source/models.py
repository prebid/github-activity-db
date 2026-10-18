"""Typed models of the GitHub responses the source layer reads. Private to ``source``.

GraphQL models forbid unknown fields: the documents select every field, so an extra one means a
document and its model disagree. Nullability follows GitHub's schema; a value the source layer
cannot use (a null id, an unknown actor type, a new state) fails validation, and with it what the
response describes: the item in a hydration batch, the repository on a resolve, label, or index
page, the node id in a user lookup (redesign plan 5.3 step 4). Descriptive enums stay ``str`` so a
new GitHub value is stored.

The REST models ignore unknown fields: a REST response carries what GitHub adds over time, not
what a document selected, so an added field is not a disagreement.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
)

_DIGITS = re.compile(r"[0-9]+")
_ISO_8601 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)


def _big_int(value: object) -> int:
    """GraphQL ``BigInt`` arrives as a JSON string of digits; nothing else is accepted."""
    if isinstance(value, str) and _DIGITS.fullmatch(value):
        return int(value)
    raise ValueError(f"expected a BigInt, a string of digits: {value!r}")


def _iso_string(value: object) -> object:
    """GitHub's ``DateTime`` is an ISO 8601 string with a zone; digits are not a timestamp here."""
    if not isinstance(value, str) or not _ISO_8601.fullmatch(value):
        raise ValueError(f"expected an ISO 8601 date-time string: {value!r}")
    return value


def _no_empty_account(value: object) -> object:
    """REST describes a commit's linked account as an account, an empty object, or null."""
    return None if value == {} else value


# Types are checked strictly: JSON from GitHub has exact types, and lax coercion would read
# "no" as False or "12" as 12.
BigInt = Annotated[int, PlainValidator(_big_int)]
Timestamp = Annotated[AwareDatetime, BeforeValidator(_iso_string)]


class GraphQLModel(BaseModel):
    """A GraphQL object: unknown fields rejected; camelCase wire names given per field."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RestModel(BaseModel):
    """A REST object: snake_case on the wire, unknown fields ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)


# --------------------------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------------------------
class RateLimitReading(GraphQLModel):
    cost: StrictInt
    remaining: StrictInt
    reset_at: Timestamp = Field(alias="resetAt")


class PageInfo(GraphQLModel):
    has_next_page: StrictBool = Field(alias="hasNextPage")
    end_cursor: StrictStr | None = Field(alias="endCursor")


class Page[NodeT](GraphQLModel):
    """A connection page. ``nodes`` and its entries are nullable in GitHub's schema."""

    total_count: StrictInt = Field(alias="totalCount")
    page_info: PageInfo = Field(alias="pageInfo")
    nodes: list[NodeT | None] | None


class IdentifiedActor(GraphQLModel):
    typename: Literal["User", "Bot", "Organization", "Mannequin"] = Field(alias="__typename")
    login: StrictStr
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")


class EnterpriseActor(GraphQLModel):
    """The one ``Actor`` type without ``databaseId``."""

    typename: Literal["EnterpriseUserAccount"] = Field(alias="__typename")
    login: StrictStr


Actor = Annotated[IdentifiedActor | EnterpriseActor, Field(discriminator="typename")]


class Login(GraphQLModel):
    login: StrictStr


class LabelNode(GraphQLModel):
    name: StrictStr


class IssueRepository(GraphQLModel):
    database_id: StrictInt = Field(alias="databaseId")
    name_with_owner: StrictStr = Field(alias="nameWithOwner")


class ClosingIssueNode(GraphQLModel):
    number: StrictInt
    repository: IssueRepository


# --------------------------------------------------------------------------------------------
# Resolve
# --------------------------------------------------------------------------------------------
class BranchRef(GraphQLModel):
    name: StrictStr


class LabelPage(GraphQLModel):
    total_count: StrictInt = Field(alias="totalCount")
    page_info: PageInfo = Field(alias="pageInfo")
    nodes: list[LabelNode | None] | None


class RepositoryNode(GraphQLModel):
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    name_with_owner: StrictStr = Field(alias="nameWithOwner")
    name: StrictStr
    owner: Login
    is_private: StrictBool = Field(alias="isPrivate")
    is_archived: StrictBool = Field(alias="isArchived")
    default_branch_ref: BranchRef | None = Field(alias="defaultBranchRef")
    labels: LabelPage


class RepositoryLabelsNode(GraphQLModel):
    id: StrictStr
    labels: LabelPage


# --------------------------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------------------------
class IndexClosingIssues(GraphQLModel):
    total_count: StrictInt = Field(alias="totalCount")
    nodes: list[ClosingIssueNode | None] | None


class IndexedPullRequestNode(GraphQLModel):
    number: StrictInt
    full_database_id: BigInt = Field(alias="fullDatabaseId")
    state: Literal["OPEN", "CLOSED", "MERGED"]
    is_draft: StrictBool = Field(alias="isDraft")
    author_association: StrictStr = Field(alias="authorAssociation")
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    merged_at: Timestamp | None = Field(alias="mergedAt")
    closing_issues_references: IndexClosingIssues | None = Field(alias="closingIssuesReferences")


class PullRequestIndexPage(GraphQLModel):
    total_count: StrictInt = Field(alias="totalCount")
    page_info: PageInfo = Field(alias="pageInfo")
    nodes: list[IndexedPullRequestNode | None] | None


# --------------------------------------------------------------------------------------------
# Hydration
# --------------------------------------------------------------------------------------------
class ReviewNode(GraphQLModel):
    full_database_id: BigInt = Field(alias="fullDatabaseId")
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"]
    author: Actor | None


class CommentNode(GraphQLModel):
    full_database_id: BigInt = Field(alias="fullDatabaseId")
    author: Actor | None


class CommitUser(GraphQLModel):
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    login: StrictStr


class GitActor(GraphQLModel):
    name: StrictStr | None
    email: StrictStr | None
    user: CommitUser | None


class Commit(GraphQLModel):
    oid: StrictStr
    authored_date: Timestamp = Field(alias="authoredDate")
    author: GitActor | None


class CommitNode(GraphQLModel):
    commit: Commit


class FileNode(GraphQLModel):
    path: StrictStr
    change_type: StrictStr = Field(alias="changeType")
    additions: StrictInt
    deletions: StrictInt


class PullRequestNode(GraphQLModel):
    id: StrictStr
    number: StrictInt
    full_database_id: BigInt = Field(alias="fullDatabaseId")
    state: Literal["OPEN", "CLOSED", "MERGED"]
    is_draft: StrictBool = Field(alias="isDraft")
    title: StrictStr
    body: StrictStr
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    merged_at: Timestamp | None = Field(alias="mergedAt")
    base_ref_name: StrictStr = Field(alias="baseRefName")
    base_ref_oid: StrictStr = Field(alias="baseRefOid")
    head_ref_oid: StrictStr = Field(alias="headRefOid")
    is_cross_repository: StrictBool = Field(alias="isCrossRepository")
    author_association: StrictStr = Field(alias="authorAssociation")
    additions: StrictInt
    deletions: StrictInt
    changed_files: StrictInt = Field(alias="changedFiles")
    author: Actor | None
    merged_by: Actor | None = Field(alias="mergedBy")
    labels: Page[LabelNode] | None
    closing_issues_references: Page[ClosingIssueNode] | None = Field(
        alias="closingIssuesReferences"
    )
    reviews: Page[ReviewNode] | None
    comments: Page[CommentNode] | None
    commits: Page[CommitNode] | None
    files: Page[FileNode] | None


class OverflowHead(GraphQLModel):
    """A continuation's PR head and update time; its connection page is parsed separately."""

    head_ref_oid: StrictStr = Field(alias="headRefOid")
    updated_at: Timestamp = Field(alias="updatedAt")


# --------------------------------------------------------------------------------------------
# User lookup
# --------------------------------------------------------------------------------------------
class LookedUpUser(GraphQLModel):
    typename: Literal["User", "Bot", "Organization", "Mannequin"] = Field(alias="__typename")
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    login: StrictStr


# --------------------------------------------------------------------------------------------
# REST rate limits and compare
# --------------------------------------------------------------------------------------------
class RestRateLimitPool(BaseModel):
    """One pool of REST ``/rate_limit``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    limit: StrictInt
    used: StrictInt
    remaining: StrictInt
    reset: StrictInt


class RestRateLimits(BaseModel):
    """``/rate_limit``: the pools are read from ``resources``; the top-level ``rate`` (equal to
    ``resources.core`` in the recorded response) is ignored like any other field."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    resources: dict[StrictStr, RestRateLimitPool]


class CompareGitAuthor(RestModel):
    name: StrictStr | None = None
    email: StrictStr | None = None
    date: Timestamp


class CompareGitCommit(RestModel):
    author: CompareGitAuthor | None


class CompareAccount(RestModel):
    id: StrictInt
    node_id: StrictStr
    login: StrictStr


class CompareCommit(RestModel):
    sha: StrictStr
    commit: CompareGitCommit
    author: Annotated[CompareAccount | None, BeforeValidator(_no_empty_account)]


class ComparePage(RestModel):
    total_commits: StrictInt
    commits: list[CompareCommit]
