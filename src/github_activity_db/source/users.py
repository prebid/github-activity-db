"""Look up accounts by node id (redesign plan 5.2)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .convert import utc, utc_or_none
from .documents import USER_LOOKUP
from .errors import LookupShapeError, RequestFailedError, SourceError, UserLookupFailed
from .limits import USER_LOOKUP_BATCH
from .models import (
    LookedUpBotNode,
    LookedUpEnterpriseUserNode,
    LookedUpMannequinNode,
    LookedUpNode,
    LookedUpOrganizationNode,
    LookedUpUserNode,
)
from .records import (
    Account,
    LookedUpAccount,
    OrganizationProfile,
    UserLookup,
    UserProfile,
)
from .transport import GraphQLError, Transport

_NODE: TypeAdapter[LookedUpNode] = TypeAdapter(LookedUpNode)


async def lookup_user_batches(
    transport: Transport, node_ids: Sequence[str]
) -> AsyncIterator[UserLookup]:
    """The account behind each distinct node id, batch by batch.

    An account is None where GitHub answers ``NOT_FOUND`` for its id; GitHub does not say why
    (D28). This is where an account first seen through a commit gets its type and its GraphQL
    login (plan 5.2): GraphQL types a commit's user ``User`` even for a bot, while
    ``nodes(ids:)`` returns the real type. A ``User`` or an ``Organization`` also comes with its
    profile (D31).

    A failure is scoped to what it covers: an error or a mismatch for one node fails that id; a
    request that still fails after its retries, or an answer that does not line up with the
    request, fails every id of its batch. Later batches are still looked up.
    """
    distinct = list(dict.fromkeys(node_ids))
    for start in range(0, len(distinct), USER_LOOKUP_BATCH):
        yield await _lookup(transport, distinct[start : start + USER_LOOKUP_BATCH])


async def _lookup(transport: Transport, batch: list[str]) -> UserLookup:
    try:
        result = await transport.graphql(USER_LOOKUP, {"ids": batch}, splittable=False)
    except RequestFailedError as e:
        return _all_failed(batch, e, None)
    nodes = result.data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(batch):
        return _all_failed(
            batch, LookupShapeError(f"{len(batch)} ids, nodes {nodes!r:.200}"), result.date
        )
    stray = [
        error
        for error in result.errors
        if len(error.path) < 2
        or error.path[0] != "nodes"
        or error.path[1] not in range(len(batch))
        or isinstance(error.path[1], bool)
    ]
    if stray:
        # An error outside every node cannot be pinned on one id.
        return _all_failed(
            batch, LookupShapeError(f"errors outside the nodes: {stray[0]}"), result.date
        )
    accounts: dict[str, LookedUpAccount | None] = {}
    failed: dict[str, UserLookupFailed] = {}
    for index, (node_id, raw) in enumerate(zip(batch, nodes, strict=True)):
        account = _account(node_id, index, raw, result.errors_under("nodes", index))
        if isinstance(account, UserLookupFailed):
            failed[node_id] = account
        else:
            accounts[node_id] = account
    return UserLookup(accounts=accounts, failed=failed, looked_up_at=result.date)


def _account(
    node_id: str, index: int, raw: Any, errors: tuple[GraphQLError, ...]
) -> LookedUpAccount | None | UserLookupFailed:
    if raw is None:
        # None only when NOT_FOUND names this node itself, as for a pull request alias; an
        # unexplained null, or one nulled from deeper down, fails the id.
        if errors and all(
            error.type == "NOT_FOUND" and error.path == ("nodes", index) for error in errors
        ):
            return None
        first = errors[0] if errors else None
        return _failed(
            node_id, f"{first.type}: {first.message}" if first else "null node without an error"
        )
    if errors:
        return _failed(node_id, f"partial data with {errors[0].type}: {errors[0].message}")
    try:
        node = _NODE.validate_python(raw)
    except ValidationError as e:
        return _failed(node_id, str(e))
    if node.id != node_id:
        return _failed(node_id, f"answered for {node.id}")
    return _looked_up(node)


def _looked_up(node: LookedUpNode) -> LookedUpAccount:
    name: str | None = None
    claimant: int | None = None
    profile: UserProfile | OrganizationProfile | None = None
    match node:
        case LookedUpUserNode():
            account = Account(node.database_id, node.id, node.login, node.typename)
            name = node.name
            profile = UserProfile(
                company=node.company, location=node.location, website_url=node.website_url
            )
        case LookedUpBotNode():
            account = Account(node.database_id, node.id, node.login, node.typename)
        case LookedUpOrganizationNode():
            account = Account(node.database_id, node.id, node.login, node.typename)
            name = node.name
            profile = OrganizationProfile(
                description=node.description,
                email=node.email,
                location=node.location,
                website_url=node.website_url,
                twitter_username=node.twitter_username,
                is_verified=node.is_verified,
                archived_at=utc_or_none(node.archived_at),
                has_sponsors_listing=node.has_sponsors_listing,
                pinned_items_remaining=node.pinned_items_remaining,
            )
        case LookedUpMannequinNode():
            account = Account(node.database_id, node.id, node.login, node.typename)
            name = node.name
            claimant = node.claimant.database_id if node.claimant is not None else None
        case LookedUpEnterpriseUserNode():
            account = Account(
                github_id=None,
                node_id=node.id,
                login=node.login,
                type=node.typename,
                linked_user_github_id=node.user.database_id if node.user is not None else None,
            )
            name = node.name
    return LookedUpAccount(
        account=account,
        name=name,
        created_at=utc(node.created_at),
        updated_at=utc(node.updated_at),
        claimant_github_id=claimant,
        profile=profile,
    )


def _failed(node_id: str, message: str) -> UserLookupFailed:
    return UserLookupFailed(node_id, LookupShapeError(message))


def _all_failed(batch: list[str], cause: SourceError, date: datetime | None) -> UserLookup:
    return UserLookup(
        accounts={},
        failed={node_id: UserLookupFailed(node_id, cause) for node_id in batch},
        looked_up_at=date,
    )
