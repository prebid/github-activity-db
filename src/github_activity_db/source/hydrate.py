"""Hydrate pull requests: every connection fetched in full and checked (redesign plan 5.3 steps
4 and 5).

A batch of up to ``PR_BATCH_SIZE`` pull requests is one GraphQL request. Connections with more
than one page continue through ``node(id:)``, continuations from several items sharing a request.
Above ``GRAPHQL_COMMIT_CAP`` commits the commit list comes from the REST compare endpoint. A
request that fails is split in half down to single items. An item that fails is retried once
alone and then reported as ``Failed``, unless the failure was its own request, already sent alone
and retried by the transport (``ItemRequestFailed``), which a retry would only repeat. Nothing
here raises for a single item.

Outcomes are delivered per batch, so a deadline or a fatal error loses at most the batch in
progress.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Hashable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import ceil
from typing import Any

from pydantic import ValidationError

from .convert import account, union_account, utc, utc_or_none
from .documents import (
    COMMITS,
    PULL_REQUEST_CONNECTIONS,
    PULL_REQUEST_HYDRATION_VERSION,
    Connection,
    overflow_alias,
    overflow_continuation,
    pull_request_alias,
    pull_request_batch,
)
from .errors import (
    AliasError,
    CloseTimeMissing,
    CompareCountMismatch,
    CompareRefused,
    ConnectionCountMismatch,
    DuplicateEntry,
    IdentityMismatch,
    ItemChangedDuringRead,
    ItemError,
    ItemRequestFailed,
    MergeStateMismatch,
    NullEntry,
    PagingStalled,
    RepositoryNotFound,
    RepositoryShapeError,
    RequestFailedError,
    ResponseShapeError,
    TotalCountChanged,
    UnattributedErrors,
    UserLinkedIssueNotListed,
)
from .limits import (
    COMPARE_PAGE_SIZE,
    GRAPHQL_COMMIT_CAP,
    OVERFLOW_ALIASES_PER_REQUEST,
    PR_BATCH_SIZE,
)
from .models import (
    AssignedActorNode,
    CommentNode,
    CommitNode,
    CompareCommit,
    ComparePage,
    FileNode,
    LabelNode,
    LinkedIssueNode,
    OverflowHead,
    Page,
    PullRequestNode,
    RequestedAccount,
    RequestedEnterpriseTeam,
    RequestedTeamNode,
    ReviewNode,
    ReviewRequestNode,
    UserLinkedIssueNode,
)
from .records import (
    Comment,
    CommitRecord,
    CommitSource,
    ConnectionTotals,
    Failed,
    FileChange,
    Gone,
    HeadRepository,
    HeadRepositoryOwner,
    Hydrated,
    Label,
    LinkedAccount,
    LinkedClosingIssue,
    Milestone,
    PullRequestOutcome,
    PullRequestRecord,
    RepositoryRecord,
    RequestedTeam,
    Review,
    ReviewRequest,
    Stack,
    StackEntry,
    link_key,
)
from .transport import GraphQLResult, RestRefused, Transport


@dataclass
class _Collected[NodeT]:
    """One connection of one item: entries so far and the cursor of the next page."""

    total: int
    nodes: list[NodeT]
    cursor: str | None


@dataclass
class _Item:
    """A pull request between its first response and its record."""

    number: int
    node: PullRequestNode
    date: datetime
    labels: _Collected[LabelNode]
    closing_issues: _Collected[LinkedIssueNode]
    user_linked_closing_issues: _Collected[UserLinkedIssueNode]
    reviews: _Collected[ReviewNode]
    comments: _Collected[CommentNode]
    commits: _Collected[CommitNode]
    files: _Collected[FileNode]
    review_requests: _Collected[ReviewRequestNode]
    assigned_actors: _Collected[AssignedActorNode]
    compare_commits: list[CommitRecord] | None = None
    error: ItemError | None = field(default=None)

    def pending(self) -> list[Connection]:
        """Connections with another page to fetch through ``node(id:)``."""
        pending = [
            connection
            for connection in PULL_REQUEST_CONNECTIONS
            if self.collected(connection.key).cursor is not None
        ]
        if self.uses_compare():
            pending = [connection for connection in pending if connection is not COMMITS]
        return pending

    def uses_compare(self) -> bool:
        return _uses_compare(self.commits.total)

    def collected(self, key: str) -> _Collected[Any]:
        """The connection a response carries under ``key`` (``Connection.key``)."""
        match key:
            case "labels":
                return self.labels
            case "closingIssuesReferences":
                return self.closing_issues
            case "userLinkedClosingIssues":
                return self.user_linked_closing_issues
            case "reviews":
                return self.reviews
            case "comments":
                return self.comments
            case "commits":
                return self.commits
            case "files":
                return self.files
            case "reviewRequests":
                return self.review_requests
            case "assignedActors":
                return self.assigned_actors
        raise KeyError(key)


_PAGE_MODELS: dict[str, type[Page[Any]]] = {
    "labels": Page[LabelNode],
    "closingIssuesReferences": Page[LinkedIssueNode],
    "userLinkedClosingIssues": Page[UserLinkedIssueNode],
    "reviews": Page[ReviewNode],
    "comments": Page[CommentNode],
    "commits": Page[CommitNode],
    "files": Page[FileNode],
    "reviewRequests": Page[ReviewRequestNode],
    "assignedActors": Page[AssignedActorNode],
}


class PullRequestHydrator:
    """Hydrates pull requests of one resolved repository."""

    def __init__(self, transport: Transport, repository: RepositoryRecord) -> None:
        self._transport = transport
        self._repository = repository

    async def batches(self, numbers: Sequence[int]) -> AsyncIterator[list[PullRequestOutcome]]:
        """Outcomes batch by batch: one per distinct number, in the order first given.

        Before a batch is delivered, its failed items are retried alone, except an item whose own
        request already failed alone after the transport's retries (``ItemRequestFailed``).
        """
        distinct = list(dict.fromkeys(numbers))
        for start in range(0, len(distinct), PR_BATCH_SIZE):
            batch = distinct[start : start + PR_BATCH_SIZE]
            first = await self._group(batch)
            outcomes: list[PullRequestOutcome] = []
            for number in batch:
                outcome = first[number]
                if isinstance(outcome, ItemRequestFailed) and not isinstance(
                    outcome.cause, UnattributedErrors
                ):
                    outcome = Failed(number, outcome)
                elif isinstance(outcome, ItemError):
                    retried = (await self._group([number]))[number]
                    outcome = Failed(number, retried) if isinstance(retried, ItemError) else retried
                outcomes.append(outcome)
            yield outcomes

    # ----------------------------------------------------------------------------------------
    # One group: first pages, overflow, compare, records
    # ----------------------------------------------------------------------------------------
    async def _group(self, numbers: list[int]) -> dict[int, PullRequestOutcome | ItemError]:
        try:
            first = await self._first_pages(numbers)
        except RequestFailedError as e:
            if len(numbers) == 1:
                return {numbers[0]: ItemRequestFailed(numbers[0], e)}
            middle = len(numbers) // 2
            return {**await self._group(numbers[:middle]), **await self._group(numbers[middle:])}
        items = [item for item in first.values() if isinstance(item, _Item)]
        await self._overflow(
            [(item, connection) for item in items for connection in item.pending()]
        )
        for item in items:
            if item.error is None and item.uses_compare():
                await self._compare(item)
        results: dict[int, PullRequestOutcome | ItemError] = {}
        for number, item_or_outcome in first.items():
            if not isinstance(item_or_outcome, _Item):
                results[number] = item_or_outcome
                continue
            item = item_or_outcome
            if item.error is not None:
                results[number] = item.error
                continue
            try:
                results[number] = Hydrated(self._record(item))
            except ItemError as e:
                results[number] = e
        return results

    async def _first_pages(self, numbers: list[int]) -> dict[int, _Item | Gone | ItemError]:
        variables: dict[str, object] = {
            "owner": self._repository.owner,
            "name": self._repository.name,
        }
        variables.update({f"n{i}": number for i, number in enumerate(numbers)})
        result = await self._transport.graphql(
            pull_request_batch(len(numbers)), variables, splittable=len(numbers) > 1
        )
        container = result.data.get("repository")
        if not isinstance(container, dict):
            errors = result.errors_under("repository")
            if any(error.type == "NOT_FOUND" and error.path == ("repository",) for error in errors):
                raise RepositoryNotFound(self._repository.full_name)
            detail = "; ".join(f"{error.type}: {error.message}" for error in errors) or "no error"
            raise RepositoryShapeError(
                self._repository.full_name, f"batch repository is null: {detail}"
            )
        aliases = [pull_request_alias(i) for i in range(len(numbers))]
        _check_attributed(result, [("repository", alias) for alias in aliases])
        items: dict[int, _Item | Gone | ItemError] = {}
        for i, number in enumerate(numbers):
            alias = pull_request_alias(i)
            items[number] = self._first_page(number, result, container, alias)
        return items

    def _first_page(
        self, number: int, result: GraphQLResult, container: dict[str, Any], alias: str
    ) -> _Item | Gone | ItemError:
        errors = result.errors_under("repository", alias)
        raw = container.get(alias)
        if raw is None:
            # Gone only when every error under the alias is NOT_FOUND on the pull request itself;
            # a NOT_FOUND deeper down, or any other error alongside, is an error about the item.
            if errors and all(
                e.type == "NOT_FOUND" and e.path == ("repository", alias) for e in errors
            ):
                return Gone(number)
            first = errors[0] if errors else None
            return AliasError(
                number, first.type if first else None, first.message if first else "null alias"
            )
        if errors:
            return AliasError(number, errors[0].type, errors[0].message)
        try:
            node = PullRequestNode.model_validate(raw)
        except ValidationError as e:
            return ResponseShapeError(number, str(e))
        if node.number != number:
            return IdentityMismatch(number, "number", number, node.number)
        if (node.state == "MERGED") != (node.merged_at is not None):
            return MergeStateMismatch(number, node.state, node.merged_at is not None)
        if node.state != "OPEN" and node.closed_at is None:
            return CloseTimeMissing(number, node.state)
        try:
            return _Item(
                number=number,
                node=node,
                date=result.date,
                labels=_first_collected(number, "labels", node.labels),
                closing_issues=_first_collected(
                    number, "closingIssuesReferences", node.closing_issues_references
                ),
                user_linked_closing_issues=_first_collected(
                    number, "userLinkedClosingIssues", node.user_linked_closing_issues
                ),
                reviews=_first_collected(number, "reviews", node.reviews),
                comments=_first_collected(number, "comments", node.comments),
                commits=_first_collected(number, "commits", node.commits),
                files=_first_collected(number, "files", node.files),
                review_requests=_first_collected(number, "reviewRequests", node.review_requests),
                assigned_actors=_first_collected(number, "assignedActors", node.assigned_actors),
            )
        except ItemError as e:
            return e

    # ----------------------------------------------------------------------------------------
    # Overflow
    # ----------------------------------------------------------------------------------------
    async def _overflow(self, pending: list[tuple[_Item, Connection]]) -> None:
        """Fetch further pages until every pending connection is complete or its item failed."""
        while pending:
            for start in range(0, len(pending), OVERFLOW_ALIASES_PER_REQUEST):
                await self._continue(pending[start : start + OVERFLOW_ALIASES_PER_REQUEST])
            pending = [
                (item, connection)
                for item, connection in pending
                if item.error is None and item.collected(connection.key).cursor is not None
            ]

    async def _continue(self, group: list[tuple[_Item, Connection]]) -> None:
        # An item that failed earlier in this round needs no more pages.
        group = [(item, connection) for item, connection in group if item.error is None]
        if not group:
            return
        connections = [connection for _, connection in group]
        variables: dict[str, object] = {}
        for i, (item, connection) in enumerate(group):
            variables[f"id{i}"] = item.node.id
            variables[f"after{i}"] = item.collected(connection.key).cursor
        try:
            result = await self._transport.graphql(
                overflow_continuation(connections), variables, splittable=len(group) > 1
            )
            _check_attributed(result, [(overflow_alias(i, c),) for i, (_, c) in enumerate(group)])
        except RequestFailedError as e:
            if len(group) == 1:
                item = group[0][0]
                item.error = ItemRequestFailed(item.number, e)
                return
            middle = len(group) // 2
            await self._continue(group[:middle])
            await self._continue(group[middle:])
            return
        for i, (item, connection) in enumerate(group):
            if item.error is None:
                alias = overflow_alias(i, connection)
                item.error = _apply_continuation(item, connection, result, alias)

    # ----------------------------------------------------------------------------------------
    # Compare
    # ----------------------------------------------------------------------------------------
    async def _compare(self, item: _Item) -> None:
        """The full commit list from REST compare, its counts checked against GraphQL."""
        node = item.node
        path = (
            f"/repos/{self._repository.owner}/{self._repository.name}"
            f"/compare/{node.base_ref_oid}...{node.head_ref_oid}"
        )
        commits: list[CommitRecord] = []
        page_number = 1
        pages = 1
        expected = 0
        while page_number <= pages:
            params = {"per_page": COMPARE_PAGE_SIZE, "page": page_number}
            try:
                response = await self._transport.rest_get(path, params)
            except RequestFailedError as e:
                item.error = ItemRequestFailed(item.number, e)
                return
            if isinstance(response, RestRefused):
                item.error = CompareRefused(item.number, response.status)
                return
            try:
                page = ComparePage.model_validate(response.body)
            except ValidationError as e:
                item.error = ResponseShapeError(item.number, f"compare page: {e}")
                return
            if page_number == 1:
                expected = page.total_commits
                if expected != item.commits.total:
                    item.error = CompareCountMismatch(
                        item.number,
                        "total_commits vs commits.totalCount",
                        item.commits.total,
                        expected,
                    )
                    return
                pages = max(1, ceil(expected / COMPARE_PAGE_SIZE))
            elif page.total_commits != expected:
                item.error = CompareCountMismatch(
                    item.number, "total_commits between pages", expected, page.total_commits
                )
                return
            try:
                commits.extend(_compare_commit(item.number, commit) for commit in page.commits)
            except ItemError as e:
                item.error = e
                return
            page_number += 1
        if len(commits) != expected:
            item.error = CompareCountMismatch(
                item.number, "fetched commits", expected, len(commits)
            )
            return
        item.compare_commits = commits

    # ----------------------------------------------------------------------------------------
    # Record
    # ----------------------------------------------------------------------------------------
    def _record(self, item: _Item) -> PullRequestRecord:
        number = item.number
        node = item.node
        for connection in PULL_REQUEST_CONNECTIONS:
            if connection is COMMITS and item.compare_commits is not None:
                continue  # checked against compare's total_commits instead
            _check_complete(number, connection.key, item.collected(connection.key))
        commit_source: CommitSource
        if item.compare_commits is not None:
            commits = item.compare_commits
            commit_source = "compare"
        else:
            commits = [_graphql_commit(commit) for commit in item.commits.nodes]
            commit_source = "graphql"

        reviews = tuple(_review(review) for review in item.reviews.nodes)
        comments = tuple(_comment(comment) for comment in item.comments.nodes)
        labels = tuple(Label(name=label.name, node_id=label.id) for label in item.labels.nodes)
        user_linked = [
            (link.repository.database_id, link.number)
            for link in item.user_linked_closing_issues.nodes
        ]
        closing_issues = tuple(
            LinkedClosingIssue(
                repo_github_id=link.repository.database_id,
                repo_full_name=link.repository.name_with_owner,
                number=link.number,
                github_id=link.full_database_id,
                user_linked=(link.repository.database_id, link.number) in user_linked,
            )
            for link in item.closing_issues.nodes
        )
        review_requests = tuple(_review_request(request) for request in item.review_requests.nodes)
        assignees = tuple(union_account(actor) for actor in item.assigned_actors.nodes)
        # The database keys these entries, so a duplicate standing in for a missing entry would
        # pass the count check and drop a row. Review requests are stored by position, so theirs
        # is compared by the request's own node id.
        _check_distinct(number, "labels", [label.name for label in labels])
        _check_distinct(
            number, "closingIssuesReferences", [link_key(link) for link in closing_issues]
        )
        _check_distinct(number, "userLinkedClosingIssues", user_linked)
        _check_distinct(number, "reviews", [review.github_id for review in reviews])
        _check_distinct(number, "comments", [comment.github_id for comment in comments])
        _check_distinct(number, "commits", [commit.sha for commit in commits])
        _check_distinct(number, "assignedActors", [assignee.node_id for assignee in assignees])
        _check_distinct(number, "reviewRequests", [request.node_id for request in review_requests])
        listed = {link_key(link) for link in closing_issues}
        for key in user_linked:
            if key not in listed:
                raise UserLinkedIssueNotListed(number, key)
        return PullRequestRecord(
            repository=self._repository.full_name,
            github_id=node.full_database_id,
            node_id=node.id,
            number=node.number,
            state=node.state,
            is_draft=node.is_draft,
            title=node.title,
            body=node.body,
            author=account(node.author),
            author_association=node.author_association,
            created_at=utc(node.created_at),
            merged_at=utc_or_none(node.merged_at),
            closed_at=utc_or_none(node.closed_at),
            published_at=utc_or_none(node.published_at),
            last_edited_at=utc_or_none(node.last_edited_at),
            editor=account(node.editor),
            includes_created_edit=node.includes_created_edit,
            maintainer_can_modify=node.maintainer_can_modify,
            merged_by=account(node.merged_by),
            base_ref=node.base_ref_name,
            base_ref_oid=node.base_ref_oid,
            head_ref_name=node.head_ref_name,
            head_ref_oid=node.head_ref_oid,
            head_repository=(
                HeadRepository(
                    github_id=node.head_repository.database_id,
                    full_name=node.head_repository.name_with_owner,
                )
                if node.head_repository is not None
                else None
            ),
            head_repository_owner=(
                HeadRepositoryOwner(
                    node_id=node.head_repository_owner.id, login=node.head_repository_owner.login
                )
                if node.head_repository_owner is not None
                else None
            ),
            merge_commit_oid=node.merge_commit.oid if node.merge_commit is not None else None,
            is_cross_repository=node.is_cross_repository,
            milestone=(
                Milestone(number=node.milestone.number, title=node.milestone.title)
                if node.milestone is not None
                else None
            ),
            review_decision=node.review_decision,
            mergeable=node.mergeable,
            merge_state_status=node.merge_state_status,
            can_be_rebased=node.can_be_rebased,
            potential_merge_commit_oid=(
                node.potential_merge_commit.oid if node.potential_merge_commit is not None else None
            ),
            stack=(
                Stack(
                    node_id=node.stack.id,
                    number=node.stack.number,
                    size=node.stack.size,
                    base_ref_name=node.stack.base_ref_name,
                )
                if node.stack is not None
                else None
            ),
            stack_entry=(
                StackEntry(node_id=node.stack_entry.id, position=node.stack_entry.position)
                if node.stack_entry is not None
                else None
            ),
            additions=node.additions,
            deletions=node.deletions,
            changed_files=node.changed_files,
            total_comments_count=node.total_comments_count,
            labels=labels,
            closing_issues=closing_issues,
            reviews=reviews,
            comments=comments,
            commits=tuple(commits),
            commits_source=commit_source,
            files=tuple(
                FileChange(
                    path=change.path,
                    change_type=change.change_type,
                    additions=change.additions,
                    deletions=change.deletions,
                )
                for change in item.files.nodes
            ),
            review_requests=review_requests,
            assignees=assignees,
            totals=ConnectionTotals(
                labels=item.labels.total,
                closing_issues=item.closing_issues.total,
                user_linked_closing_issues=item.user_linked_closing_issues.total,
                reviews=item.reviews.total,
                comments=item.comments.total,
                commits=item.commits.total,
                files=item.files.total,
                review_requests=item.review_requests.total,
                assignees=item.assigned_actors.total,
            ),
            github_updated_at=utc(node.updated_at),
            hydrated_at=item.date,
            hydration_version=PULL_REQUEST_HYDRATION_VERSION,
        )


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------
def _check_attributed(result: GraphQLResult, prefixes: Sequence[tuple[str, ...]]) -> None:
    """Raise when GitHub reported an error outside every item of the request."""
    stray = [
        error
        for error in result.errors
        if not any(error.path[: len(prefix)] == prefix for prefix in prefixes)
    ]
    if stray:
        detail = "; ".join(
            f"{error.type} at {list(error.path)}: {error.message}" for error in stray
        )
        raise UnattributedErrors(f"errors outside every item: {detail}")


def _uses_compare(total_commits: int) -> bool:
    """GraphQL stops at 250 commits; above that the list comes from compare."""
    return total_commits > GRAPHQL_COMMIT_CAP


def _first_collected[NodeT](number: int, name: str, page: Page[NodeT] | None) -> _Collected[NodeT]:
    """The first page of a connection; a null connection or entry fails the item."""
    if page is None or page.nodes is None or any(node is None for node in page.nodes):
        raise NullEntry(number, name)
    cursor = page.page_info.end_cursor
    if page.page_info.has_next_page and (cursor is None or not page.nodes):
        raise PagingStalled(number, name, cursor, len(page.nodes))
    return _Collected(
        total=page.total_count,
        nodes=[node for node in page.nodes if node is not None],
        cursor=cursor if page.page_info.has_next_page else None,
    )


def _apply_continuation(
    item: _Item, connection: Connection, result: GraphQLResult, alias: str
) -> ItemError | None:
    """Append one continuation page to ``item``; an error means the item failed."""
    number = item.number
    errors = result.errors_under(alias)
    raw = result.data.get(alias)
    if errors or not isinstance(raw, dict):
        first = errors[0] if errors else None
        return AliasError(
            number, first.type if first else None, first.message if first else "null continuation"
        )
    raw = dict(raw)
    raw_page = raw.pop(connection.key, None)
    try:
        head = OverflowHead.model_validate(raw)
        page = _PAGE_MODELS[connection.key].model_validate(raw_page)
    except ValidationError as e:
        return ResponseShapeError(number, f"{connection.key} continuation: {e}")
    if head.head_ref_oid != item.node.head_ref_oid:
        return ItemChangedDuringRead(
            number, "headRefOid", item.node.head_ref_oid, head.head_ref_oid
        )
    if head.updated_at != item.node.updated_at:
        return ItemChangedDuringRead(
            number, "updatedAt", item.node.updated_at.isoformat(), head.updated_at.isoformat()
        )
    collected = item.collected(connection.key)
    if page.total_count != collected.total:
        return TotalCountChanged(number, connection.key, collected.total, page.total_count)
    if page.nodes is None or any(node is None for node in page.nodes):
        return NullEntry(number, connection.key)
    collected.nodes.extend(node for node in page.nodes if node is not None)
    if len(collected.nodes) > collected.total:
        return ConnectionCountMismatch(
            number, connection.key, len(collected.nodes), collected.total
        )
    if page.page_info.has_next_page:
        cursor = page.page_info.end_cursor
        if cursor is None or cursor == collected.cursor or not page.nodes:
            return PagingStalled(number, connection.key, cursor, len(page.nodes))
        collected.cursor = cursor
    else:
        collected.cursor = None
    return None


def _check_distinct(number: int, connection: str, keys: Sequence[Hashable]) -> None:
    seen: set[Hashable] = set()
    for key in keys:
        if key in seen:
            raise DuplicateEntry(number, connection, key)
        seen.add(key)


def _check_complete(number: int, name: str, collected: _Collected[Any]) -> None:
    if len(collected.nodes) != collected.total:
        raise ConnectionCountMismatch(number, name, len(collected.nodes), collected.total)


def _review(node: ReviewNode) -> Review:
    return Review(
        github_id=node.full_database_id,
        node_id=node.id,
        reviewer=account(node.author),
        state=node.state,
        created_at=utc(node.created_at),
        submitted_at=utc_or_none(node.submitted_at),
        published_at=utc_or_none(node.published_at),
        updated_at=utc(node.updated_at),
        last_edited_at=utc_or_none(node.last_edited_at),
        editor=account(node.editor),
        includes_created_edit=node.includes_created_edit,
        commit_oid=node.commit.oid if node.commit is not None else None,
        author_association=node.author_association,
        author_can_push_to_repository=node.author_can_push_to_repository,
        inline_comments_total=node.comments.total_count,
    )


def _comment(node: CommentNode) -> Comment:
    return Comment(
        github_id=node.full_database_id,
        node_id=node.id,
        author=account(node.author),
        created_at=utc(node.created_at),
        published_at=utc_or_none(node.published_at),
        updated_at=utc(node.updated_at),
        last_edited_at=utc_or_none(node.last_edited_at),
        editor=account(node.editor),
        created_via_email=node.created_via_email,
        includes_created_edit=node.includes_created_edit,
        author_association=node.author_association,
    )


def _review_request(node: ReviewRequestNode) -> ReviewRequest:
    requested = node.requested_reviewer
    return ReviewRequest(
        github_id=node.database_id,
        node_id=node.id,
        as_code_owner=node.as_code_owner,
        requested_type=requested.typename if requested is not None else None,
        account=union_account(requested) if isinstance(requested, RequestedAccount) else None,
        team=_requested_team(requested),
    )


def _requested_team(
    requested: RequestedAccount | RequestedTeamNode | RequestedEnterpriseTeam | None,
) -> RequestedTeam | None:
    """A ``Team`` or an ``EnterpriseTeam``; neither is an account."""
    if isinstance(requested, RequestedTeamNode):
        return RequestedTeam(
            github_id=requested.database_id, node_id=requested.id, slug=requested.slug
        )
    if isinstance(requested, RequestedEnterpriseTeam):
        return RequestedTeam(
            github_id=requested.full_database_id, node_id=requested.id, slug=requested.slug
        )
    return None


def _graphql_commit(node: CommitNode) -> CommitRecord:
    author = node.commit.author
    user = author.user if author is not None else None
    return CommitRecord(
        sha=node.commit.oid,
        author=(
            LinkedAccount(github_id=user.database_id, node_id=user.id, login=user.login)
            if user is not None
            else None
        ),
        author_name=author.name if author is not None else None,
        author_email=author.email if author is not None else None,
        authored_at=utc(node.commit.authored_date),
    )


def _compare_commit(number: int, commit: CompareCommit) -> CommitRecord:
    git_author = commit.commit.author
    linked = commit.author
    if git_author is None:
        raise ResponseShapeError(number, f"compare commit {commit.sha} has no git author")
    return CommitRecord(
        sha=commit.sha,
        author=(
            LinkedAccount(github_id=linked.id, node_id=linked.node_id, login=linked.login)
            if linked is not None
            else None
        ),
        author_name=git_author.name,
        author_email=git_author.email,
        authored_at=utc(git_author.date),
    )
