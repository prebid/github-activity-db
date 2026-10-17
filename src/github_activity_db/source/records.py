"""Records the source layer hands to the sync engine (redesign plan 5.7).

These are the only types that cross from ``source`` into ``sync``: the response models stay
private. Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .errors import ItemError, UserLookupFailed

AccountType = Literal["User", "Bot", "Organization", "Mannequin"]
PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
ReviewState = Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"]
CommitSource = Literal["graphql", "compare"]


@dataclass(frozen=True, slots=True)
class Account:
    """A GitHub account as an actor sighting or a ``nodes(ids:)`` lookup reports it.

    Those two are the only sources of an account's type and login (plan 5.2).
    """

    github_id: int
    node_id: str
    login: str
    type: AccountType


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
class RepositoryRecord:
    """A resolved repository, names in GitHub's spelling."""

    github_id: int
    node_id: str
    owner: str
    name: str
    full_name: str
    is_private: bool
    is_archived: bool
    default_branch: str
    label_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class ClosingIssue:
    """An issue a pull request closes, possibly in another repository."""

    repo_github_id: int
    repo_full_name: str
    number: int


def link_key(link: ClosingIssue) -> tuple[int, int]:
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
class Review:
    github_id: int
    reviewer: Account | None
    state: ReviewState


@dataclass(frozen=True, slots=True)
class Comment:
    """A conversation comment: its id and author, never its text (D24).

    The author is None for an account GitHub reports as deleted.
    """

    github_id: int
    author: Account | None


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
class ConnectionTotals:
    """Validated ``totalCount`` of each connection (plan 5.1 ``*_total`` columns)."""

    labels: int
    closing_issues: int
    reviews: int
    comments: int
    commits: int
    files: int


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
    merged_by: Account | None
    base_ref: str
    is_cross_repository: bool
    additions: int
    deletions: int
    changed_files: int
    labels: tuple[str, ...]
    closing_issues: tuple[ClosingIssue, ...]
    reviews: tuple[Review, ...]
    comments: tuple[Comment, ...]
    commits: tuple[CommitRecord, ...]
    commits_source: CommitSource
    files: tuple[FileChange, ...]
    totals: ConnectionTotals
    github_updated_at: datetime
    # GitHub's Date header of the item's alias response (plan 5.3 step 4).
    hydrated_at: datetime
    hydration_version: int

    def accounts(self) -> tuple[Account, ...]:
        """Every actor sighting, in order: author, merger, reviewers, comment authors."""
        seen = [self.author, self.merged_by]
        seen.extend(review.reviewer for review in self.reviews)
        seen.extend(comment.author for comment in self.comments)
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
class UserLookup:
    """One ``nodes(ids:)`` batch: the account behind each id, and the ids whose lookup failed.

    An account is None where GitHub has no such account. Every requested id is in exactly one of
    the two mappings.
    """

    accounts: dict[str, Account | None]
    failed: dict[str, UserLookupFailed]
