"""Snapshots of the stored rows the planner reads (redesign plan 5.3 step 3).

Every read is one statement whose rows are all fetched before the next one starts, never a
server-side cursor (``stream``, ``stream_scalars``, ``stream_results``, ``yield_per``): in
rollback-journal mode a read left half-consumed holds a lock that blocks a running sync's commit
until the busy timeout (gap 77). Give it a reader connection (``sync.connection.open_reader``):
there the reads are separate statements and none holds a lock between them. Under a run's claim
the run is the only writer the code allows, so its reads see one state; a ``--dry-run`` beside a
running sync can see that sync's commits land between them. On the writer connection a bare read
would take the write lock, so the writer never reads a snapshot.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from github_activity_db.db.models import (
    PullRequest,
    PullRequestClosingIssue,
    PullRequestLabel,
    Repository,
    RepositoryLabel,
    SyncFailure,
    SyncPending,
)

# An item as sync_failures and sync_pending key it within a repository: its kind and number.
ItemKey = tuple[str, int]
# A label as GitHub identifies it: its node id and its name (D8).
LabelKey = tuple[str, str]
# A closing link as the database keys it: the issue's repository id and number.
LinkKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class StoredPullRequest:
    """The columns of a stored pull request the planner compares with the index."""

    row_id: int
    github_id: int
    number: int
    state: str
    is_draft: bool
    author_association: str
    github_updated_at: datetime
    hydrated_at: datetime
    hydration_version: int
    closing_issues_total: int
    labels: frozenset[LabelKey]
    closing_links: frozenset[LinkKey]


@dataclass(frozen=True, slots=True)
class StoredRepository:
    """A stored repository with its items and its ``sync_failures`` and ``sync_pending`` rows;
    its default branch and labels are what a dry run reports step 1 would change."""

    row_id: int
    github_id: int
    full_name: str
    is_tracked: bool
    default_branch: str
    labels: frozenset[LabelKey]
    pull_requests: tuple[StoredPullRequest, ...]
    failures: frozenset[ItemKey]
    pending: frozenset[ItemKey]

    @property
    def item_rows(self) -> int:
        """Stored PR and issue rows, which step 7's guard counts; issues arrive in Step 4."""
        return len(self.pull_requests)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Every stored repository, in row order."""

    repositories: tuple[StoredRepository, ...]

    def repository(self, github_id: int) -> StoredRepository | None:
        """The stored repository with this GitHub id, None when none is stored."""
        for repository in self.repositories:
            if repository.github_id == github_id:
                return repository
        return None


async def read_snapshot(connection: AsyncConnection) -> Snapshot:
    """Read every stored repository with its labels, and every pull request, label, closing link,
    failure, and pending row.

    Writes nothing. Each ``execute`` returns its rows already fetched (``Result.all()`` on a
    buffered result), so no statement is left open when this returns or between reads.
    """
    labels: defaultdict[int, set[LabelKey]] = defaultdict(set)
    for pr_id, node_id, name in (
        await connection.execute(
            select(PullRequestLabel.pr_id, PullRequestLabel.label_node_id, PullRequestLabel.name)
        )
    ).all():
        labels[pr_id].add((node_id, name))

    links: defaultdict[int, set[LinkKey]] = defaultdict(set)
    for pr_id, repo_github_id, number in (
        await connection.execute(
            select(
                PullRequestClosingIssue.pr_id,
                PullRequestClosingIssue.issue_repo_github_id,
                PullRequestClosingIssue.issue_number,
            )
        )
    ).all():
        links[pr_id].add((repo_github_id, number))

    pull_requests: defaultdict[int, list[StoredPullRequest]] = defaultdict(list)
    for row in (
        await connection.execute(
            select(
                PullRequest.repository_id,
                PullRequest.id,
                PullRequest.github_id,
                PullRequest.number,
                PullRequest.state,
                PullRequest.is_draft,
                PullRequest.author_association,
                PullRequest.github_updated_at,
                PullRequest.hydrated_at,
                PullRequest.hydration_version,
                PullRequest.closing_issues_total,
            ).order_by(PullRequest.repository_id, PullRequest.number)
        )
    ).all():
        pull_requests[row.repository_id].append(
            StoredPullRequest(
                row_id=row.id,
                github_id=row.github_id,
                number=row.number,
                state=row.state,
                is_draft=row.is_draft,
                author_association=row.author_association,
                github_updated_at=row.github_updated_at,
                hydrated_at=row.hydrated_at,
                hydration_version=row.hydration_version,
                closing_issues_total=row.closing_issues_total,
                labels=frozenset(labels[row.id]),
                closing_links=frozenset(links[row.id]),
            )
        )

    failures: defaultdict[int, set[ItemKey]] = defaultdict(set)
    for repository_id, kind, number in (
        await connection.execute(
            select(SyncFailure.repository_id, SyncFailure.item_kind, SyncFailure.number)
        )
    ).all():
        failures[repository_id].add((kind, number))

    pending: defaultdict[int, set[ItemKey]] = defaultdict(set)
    for repository_id, kind, number in (
        await connection.execute(
            select(SyncPending.repository_id, SyncPending.item_kind, SyncPending.number)
        )
    ).all():
        pending[repository_id].add((kind, number))

    repository_labels: defaultdict[int, set[LabelKey]] = defaultdict(set)
    for repository_id, node_id, name in (
        await connection.execute(
            select(RepositoryLabel.repository_id, RepositoryLabel.node_id, RepositoryLabel.name)
        )
    ).all():
        repository_labels[repository_id].add((node_id, name))

    repositories = (
        await connection.execute(
            select(
                Repository.id,
                Repository.github_id,
                Repository.full_name,
                Repository.is_tracked,
                Repository.default_branch,
            ).order_by(Repository.id)
        )
    ).all()
    return Snapshot(
        repositories=tuple(
            StoredRepository(
                row_id=row.id,
                github_id=row.github_id,
                full_name=row.full_name,
                is_tracked=row.is_tracked,
                default_branch=row.default_branch,
                labels=frozenset(repository_labels[row.id]),
                pull_requests=tuple(pull_requests[row.id]),
                failures=frozenset(failures[row.id]),
                pending=frozenset(pending[row.id]),
            )
            for row in repositories
        )
    )
