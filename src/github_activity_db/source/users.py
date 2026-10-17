"""Look up accounts by node id (redesign plan 5.2)."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from .documents import USER_LOOKUP
from .errors import RequestFailedError, UnexpectedResponse, UserLookupFailed
from .limits import USER_LOOKUP_BATCH
from .models import LookedUpUser
from .records import Account
from .transport import Transport


async def lookup_users(transport: Transport, node_ids: Sequence[str]) -> dict[str, Account | None]:
    """The account behind each node id; None where GitHub no longer has one (deleted).

    This is where an account first seen through a commit gets its type and its GraphQL login
    (plan 5.2): GraphQL types a commit's user ``User`` even for a bot, while ``nodes(ids:)``
    returns the real type.

    Raises:
        UserLookupFailed: a lookup request still failed after its retries.
    """
    unique = list(dict.fromkeys(node_ids))
    accounts: dict[str, Account | None] = {}
    for start in range(0, len(unique), USER_LOOKUP_BATCH):
        batch = unique[start : start + USER_LOOKUP_BATCH]
        try:
            result = await transport.graphql(USER_LOOKUP, {"ids": batch}, splittable=False)
        except RequestFailedError as e:
            raise UserLookupFailed(e) from e
        nodes = result.data.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(batch):
            raise _failed(f"user lookup returned {len(batch)} ids but nodes {nodes!r}")
        for index, (node_id, raw) in enumerate(zip(batch, nodes, strict=True)):
            errors = result.errors_under("nodes", index)
            if raw is None:
                if any(error.type != "NOT_FOUND" for error in errors):
                    raise _failed(f"user lookup for {node_id}: {errors[0].message}")
                accounts[node_id] = None
                continue
            try:
                user = LookedUpUser.model_validate(raw)
            except ValidationError as e:
                raise _failed(f"user lookup for {node_id}: {e}") from e
            if user.id != node_id:
                raise _failed(f"user lookup for {node_id} returned {user.id}")
            accounts[node_id] = Account(
                github_id=user.database_id, node_id=user.id, login=user.login, type=user.typename
            )
    return accounts


def _failed(message: str) -> UserLookupFailed:
    return UserLookupFailed(UnexpectedResponse(message))
