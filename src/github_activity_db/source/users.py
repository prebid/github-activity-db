"""Look up accounts by node id (redesign plan 5.2)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from pydantic import ValidationError

from .documents import USER_LOOKUP
from .errors import LookupShapeError, RequestFailedError, SourceError, UserLookupFailed
from .limits import USER_LOOKUP_BATCH
from .models import LookedUpUser
from .records import Account, UserLookup
from .transport import GraphQLError, Transport


async def lookup_user_batches(
    transport: Transport, node_ids: Sequence[str]
) -> AsyncIterator[UserLookup]:
    """The account behind each distinct node id, batch by batch.

    An account is None where GitHub has none (deleted). This is where an account first seen
    through a commit gets its type and its GraphQL login (plan 5.2): GraphQL types a commit's
    user ``User`` even for a bot, while ``nodes(ids:)`` returns the real type.

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
        return _all_failed(batch, e)
    nodes = result.data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(batch):
        return _all_failed(batch, LookupShapeError(f"{len(batch)} ids, nodes {nodes!r:.200}"))
    stray = [error for error in result.errors if error.path[:1] != ("nodes",)]
    if stray:
        # An error outside every node cannot be pinned on one id.
        return _all_failed(batch, LookupShapeError(f"errors outside the nodes: {stray[0]}"))
    accounts: dict[str, Account | None] = {}
    failed: dict[str, UserLookupFailed] = {}
    for index, (node_id, raw) in enumerate(zip(batch, nodes, strict=True)):
        account = _account(node_id, raw, result.errors_under("nodes", index))
        if isinstance(account, UserLookupFailed):
            failed[node_id] = account
        else:
            accounts[node_id] = account
    return UserLookup(accounts=accounts, failed=failed)


def _account(
    node_id: str, raw: Any, errors: tuple[GraphQLError, ...]
) -> Account | None | UserLookupFailed:
    if raw is None:
        # None means deleted (D28) only when GitHub says NOT_FOUND; an unexplained null fails.
        if not errors:
            return _failed(node_id, "null node without an error")
        if any(error.type != "NOT_FOUND" for error in errors):
            return _failed(node_id, f"{errors[0].type}: {errors[0].message}")
        return None
    if errors:
        return _failed(node_id, f"partial data with {errors[0].type}: {errors[0].message}")
    try:
        user = LookedUpUser.model_validate(raw)
    except ValidationError as e:
        return _failed(node_id, str(e))
    if user.id != node_id:
        return _failed(node_id, f"answered for {user.id}")
    return Account(
        github_id=user.database_id, node_id=user.id, login=user.login, type=user.typename
    )


def _failed(node_id: str, message: str) -> UserLookupFailed:
    return UserLookupFailed(node_id, LookupShapeError(message))


def _all_failed(batch: list[str], cause: SourceError) -> UserLookup:
    return UserLookup(
        accounts={}, failed={node_id: UserLookupFailed(node_id, cause) for node_id in batch}
    )
