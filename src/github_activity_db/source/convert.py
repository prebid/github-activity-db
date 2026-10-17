"""Conversions from the private response models to the public records."""

from __future__ import annotations

from datetime import UTC, datetime

from .errors import ActorWithoutId
from .models import Actor, ClosingIssueNode, EnterpriseActor
from .records import Account, ClosingIssue


def utc(value: datetime) -> datetime:
    """``value`` in UTC; the models accept only aware datetimes."""
    return value.astimezone(UTC)


def account(actor: Actor | None, *, number: int, role: str) -> Account | None:
    """The account behind an actor; a null actor (deleted account) is None.

    An ``EnterpriseUserAccount`` has no ``databaseId``, so the item fails rather than storing a
    null reference that would read as a deleted account (plan 5.2).
    """
    if actor is None:
        return None
    if isinstance(actor, EnterpriseActor):
        raise ActorWithoutId(number, role, actor.typename, actor.login)
    return Account(
        github_id=actor.database_id, node_id=actor.id, login=actor.login, type=actor.typename
    )


def closing_issue(node: ClosingIssueNode) -> ClosingIssue:
    return ClosingIssue(
        repo_github_id=node.repository.database_id,
        repo_full_name=node.repository.name_with_owner,
        number=node.number,
    )
