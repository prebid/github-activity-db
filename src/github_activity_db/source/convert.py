"""Conversions from the private response models to the public records."""

from __future__ import annotations

from datetime import UTC, datetime

from .models import (
    AccountFields,
    Actor,
    AssignedActorNode,
    ClosingIssueNode,
    EnterpriseActor,
    RequestedAccount,
)
from .records import Account, AccountType, ClosingIssue


def utc(value: datetime) -> datetime:
    """``value`` in UTC; the models accept only aware datetimes."""
    return value.astimezone(UTC)


def utc_or_none(value: datetime | None) -> datetime | None:
    return utc(value) if value is not None else None


def account(actor: Actor | None) -> Account | None:
    """The account behind an actor; None where GitHub returned no actor, which GitHub does not
    explain (D28).

    An ``EnterpriseUserAccount`` has no ``databaseId``: it is kept with a null ``github_id``,
    identified by its node id, its user's ``databaseId`` beside it (plan 5.2, gap 85).
    """
    if actor is None:
        return None
    if isinstance(actor, EnterpriseActor):
        return Account(
            github_id=None,
            node_id=actor.id,
            login=actor.login,
            type=actor.typename,
            linked_user_github_id=actor.user.database_id if actor.user is not None else None,
        )
    return Account(
        github_id=actor.database_id, node_id=actor.id, login=actor.login, type=actor.typename
    )


def union_account(node: RequestedAccount | AssignedActorNode) -> Account:
    """An account selected through a union with ``__typename`` (a requested reviewer, an
    assignee): an actor sighting like any other (plan 5.2)."""
    return _account_fields(node, node.typename)


def _account_fields(node: AccountFields, typename: AccountType) -> Account:
    return Account(github_id=node.database_id, node_id=node.id, login=node.login, type=typename)


def closing_issue(node: ClosingIssueNode) -> ClosingIssue:
    return ClosingIssue(
        repo_github_id=node.repository.database_id,
        repo_full_name=node.repository.name_with_owner,
        number=node.number,
    )
