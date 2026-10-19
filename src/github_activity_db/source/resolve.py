"""Resolve a configured repository (redesign plan 5.3 step 1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .convert import utc, utc_or_none
from .documents import REPOSITORY_LABELS, RESOLVE
from .errors import (
    LabelCountMismatch,
    RepositoryNotFound,
    RepositoryRequestFailed,
    RepositoryShapeError,
    RepositoryWithoutDefaultBranch,
    RequestFailedError,
)
from .models import LabelPage, RepositoryLabelNode, RepositoryLabelsNode, RepositoryNode
from .records import RepositoryLabel, RepositoryRecord
from .transport import GraphQLResult, Transport


async def resolve_repository(transport: Transport, owner: str, name: str) -> RepositoryRecord:
    """The repository GitHub serves under ``owner/name``, following renames, with its labels.

    Raises:
        RepositoryError: the repository is missing, has no default branch, or a request or
            response for it failed, or its label list is incomplete or names a label id twice.
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
    labels = await _labels(transport, full_name, repository)
    seen: set[str] = set()
    for label in labels:
        if label.node_id in seen:
            raise RepositoryShapeError(full_name, f"label id {label.node_id} is listed twice")
        seen.add(label.node_id)
    plan = repository.plan_features
    return RepositoryRecord(
        github_id=repository.database_id,
        node_id=repository.id,
        owner=repository.owner.login,
        name=repository.name,
        full_name=repository.name_with_owner,
        is_private=repository.is_private,
        is_archived=repository.is_archived,
        default_branch=repository.default_branch_ref.name,
        labels=tuple(labels),
        labels_total=repository.labels.total_count,
        is_security_policy_enabled=repository.is_security_policy_enabled,
        visibility=repository.visibility,
        description=repository.description,
        homepage_url=repository.homepage_url,
        created_at=utc(repository.created_at),
        updated_at=utc(repository.updated_at),
        pushed_at=utc_or_none(repository.pushed_at),
        archived_at=utc_or_none(repository.archived_at),
        stargazer_count=repository.stargazer_count,
        fork_count=repository.fork_count,
        disk_usage=repository.disk_usage,
        forking_allowed=repository.forking_allowed,
        allow_update_branch=repository.allow_update_branch,
        web_commit_signoff_required=repository.web_commit_signoff_required,
        has_wiki_enabled=repository.has_wiki_enabled,
        has_discussions_enabled=repository.has_discussions_enabled,
        has_projects_enabled=repository.has_projects_enabled,
        has_vulnerability_alerts_enabled=repository.has_vulnerability_alerts_enabled,
        uses_custom_open_graph_image=repository.uses_custom_open_graph_image,
        merge_commit_allowed=repository.merge_commit_allowed,
        squash_merge_allowed=repository.squash_merge_allowed,
        rebase_merge_allowed=repository.rebase_merge_allowed,
        delete_branch_on_merge=repository.delete_branch_on_merge,
        merge_commit_title=repository.merge_commit_title,
        merge_commit_message=repository.merge_commit_message,
        squash_merge_commit_title=repository.squash_merge_commit_title,
        squash_merge_commit_message=repository.squash_merge_commit_message,
        primary_language=(
            repository.primary_language.name if repository.primary_language is not None else None
        ),
        license_spdx_id=(
            repository.license_info.spdx_id if repository.license_info is not None else None
        ),
        plan_features_codeowners=plan.codeowners,
        plan_features_team_review_requests=plan.team_review_requests,
        plan_features_maximum_manual_review_requests=plan.maximum_manual_review_requests,
        resolved_at=result.date,
    )


async def _labels(
    transport: Transport, full_name: str, repository: RepositoryNode
) -> list[RepositoryLabel]:
    """Every label, paging past the first 100 through ``node(id:)``."""
    page: LabelPage = repository.labels
    total = page.total_count
    labels = _page_labels(full_name, page)
    previous: str | None = None
    while page.page_info.has_next_page:
        cursor = page.page_info.end_cursor
        if cursor is None or cursor == previous or not page.nodes:
            raise RepositoryShapeError(
                full_name,
                f"label page has a next page but no progress ({len(page.nodes or ())} labels, "
                f"cursor {cursor!r})",
            )
        previous = cursor
        result = await _send(
            transport, full_name, REPOSITORY_LABELS, {"id": repository.id, "after": cursor}
        )
        if result.errors:
            raise RepositoryShapeError(full_name, f"errors on a label page: {_messages(result)}")
        try:
            node = RepositoryLabelsNode.model_validate(result.data.get("node"))
        except ValidationError as e:
            raise RepositoryShapeError(full_name, f"unexpected label page: {e}") from e
        if node.id != repository.id:
            raise RepositoryShapeError(full_name, f"label page for {node.id}, not {repository.id}")
        page = node.labels
        if page.total_count != total:
            raise RepositoryShapeError(
                full_name, f"label totalCount changed from {total} to {page.total_count}"
            )
        labels.extend(_page_labels(full_name, page))
        if len(labels) > total:
            raise LabelCountMismatch(full_name, len(labels), total)
    if len(labels) != total:
        raise LabelCountMismatch(full_name, len(labels), total)
    return labels


def _page_labels(full_name: str, page: LabelPage) -> list[RepositoryLabel]:
    if page.nodes is None or any(node is None for node in page.nodes):
        raise RepositoryShapeError(full_name, "null label entry")
    return [_label(node) for node in page.nodes if node is not None]


def _label(node: RepositoryLabelNode) -> RepositoryLabel:
    return RepositoryLabel(
        node_id=node.id,
        name=node.name,
        color=node.color,
        description=node.description,
        is_default=node.is_default,
        created_at=utc_or_none(node.created_at),
        updated_at=utc_or_none(node.updated_at),
    )


async def _send(
    transport: Transport, full_name: str, document: str, variables: Mapping[str, Any]
) -> GraphQLResult:
    try:
        return await transport.graphql(document, variables, splittable=False)
    except RequestFailedError as e:
        raise RepositoryRequestFailed(full_name, e) from e


def _messages(result: GraphQLResult) -> str:
    return "; ".join(f"{error.type}: {error.message}" for error in result.errors) or "no errors"
