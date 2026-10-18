"""Records the source layer hands to the sync engine (redesign plan 5.7).

These are the only types that cross from ``source`` into ``sync``: the response models stay
private. Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .errors import ItemError, UserLookupFailed

AccountType = Literal["User", "Bot", "Organization", "Mannequin", "EnterpriseUserAccount"]
PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
ReviewState = Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"]
CommitSource = Literal["graphql", "compare"]


@dataclass(frozen=True, slots=True)
class Account:
    """A GitHub account as an actor sighting or a ``nodes(ids:)`` lookup reports it.

    Those two are the only sources of an account's type and login (plan 5.2). An
    ``EnterpriseUserAccount`` has no ``databaseId``: its ``github_id`` is None, it is identified
    by its node id, and ``linked_user_github_id`` holds its user's ``databaseId`` when GitHub
    returns one (gap 85).
    """

    github_id: int | None
    node_id: str
    login: str
    type: AccountType
    linked_user_github_id: int | None = None


@dataclass(frozen=True, slots=True)
class LinkedAccount:
    """The account a commit links to.

    It carries no type: GraphQL declares it ``User`` even for bots, and REST uses another login
    form, so a commit link never sets an account's type or login (plan 5.2).
    """

    github_id: int
    node_id: str
    login: str


@dataclass(frozen=True, slots=True)
class RepositoryLabel:
    """One label of a repository's label set (D8)."""

    node_id: str
    name: str
    color: str
    description: str | None
    is_default: bool
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    """A resolved repository, names in GitHub's spelling, with every field the resolve reads.

    ``resolved_at`` is GitHub's ``Date`` header of the resolve response.
    """

    github_id: int
    node_id: str
    owner: str
    name: str
    full_name: str
    is_private: bool
    is_archived: bool
    default_branch: str
    labels: tuple[RepositoryLabel, ...]
    labels_total: int
    is_security_policy_enabled: bool | None
    visibility: str
    description: str | None
    homepage_url: str | None
    created_at: datetime
    updated_at: datetime
    pushed_at: datetime | None
    archived_at: datetime | None
    stargazer_count: int
    fork_count: int
    disk_usage: int | None
    forking_allowed: bool
    allow_update_branch: bool
    web_commit_signoff_required: bool
    has_wiki_enabled: bool
    has_discussions_enabled: bool
    has_projects_enabled: bool
    has_vulnerability_alerts_enabled: bool
    uses_custom_open_graph_image: bool
    merge_commit_allowed: bool
    squash_merge_allowed: bool
    rebase_merge_allowed: bool
    delete_branch_on_merge: bool
    merge_commit_title: str
    merge_commit_message: str
    squash_merge_commit_title: str
    squash_merge_commit_message: str
    primary_language: str | None
    license_spdx_id: str | None
    plan_features_codeowners: bool
    plan_features_team_review_requests: bool
    plan_features_maximum_manual_review_requests: int
    resolved_at: datetime

    @property
    def label_names(self) -> frozenset[str]:
        return frozenset(label.name for label in self.labels)


@dataclass(frozen=True, slots=True)
class ClosingIssue:
    """An issue the index lists in a pull request's ``closingIssuesReferences``, which GitHub
    describes as "List of issues that may be closed by this pull request"; it can be in another
    repository."""

    repo_github_id: int
    repo_full_name: str
    number: int


@dataclass(frozen=True, slots=True)
class LinkedClosingIssue:
    """A hydrated pull request's closing link, with the issue's ``fullDatabaseId`` and whether
    GitHub also lists it under ``userLinkedOnly: true`` ("Return only manually linked
    Issues")."""

    repo_github_id: int
    repo_full_name: str
    number: int
    github_id: int
    user_linked: bool


def link_key(link: ClosingIssue | LinkedClosingIssue) -> tuple[int, int]:
    """A closing link as the database keys it: the repository's id and the issue number.

    The repository's name is not part of it; a rename between two pages changes only the name.
    """
    return (link.repo_github_id, link.number)


@dataclass(frozen=True, slots=True)
class IndexedPullRequest:
    """One pull request as the index reports it."""

    number: int
    github_id: int
    state: PullRequestState
    is_draft: bool
    author_association: str
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None
    closed_at: datetime | None
    # The first INDEX_CLOSING_LINKS distinct links; complete when len() == closing_issues_total.
    closing_issues: tuple[ClosingIssue, ...]
    closing_issues_total: int


@dataclass(frozen=True, slots=True)
class PullRequestIndex:
    """Every pull request of a repository, in creation order."""

    repository: str
    # GitHub's Date header of the first page minus INDEX_BOUNDARY_MARGIN (plan 5.3 step 2).
    started_at: datetime
    total_count: int
    entries: tuple[IndexedPullRequest, ...]
    scans: int


@dataclass(frozen=True, slots=True)
class Label:
    """A label applied to a pull request: its name and GitHub's label id (D8)."""

    name: str
    node_id: str


@dataclass(frozen=True, slots=True)
class Review:
    """A submitted review, never its body (D24).

    The reviewer or editor is None where GitHub returned no actor; GitHub does not say why
    (D28). ``inline_comments_total`` is GitHub's count of the review's inline comments (D5).
    """

    github_id: int
    node_id: str
    reviewer: Account | None
    state: ReviewState
    created_at: datetime
    submitted_at: datetime | None
    published_at: datetime | None
    updated_at: datetime
    last_edited_at: datetime | None
    editor: Account | None
    includes_created_edit: bool
    commit_oid: str | None
    author_association: str
    author_can_push_to_repository: bool
    inline_comments_total: int


@dataclass(frozen=True, slots=True)
class Comment:
    """A conversation comment: its ids, author, and times, never its text (D24).

    The author or editor is None where GitHub returned no actor; GitHub does not say why (D28).
    """

    github_id: int
    node_id: str
    author: Account | None
    created_at: datetime
    published_at: datetime | None
    updated_at: datetime
    last_edited_at: datetime | None
    editor: Account | None
    created_via_email: bool
    includes_created_edit: bool
    author_association: str


@dataclass(frozen=True, slots=True)
class CommitRecord:
    sha: str
    author: LinkedAccount | None
    author_name: str | None
    author_email: str | None
    authored_at: datetime


@dataclass(frozen=True, slots=True)
class FileChange:
    """One entry of the file list; the same path can appear twice, so rows key by position."""

    path: str
    change_type: str
    additions: int
    deletions: int


@dataclass(frozen=True, slots=True)
class RequestedTeam:
    """A team or an enterprise team a review is requested from; neither is an account.

    ``github_id`` is a ``Team``'s ``databaseId`` or an ``EnterpriseTeam``'s ``fullDatabaseId``,
    either nullable in GitHub's schema.
    """

    github_id: int | None
    node_id: str
    slug: str


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """One entry of a pull request's ``reviewRequests`` as GitHub lists it (D4).

    ``requested_type`` is GitHub's ``__typename`` of the requested reviewer, None when GitHub
    returns none. ``account`` is set for a ``User``, ``Bot``, or ``Mannequin``, ``team`` for a
    ``Team`` or an ``EnterpriseTeam``.
    """

    github_id: int | None
    node_id: str
    as_code_owner: bool
    requested_type: str | None
    account: Account | None
    team: RequestedTeam | None


@dataclass(frozen=True, slots=True)
class HeadRepository:
    """The repository GitHub reports a pull request's head branch in."""

    github_id: int | None
    full_name: str


@dataclass(frozen=True, slots=True)
class HeadRepositoryOwner:
    node_id: str
    login: str


@dataclass(frozen=True, slots=True)
class Milestone:
    """GitHub's reference to the milestone a pull request is in, not a stored milestone."""

    number: int
    title: str


@dataclass(frozen=True, slots=True)
class Stack:
    """The stack a pull request belongs to; ``number`` identifies it within its repository."""

    node_id: str
    number: int
    size: int
    base_ref_name: str


@dataclass(frozen=True, slots=True)
class StackEntry:
    """The pull request's entry in its stack: GitHub's position, 1 closest to the base branch."""

    node_id: str
    position: int


@dataclass(frozen=True, slots=True)
class ConnectionTotals:
    """Validated ``totalCount`` of each connection (plan 5.1 ``*_total`` columns)."""

    labels: int
    closing_issues: int
    user_linked_closing_issues: int
    reviews: int
    comments: int
    commits: int
    files: int
    review_requests: int
    assignees: int


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    """A fully hydrated pull request: every connection fetched and its count checked."""

    repository: str
    github_id: int
    node_id: str
    number: int
    state: PullRequestState
    is_draft: bool
    title: str
    body: str
    author: Account | None
    author_association: str
    created_at: datetime
    merged_at: datetime | None
    closed_at: datetime | None
    published_at: datetime | None
    last_edited_at: datetime | None
    editor: Account | None
    includes_created_edit: bool
    maintainer_can_modify: bool
    merged_by: Account | None
    base_ref: str
    base_ref_oid: str
    head_ref_name: str
    head_ref_oid: str
    head_repository: HeadRepository | None
    head_repository_owner: HeadRepositoryOwner | None
    merge_commit_oid: str | None
    is_cross_repository: bool
    milestone: Milestone | None
    review_decision: str | None
    # GitHub's merge state when the item was read; it changes without the PR's updatedAt.
    mergeable: str
    merge_state_status: str
    can_be_rebased: bool
    potential_merge_commit_oid: str | None
    stack: Stack | None
    stack_entry: StackEntry | None
    additions: int
    deletions: int
    changed_files: int
    total_comments_count: int | None
    labels: tuple[Label, ...]
    closing_issues: tuple[LinkedClosingIssue, ...]
    reviews: tuple[Review, ...]
    comments: tuple[Comment, ...]
    commits: tuple[CommitRecord, ...]
    commits_source: CommitSource
    files: tuple[FileChange, ...]
    review_requests: tuple[ReviewRequest, ...]
    assignees: tuple[Account, ...]
    totals: ConnectionTotals
    github_updated_at: datetime
    # GitHub's Date header of the item's alias response (plan 5.3 step 4).
    hydrated_at: datetime
    hydration_version: int

    def accounts(self) -> tuple[Account, ...]:
        """Every actor sighting, in order: author, merger, editor, each review's reviewer and
        editor, each comment's author and editor, requested accounts, assignees."""
        seen = [self.author, self.merged_by, self.editor]
        for review in self.reviews:
            seen.extend((review.reviewer, review.editor))
        for comment in self.comments:
            seen.extend((comment.author, comment.editor))
        seen.extend(request.account for request in self.review_requests)
        seen.extend(self.assignees)
        return tuple(account for account in seen if account is not None)

    def commit_links(self) -> tuple[LinkedAccount, ...]:
        """Accounts linked from commits; they never set type or login."""
        return tuple(commit.author for commit in self.commits if commit.author is not None)


@dataclass(frozen=True, slots=True)
class Hydrated:
    record: PullRequestRecord

    @property
    def number(self) -> int:
        return self.record.number


@dataclass(frozen=True, slots=True)
class Gone:
    """The number answered ``NOT_FOUND``; the next index removes it."""

    number: int


@dataclass(frozen=True, slots=True)
class Failed:
    """The item failed: on its solo retry, or at once when its own request had already failed
    alone after the transport's retries (plan 5.3 step 4)."""

    number: int
    error: ItemError


PullRequestOutcome = Hydrated | Gone | Failed


@dataclass(frozen=True, slots=True)
class RestRateLimit:
    """One pool REST ``/rate_limit`` reports (``core``, ``graphql``, ``search``, ...)."""

    resource: str
    limit: int
    used: int
    remaining: int
    reset_at: datetime


@dataclass(frozen=True, slots=True)
class RateLimits:
    """The GraphQL ``rateLimit`` reading and every REST pool, read one after the other.

    One token's GraphQL readings can come from two windows (plan section 7, "Rate-limit
    windows"), so the GraphQL reading and REST's ``graphql`` pool may disagree.
    """

    graphql_remaining: int
    graphql_reset_at: datetime
    rest: tuple[RestRateLimit, ...]


@dataclass(frozen=True, slots=True)
class UserProfile:
    """A ``User``'s public profile fields the database collects (D31)."""

    company: str | None
    location: str | None
    website_url: str | None


@dataclass(frozen=True, slots=True)
class OrganizationProfile:
    """An ``Organization``'s public profile fields the database collects (D31)."""

    description: str | None
    email: str | None
    location: str | None
    website_url: str | None
    twitter_username: str | None
    is_verified: bool
    archived_at: datetime | None
    has_sponsors_listing: bool
    pinned_items_remaining: int


@dataclass(frozen=True, slots=True)
class LookedUpAccount:
    """An account as a ``nodes(ids:)`` lookup reports it.

    ``name`` is None for a ``Bot``, which has none, and where GitHub returns none;
    ``claimant_github_id`` is a ``Mannequin``'s claimant; ``profile`` is set for a ``User`` and
    an ``Organization`` only (D31).
    """

    account: Account
    name: str | None
    created_at: datetime
    updated_at: datetime
    claimant_github_id: int | None
    profile: UserProfile | OrganizationProfile | None


@dataclass(frozen=True, slots=True)
class UserLookup:
    """One ``nodes(ids:)`` batch: the account behind each id, and the ids whose lookup failed.

    An account is None where GitHub answered ``NOT_FOUND`` for its id. Every requested id is in
    exactly one of the two mappings. ``looked_up_at`` is GitHub's ``Date`` header of the
    response, None when the request itself failed.
    """

    accounts: dict[str, LookedUpAccount | None]
    failed: dict[str, UserLookupFailed]
    looked_up_at: datetime | None
