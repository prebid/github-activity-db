"""Enumerate every pull request of a repository (redesign plan 5.3 step 2)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import ValidationError

from .convert import closing_issue, utc, utc_or_none
from .documents import PULL_REQUEST_INDEX
from .errors import (
    IndexCountMismatch,
    IndexOrderViolation,
    RepositoryRequestFailed,
    RepositoryShapeError,
    RequestFailedError,
)
from .limits import INDEX_BOUNDARY_MARGIN
from .models import IndexedPullRequestNode, PullRequestIndexPage
from .records import (
    ClosingIssue,
    IndexedPullRequest,
    PullRequestIndex,
    RepositoryRecord,
    link_key,
)
from .transport import Transport

_CONNECTION = "pullRequests"


async def index_pull_requests(
    transport: Transport, repository: RepositoryRecord
) -> PullRequestIndex:
    """Every pull request in creation order, its count equal to ``totalCount``.

    Creation order is stable, so an update during the scan cannot make pagination skip an item;
    a transfer or deletion during the scan breaks the count, so a mismatch rescans once.

    Raises:
        IndexCountMismatch: the count still differs on the second scan.
        RepositoryError: a request or response for the repository failed.
    """
    entries: tuple[IndexedPullRequest, ...] = ()
    total = 0
    for scan in (1, 2):
        entries, total, first_page_date = await _scan(transport, repository)
        if len(entries) == total:
            return PullRequestIndex(
                repository=repository.full_name,
                started_at=first_page_date - INDEX_BOUNDARY_MARGIN,
                total_count=total,
                entries=entries,
                scans=scan,
            )
    raise IndexCountMismatch(repository.full_name, _CONNECTION, len(entries), total)


async def _scan(
    transport: Transport, repository: RepositoryRecord
) -> tuple[tuple[IndexedPullRequest, ...], int, datetime]:
    """One pass over the connection: entries, the last page's ``totalCount``, first page date."""
    full_name = repository.full_name
    entries: list[IndexedPullRequest] = []
    seen: set[int] = set()
    cursor: str | None = None
    first_page_date: datetime | None = None
    while True:
        variables = {"owner": repository.owner, "name": repository.name, "after": cursor}
        try:
            result = await transport.graphql(PULL_REQUEST_INDEX, variables, splittable=False)
        except RequestFailedError as e:
            raise RepositoryRequestFailed(full_name, e) from e
        first_page_date = first_page_date or result.date
        if result.errors:
            detail = "; ".join(f"{error.type}: {error.message}" for error in result.errors)
            raise RepositoryShapeError(full_name, f"index errors: {detail}")
        repository_data = result.data.get("repository")
        try:
            page = PullRequestIndexPage.model_validate(
                repository_data.get("pullRequests") if isinstance(repository_data, dict) else None
            )
        except ValidationError as e:
            raise RepositoryShapeError(full_name, f"unexpected index page: {e}") from e
        if page.nodes is None:
            raise RepositoryShapeError(full_name, "index page without nodes")
        for node in page.nodes:
            if node is None:
                raise RepositoryShapeError(full_name, "null index entry")
            entry = _entry(full_name, node)
            if entry.number in seen:
                raise IndexOrderViolation(full_name, _CONNECTION, entry.number, "listed twice")
            if entries and entry.created_at < entries[-1].created_at:
                raise IndexOrderViolation(
                    full_name, _CONNECTION, entry.number, "created before the previous entry"
                )
            seen.add(entry.number)
            entries.append(entry)
        if not page.page_info.has_next_page:
            return tuple(entries), page.total_count, first_page_date
        next_cursor = page.page_info.end_cursor
        if next_cursor is None or next_cursor == cursor or not page.nodes:
            raise RepositoryShapeError(
                full_name,
                f"index page has a next page but no progress ({len(page.nodes)} entries, "
                f"cursor {next_cursor!r})",
            )
        cursor = next_cursor


def _distinct_links(links: Iterable[ClosingIssue]) -> tuple[ClosingIssue, ...]:
    """Each link once by its database key, first sighting kept.

    A duplicate leaves the list shorter than ``totalCount``, so the entry reads as incomplete
    and the pull request is hydrated, where the duplicate fails that item (plan 5.3 step 3).
    """
    seen: dict[tuple[int, int], ClosingIssue] = {}
    for link in links:
        seen.setdefault(link_key(link), link)
    return tuple(seen.values())


def _entry(full_name: str, node: IndexedPullRequestNode) -> IndexedPullRequest:
    links = node.closing_issues_references
    if links is None or links.nodes is None or any(link is None for link in links.nodes):
        raise RepositoryShapeError(full_name, f"#{node.number}: null closing-issue links")
    return IndexedPullRequest(
        number=node.number,
        github_id=node.full_database_id,
        state=node.state,
        is_draft=node.is_draft,
        author_association=node.author_association,
        created_at=utc(node.created_at),
        updated_at=utc(node.updated_at),
        merged_at=utc_or_none(node.merged_at),
        closed_at=utc_or_none(node.closed_at),
        closing_issues=_distinct_links(
            closing_issue(link) for link in links.nodes if link is not None
        ),
        closing_issues_total=links.total_count,
    )
