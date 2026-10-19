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


class DatabaseIdRef(GraphQLModel):
    """An object GitHub refers to by its ``databaseId`` alone, beside an account's own identity (an
    enterprise account's user, a mannequin's claimant). ``User.databaseId`` is nullable in GitHub's
    schema, so the value may be null; the field itself is required."""

    database_id: StrictInt | None = Field(alias="databaseId")


class EnterpriseActor(GraphQLModel):
    """The one ``Actor`` type without ``databaseId``: its node id and its user's id instead."""

    typename: Literal["EnterpriseUserAccount"] = Field(alias="__typename")
    login: StrictStr
    id: StrictStr
    user: DatabaseIdRef | None


Actor = Annotated[IdentifiedActor | EnterpriseActor, Field(discriminator="typename")]


class Login(GraphQLModel):
    login: StrictStr


class AccountFields(GraphQLModel):
    """An account selected through a union (``id databaseId login``), not through ``Actor``."""

    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    login: StrictStr


class LabelNode(GraphQLModel):
    id: StrictStr
    name: StrictStr


class IssueRepository(GraphQLModel):
    database_id: StrictInt = Field(alias="databaseId")
    name_with_owner: StrictStr = Field(alias="nameWithOwner")


class ClosingIssueNode(GraphQLModel):
    """A link as the index reads it."""

    number: StrictInt
    repository: IssueRepository


# --------------------------------------------------------------------------------------------
# Resolve
# --------------------------------------------------------------------------------------------
class BranchRef(GraphQLModel):
    name: StrictStr


class RepositoryLabelNode(GraphQLModel):
    id: StrictStr
    name: StrictStr
    color: StrictStr
    description: StrictStr | None
    is_default: StrictBool = Field(alias="isDefault")
    created_at: Timestamp | None = Field(alias="createdAt")
    updated_at: Timestamp | None = Field(alias="updatedAt")


class LabelPage(GraphQLModel):
    total_count: StrictInt = Field(alias="totalCount")
    page_info: PageInfo = Field(alias="pageInfo")
    nodes: list[RepositoryLabelNode | None] | None


class LanguageRef(GraphQLModel):
    name: StrictStr


class LicenseRef(GraphQLModel):
    spdx_id: StrictStr | None = Field(alias="spdxId")


class PlanFeatures(GraphQLModel):
    codeowners: StrictBool
    team_review_requests: StrictBool = Field(alias="teamReviewRequests")
    maximum_manual_review_requests: StrictInt = Field(alias="maximumManualReviewRequests")


class RepositoryNode(GraphQLModel):
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    name_with_owner: StrictStr = Field(alias="nameWithOwner")
    name: StrictStr
    owner: Login
    is_private: StrictBool = Field(alias="isPrivate")
    is_archived: StrictBool = Field(alias="isArchived")
    is_security_policy_enabled: StrictBool | None = Field(alias="isSecurityPolicyEnabled")
    visibility: StrictStr
    description: StrictStr | None
    homepage_url: StrictStr | None = Field(alias="homepageUrl")
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    pushed_at: Timestamp | None = Field(alias="pushedAt")
    archived_at: Timestamp | None = Field(alias="archivedAt")
    stargazer_count: StrictInt = Field(alias="stargazerCount")
    fork_count: StrictInt = Field(alias="forkCount")
    disk_usage: StrictInt | None = Field(alias="diskUsage")
    forking_allowed: StrictBool = Field(alias="forkingAllowed")
    allow_update_branch: StrictBool = Field(alias="allowUpdateBranch")
    web_commit_signoff_required: StrictBool = Field(alias="webCommitSignoffRequired")
    has_wiki_enabled: StrictBool = Field(alias="hasWikiEnabled")
    has_discussions_enabled: StrictBool = Field(alias="hasDiscussionsEnabled")
    has_projects_enabled: StrictBool = Field(alias="hasProjectsEnabled")
    has_vulnerability_alerts_enabled: StrictBool = Field(alias="hasVulnerabilityAlertsEnabled")
    uses_custom_open_graph_image: StrictBool = Field(alias="usesCustomOpenGraphImage")
    merge_commit_allowed: StrictBool = Field(alias="mergeCommitAllowed")
    squash_merge_allowed: StrictBool = Field(alias="squashMergeAllowed")
    rebase_merge_allowed: StrictBool = Field(alias="rebaseMergeAllowed")
    delete_branch_on_merge: StrictBool = Field(alias="deleteBranchOnMerge")
    merge_commit_title: StrictStr = Field(alias="mergeCommitTitle")
    merge_commit_message: StrictStr = Field(alias="mergeCommitMessage")
    squash_merge_commit_title: StrictStr = Field(alias="squashMergeCommitTitle")
    squash_merge_commit_message: StrictStr = Field(alias="squashMergeCommitMessage")
    primary_language: LanguageRef | None = Field(alias="primaryLanguage")
    license_info: LicenseRef | None = Field(alias="licenseInfo")
    default_branch_ref: BranchRef | None = Field(alias="defaultBranchRef")
    plan_features: PlanFeatures = Field(alias="planFeatures")
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
    closed_at: Timestamp | None = Field(alias="closedAt")
    closing_issues_references: IndexClosingIssues | None = Field(alias="closingIssuesReferences")


class PullRequestIndexPage(GraphQLModel):
    total_count: StrictInt = Field(alias="totalCount")
    page_info: PageInfo = Field(alias="pageInfo")
    nodes: list[IndexedPullRequestNode | None] | None


# --------------------------------------------------------------------------------------------
# Hydration
# --------------------------------------------------------------------------------------------
class Oid(GraphQLModel):
    oid: StrictStr


class Count(GraphQLModel):
    """A connection selected for its ``totalCount`` alone."""

    total_count: StrictInt = Field(alias="totalCount")


class LinkedIssueNode(GraphQLModel):
    """A closing link as hydration reads it, with the issue's own id."""

    full_database_id: BigInt = Field(alias="fullDatabaseId")
    number: StrictInt
    repository: IssueRepository


class UserLinkedIssueRepository(GraphQLModel):
    database_id: StrictInt = Field(alias="databaseId")


class UserLinkedIssueNode(GraphQLModel):
    number: StrictInt
    repository: UserLinkedIssueRepository


class ReviewNode(GraphQLModel):
    id: StrictStr
    full_database_id: BigInt = Field(alias="fullDatabaseId")
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"]
    created_at: Timestamp = Field(alias="createdAt")
    submitted_at: Timestamp | None = Field(alias="submittedAt")
    author_association: StrictStr = Field(alias="authorAssociation")
    author_can_push_to_repository: StrictBool = Field(alias="authorCanPushToRepository")
    updated_at: Timestamp = Field(alias="updatedAt")
    last_edited_at: Timestamp | None = Field(alias="lastEditedAt")
    published_at: Timestamp | None = Field(alias="publishedAt")
    includes_created_edit: StrictBool = Field(alias="includesCreatedEdit")
    commit: Oid | None
    comments: Count
    author: Actor | None
    editor: Actor | None


class CommentNode(GraphQLModel):
    id: StrictStr
    full_database_id: BigInt = Field(alias="fullDatabaseId")
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    last_edited_at: Timestamp | None = Field(alias="lastEditedAt")
    author_association: StrictStr = Field(alias="authorAssociation")
    published_at: Timestamp | None = Field(alias="publishedAt")
    created_via_email: StrictBool = Field(alias="createdViaEmail")
    includes_created_edit: StrictBool = Field(alias="includesCreatedEdit")
    author: Actor | None
    editor: Actor | None


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


class RequestedAccount(AccountFields):
    typename: Literal["User", "Bot", "Mannequin"] = Field(alias="__typename")


class RequestedTeamNode(GraphQLModel):
    typename: Literal["Team"] = Field(alias="__typename")
    id: StrictStr
    database_id: StrictInt | None = Field(alias="databaseId")
    slug: StrictStr


class RequestedEnterpriseTeam(GraphQLModel):
    """An enterprise team, which GitHub identifies by ``fullDatabaseId`` (nullable), not
    ``databaseId``."""

    typename: Literal["EnterpriseTeam"] = Field(alias="__typename")
    id: StrictStr
    full_database_id: BigInt | None = Field(alias="fullDatabaseId")
    slug: StrictStr


RequestedReviewer = Annotated[
    RequestedAccount | RequestedTeamNode | RequestedEnterpriseTeam,
    Field(discriminator="typename"),
]


class ReviewRequestNode(GraphQLModel):
    id: StrictStr
    database_id: StrictInt | None = Field(alias="databaseId")
    as_code_owner: StrictBool = Field(alias="asCodeOwner")
    requested_reviewer: RequestedReviewer | None = Field(alias="requestedReviewer")


class AssignedActorNode(AccountFields):
    typename: Literal["User", "Bot", "Mannequin", "Organization"] = Field(alias="__typename")


class HeadRepository(GraphQLModel):
    database_id: StrictInt | None = Field(alias="databaseId")
    name_with_owner: StrictStr = Field(alias="nameWithOwner")


class RepositoryOwnerRef(GraphQLModel):
    id: StrictStr
    login: StrictStr


class MilestoneRef(GraphQLModel):
    number: StrictInt
    title: StrictStr


class StackNode(GraphQLModel):
    id: StrictStr
    number: StrictInt
    size: StrictInt
    base_ref_name: StrictStr = Field(alias="baseRefName")


class StackEntryNode(GraphQLModel):
    id: StrictStr
    position: StrictInt


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
    closed_at: Timestamp | None = Field(alias="closedAt")
    last_edited_at: Timestamp | None = Field(alias="lastEditedAt")
    base_ref_name: StrictStr = Field(alias="baseRefName")
    base_ref_oid: StrictStr = Field(alias="baseRefOid")
    head_ref_name: StrictStr = Field(alias="headRefName")
    head_ref_oid: StrictStr = Field(alias="headRefOid")
    is_cross_repository: StrictBool = Field(alias="isCrossRepository")
    author_association: StrictStr = Field(alias="authorAssociation")
    head_repository: HeadRepository | None = Field(alias="headRepository")
    merge_commit: Oid | None = Field(alias="mergeCommit")
    milestone: MilestoneRef | None
    review_decision: StrictStr | None = Field(alias="reviewDecision")
    total_comments_count: StrictInt | None = Field(alias="totalCommentsCount")
    published_at: Timestamp | None = Field(alias="publishedAt")
    includes_created_edit: StrictBool = Field(alias="includesCreatedEdit")
    maintainer_can_modify: StrictBool = Field(alias="maintainerCanModify")
    head_repository_owner: RepositoryOwnerRef | None = Field(alias="headRepositoryOwner")
    mergeable: StrictStr
    merge_state_status: StrictStr = Field(alias="mergeStateStatus")
    can_be_rebased: StrictBool = Field(alias="canBeRebased")
    potential_merge_commit: Oid | None = Field(alias="potentialMergeCommit")
    stack: StackNode | None
    stack_entry: StackEntryNode | None = Field(alias="stackEntry")
    additions: StrictInt
    deletions: StrictInt
    changed_files: StrictInt = Field(alias="changedFiles")
    author: Actor | None
    merged_by: Actor | None = Field(alias="mergedBy")
    editor: Actor | None
    labels: Page[LabelNode] | None
    closing_issues_references: Page[LinkedIssueNode] | None = Field(alias="closingIssuesReferences")
    user_linked_closing_issues: Page[UserLinkedIssueNode] | None = Field(
        alias="userLinkedClosingIssues"
    )
    reviews: Page[ReviewNode] | None
    comments: Page[CommentNode] | None
    commits: Page[CommitNode] | None
    files: Page[FileNode] | None
    review_requests: Page[ReviewRequestNode] | None = Field(alias="reviewRequests")
    assigned_actors: Page[AssignedActorNode] | None = Field(alias="assignedActors")


class OverflowHead(GraphQLModel):
    """A continuation's PR head and update time; its connection page is parsed separately."""

    head_ref_oid: StrictStr = Field(alias="headRefOid")
    updated_at: Timestamp = Field(alias="updatedAt")


# --------------------------------------------------------------------------------------------
# User lookup
# --------------------------------------------------------------------------------------------
class LookedUpUserNode(GraphQLModel):
    typename: Literal["User"] = Field(alias="__typename")
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    login: StrictStr
    name: StrictStr | None
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    company: StrictStr | None
    location: StrictStr | None
    website_url: StrictStr | None = Field(alias="websiteUrl")


class LookedUpBotNode(GraphQLModel):
    typename: Literal["Bot"] = Field(alias="__typename")
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    login: StrictStr
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")


class LookedUpOrganizationNode(GraphQLModel):
    typename: Literal["Organization"] = Field(alias="__typename")
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    login: StrictStr
    name: StrictStr | None
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    description: StrictStr | None
    email: StrictStr | None
    location: StrictStr | None
    website_url: StrictStr | None = Field(alias="websiteUrl")
    twitter_username: StrictStr | None = Field(alias="twitterUsername")
    is_verified: StrictBool = Field(alias="isVerified")
    archived_at: Timestamp | None = Field(alias="archivedAt")
    has_sponsors_listing: StrictBool = Field(alias="hasSponsorsListing")
    pinned_items_remaining: StrictInt = Field(alias="pinnedItemsRemaining")


class LookedUpMannequinNode(GraphQLModel):
    typename: Literal["Mannequin"] = Field(alias="__typename")
    id: StrictStr
    database_id: StrictInt = Field(alias="databaseId")
    login: StrictStr
    name: StrictStr | None
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    claimant: DatabaseIdRef | None


class LookedUpEnterpriseUserNode(GraphQLModel):
    typename: Literal["EnterpriseUserAccount"] = Field(alias="__typename")
    id: StrictStr
    login: StrictStr
    name: StrictStr | None
    created_at: Timestamp = Field(alias="createdAt")
    updated_at: Timestamp = Field(alias="updatedAt")
    user: DatabaseIdRef | None


LookedUpNode = Annotated[
    LookedUpUserNode
    | LookedUpBotNode
    | LookedUpOrganizationNode
    | LookedUpMannequinNode
    | LookedUpEnterpriseUserNode,
    Field(discriminator="typename"),
]


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
