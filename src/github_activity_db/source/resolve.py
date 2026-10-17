"""Resolve a configured repository (redesign plan 5.3 step 1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .documents import REPOSITORY_LABELS, RESOLVE
from .errors import (
    LabelCountMismatch,
    RepositoryNotFound,
    RepositoryRequestFailed,
    RepositoryShapeError,
    RepositoryWithoutDefaultBranch,
    RequestFailedError,
)
from .models import LabelPage, RepositoryLabelsNode, RepositoryNode
from .records import RepositoryRecord
from .transport import GraphQLResult, Transport


async def resolve_repository(transport: Transport, owner: str, name: str) -> RepositoryRecord:
    """The repository GitHub serves under ``owner/name``, following renames.

    Raises:
        RepositoryError: the repository is missing, has no default branch, or a request or
            response for it failed. Nothing of it is deleted this run.
    """
    full_name = f"{owner}/{name}"
    result = await _send(transport, full_name, RESOLVE, {"owner": owner, "name": name})
    raw = result.data.get("repository")
    if raw is None:
        if any(error.type == "NOT_FOUND" for error in result.errors_under("repository")):
            raise RepositoryNotFound(full_name)
        raise RepositoryShapeError(full_name, f"repository is null: {_messages(result)}")
    if result.errors:
        raise RepositoryShapeError(full_name, f"errors under repository: {_messages(result)}")
    try:
        repository = RepositoryNode.model_validate(raw)
    except ValidationError as e:
        raise RepositoryShapeError(full_name, f"unexpected repository shape: {e}") from e
    if repository.default_branch_ref is None:
        raise RepositoryWithoutDefaultBranch(full_name)
    labels = await _label_names(transport, full_name, repository)
    return RepositoryRecord(
        github_id=repository.database_id,
        node_id=repository.id,
        owner=repository.owner.login,
        name=repository.name,
        full_name=repository.name_with_owner,
        is_private=repository.is_private,
        is_archived=repository.is_archived,
        default_branch=repository.default_branch_ref.name,
        label_names=frozenset(labels),
    )


async def _label_names(
    transport: Transport, full_name: str, repository: RepositoryNode
) -> list[str]:
    """Every label name, paging past the first 100 through ``node(id:)``."""
    page: LabelPage = repository.labels
    total = page.total_count
    names = _page_names(full_name, page)
    while page.page_info.has_next_page:
        cursor = page.page_info.end_cursor
        if cursor is None:
            raise RepositoryShapeError(full_name, "label page has a next page but no cursor")
        result = await _send(
            transport, full_name, REPOSITORY_LABELS, {"id": repository.id, "after": cursor}
        )
        try:
            node = RepositoryLabelsNode.model_validate(result.data.get("node"))
        except ValidationError as e:
            raise RepositoryShapeError(full_name, f"unexpected label page: {e}") from e
        if node.id != repository.id:
            raise RepositoryShapeError(full_name, f"label page for {node.id}, not {repository.id}")
        page = node.labels
        if page.total_count != total:
            raise LabelCountMismatch(full_name, len(names), page.total_count)
        names.extend(_page_names(full_name, page))
    if len(names) != total:
        raise LabelCountMismatch(full_name, len(names), total)
    return names


def _page_names(full_name: str, page: LabelPage) -> list[str]:
    if page.nodes is None or any(node is None for node in page.nodes):
        raise RepositoryShapeError(full_name, "null label entry")
    return [node.name for node in page.nodes if node is not None]


async def _send(
    transport: Transport, full_name: str, document: str, variables: Mapping[str, Any]
) -> GraphQLResult:
    try:
        return await transport.graphql(document, variables, splittable=False)
    except RequestFailedError as e:
        raise RepositoryRequestFailed(full_name, e) from e


def _messages(result: GraphQLResult) -> str:
    return "; ".join(f"{error.type}: {error.message}" for error in result.errors) or "no errors"
