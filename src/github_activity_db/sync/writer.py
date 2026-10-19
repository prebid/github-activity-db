"""The sync's writes (redesign plan 5.3 steps 1, 3, 6, and 7).

Every function runs on the writer's one connection (``sync.connection``) in its own transaction,
which with ``run_id`` first checks that the run's row is still ``running``. A refusal
(``WriteRefused``) is raised inside that transaction, which then rolls back, so a refused write
leaves nothing in the file.
The engine passes every time stamped; nothing here reads the clock or sends a request.

Accounts are keyed by GitHub's ``databaseId``, or by node id for an ``EnterpriseUserAccount``,
which has none (5.2). Only lookups set a stored account's login and type; an actor sighting
inserts an account not yet stored and clears ``not_found_at`` (D28).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from github_activity_db.db.models import (
    AccountProfile,
    GitHubUser,
    PullRequest,
    PullRequestAssignee,
    PullRequestClosingIssue,
    PullRequestComment,
    PullRequestCommit,
    PullRequestFile,
    PullRequestLabel,
    PullRequestReview,
    PullRequestReviewRequest,
    Repository,
    RepositoryLabel,
    SyncFailure,
    SyncRun,
)
from github_activity_db.source.documents import PULL_REQUEST_KIND
from github_activity_db.source.records import (
    Account,
    LookedUpAccount,
    OrganizationProfile,
    PullRequestRecord,
    RepositoryRecord,
    UserProfile,
)
from github_activity_db.sync.connection import transaction
from github_activity_db.sync.deletions import DeletionApproval, Deletions
from github_activity_db.sync.plan import InPlaceUpdate

# An account is due for the refresh when its refreshed_at is more than this far behind (5.2).
REFRESH_AFTER: Final = timedelta(hours=24)

_ENTERPRISE: Final = "EnterpriseUserAccount"


# --------------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------------
class WriteRefused(Exception):
    """A write the writer refused before writing anything."""


class RepositoryConflict(WriteRefused):
    """Another tracked repository holds the resolved ``full_name``, ignoring case (5.1)."""

    def __init__(self, record: RepositoryRecord, holder_github_id: int) -> None:
        super().__init__(
            f"{record.full_name} (github_id {record.github_id}): the tracked repository with "
            f"github_id {holder_github_id} holds that name"
        )
        self.holder_github_id = holder_github_id


class ItemConflict(WriteRefused):
    """A hydrated item that cannot be written as GitHub reports it: an id the database stores
    under another pull request, or an account identity it stores differently. The engine records
    it at once, without the solo retry (5.3 step 3)."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(f"#{number}: {message}")
        self.number = number


class AccountMissing(WriteRefused):
    """A commit link's account is neither stored nor given with the record (5.2)."""

    def __init__(self, number: int, github_ids: Sequence[int]) -> None:
        super().__init__(
            f"#{number}: commit links to accounts neither stored nor given: {list(github_ids)}"
        )
        self.number = number
        self.github_ids = tuple(github_ids)


class LookupConflict(WriteRefused):
    """A lookup whose ``databaseId`` and node id disagree with what the database stores (5.2,
    gap 78); for the refresh, a failed refresh."""


# --------------------------------------------------------------------------------------------
# Account inputs
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Sighted:
    """An actor sighting, with the ``Date`` of the response that carried it."""

    account: Account
    seen_at: datetime


@dataclass(frozen=True, slots=True)
class Found:
    """A lookup that returned the account, with its response's ``Date``."""

    account: LookedUpAccount
    looked_up_at: datetime


@dataclass(frozen=True, slots=True)
class NotFound:
    """A lookup that answered ``NOT_FOUND``, with its response's ``Date``."""

    looked_up_at: datetime


# A commit link's account: a sighting in the same batch, or a lookup's answer.
LinkAccount = Sighted | Found | NotFound


@dataclass(frozen=True, slots=True)
class SecondNodeId:
    """A ``databaseId`` GitHub reported under another node id than the stored one; the stored row
    keeps its node id and the run reports this (plan section 8)."""

    github_id: int
    stored_node_id: str
    reported_node_id: str


# --------------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredRepositoryRef:
    row_id: int
    github_id: int
    full_name: str


async def tracked_repository_named(
    connection: AsyncConnection, full_name: str
) -> StoredRepositoryRef | None:
    """The ``is_tracked`` row whose ``full_name`` equals a configured name, ignoring case; step 1
    fails a repository that resolves to another ``github_id`` (5.3 step 1)."""
    async with transaction(connection):
        rows = (
            await connection.execute(
                select(Repository.id, Repository.github_id, Repository.full_name).where(
                    Repository.is_tracked.is_(True)
                )
            )
        ).all()
    name = full_name.lower()
    for row in rows:
        if row.full_name.lower() == name:
            return StoredRepositoryRef(row.id, row.github_id, row.full_name)
    return None


@dataclass(frozen=True, slots=True)
class StoredAccount:
    row_id: int
    github_id: int | None
    node_id: str
    not_found_at: datetime | None


async def stored_accounts(
    connection: AsyncConnection, github_ids: Iterable[int]
) -> dict[int, StoredAccount]:
    """Stored accounts by ``github_id``, for the commit links a batch carries (5.2, gap 90)."""
    ids = sorted(set(github_ids))
    if not ids:
        return {}
    async with transaction(connection):
        rows = (
            await connection.execute(
                select(
                    GitHubUser.id, GitHubUser.github_id, GitHubUser.node_id, GitHubUser.not_found_at
                ).where(GitHubUser.github_id.in_(ids))
            )
        ).all()
    return {
        row.github_id: StoredAccount(row.id, row.github_id, row.node_id, row.not_found_at)
        for row in rows
    }


@dataclass(frozen=True, slots=True)
class RefreshTarget:
    node_id: str
    github_id: int | None
    refreshed_at: datetime


async def accounts_to_refresh(
    connection: AsyncConnection, *, now: datetime
) -> tuple[RefreshTarget, ...]:
    """Accounts last refreshed more than 24 hours before ``now`` and without ``not_found_at``,
    oldest ``refreshed_at`` first (5.2)."""
    async with transaction(connection):
        rows = (
            await connection.execute(
                select(GitHubUser.node_id, GitHubUser.github_id, GitHubUser.refreshed_at)
                .where(
                    GitHubUser.refreshed_at < now - REFRESH_AFTER,
                    GitHubUser.not_found_at.is_(None),
                )
                .order_by(GitHubUser.refreshed_at, GitHubUser.id)
            )
        ).all()
    return tuple(RefreshTarget(row.node_id, row.github_id, row.refreshed_at) for row in rows)


# --------------------------------------------------------------------------------------------
# Step 1: repositories
# --------------------------------------------------------------------------------------------
def full_name_conflict(
    stored: Iterable[tuple[int, str, bool]], github_id: int, full_name: str
) -> int | None:
    """The ``github_id`` of another ``is_tracked`` repository holding ``full_name``, ignoring
    case, or None. ``stored`` holds (github_id, full_name, is_tracked) rows; a dry run passes its
    snapshot, the write the rows it reads."""
    name = full_name.lower()
    for stored_id, stored_name, is_tracked in stored:
        if is_tracked and stored_id != github_id and stored_name.lower() == name:
            return stored_id
    return None


@dataclass(frozen=True, slots=True)
class RepositoryWrite:
    row_id: int
    inserted: bool
    default_branch_changed: bool


_REPOSITORY_FIELDS: Final = (
    "node_id",
    "owner",
    "name",
    "full_name",
    "is_private",
    "is_archived",
    "default_branch",
    "labels_total",
    "is_security_policy_enabled",
    "visibility",
    "description",
    "homepage_url",
    "created_at",
    "updated_at",
    "pushed_at",
    "archived_at",
    "stargazer_count",
    "fork_count",
    "disk_usage",
    "forking_allowed",
    "allow_update_branch",
    "web_commit_signoff_required",
    "has_wiki_enabled",
    "has_discussions_enabled",
    "has_projects_enabled",
    "has_vulnerability_alerts_enabled",
    "uses_custom_open_graph_image",
    "merge_commit_allowed",
    "squash_merge_allowed",
    "rebase_merge_allowed",
    "delete_branch_on_merge",
    "merge_commit_title",
    "merge_commit_message",
    "squash_merge_commit_title",
    "squash_merge_commit_message",
    "primary_language",
    "license_spdx_id",
    "plan_features_codeowners",
    "plan_features_team_review_requests",
    "plan_features_maximum_manual_review_requests",
    "resolved_at",
)


async def write_repository(
    connection: AsyncConnection, run_id: int, record: RepositoryRecord, *, group: str
) -> RepositoryWrite:
    """Step 1 for a resolved repository: its row by ``github_id``, tracked, with the configured
    group; its labels replaced; and, when the default branch changed, ``targets_default_branch``
    recomputed for its pull requests (5.1, 5.3 step 1).

    Raises:
        RepositoryConflict: another tracked repository holds the resolved name.
    """
    values: dict[str, Any] = {field: getattr(record, field) for field in _REPOSITORY_FIELDS}
    values.update(repo_group=group, is_tracked=True)
    async with transaction(connection, run_id):
        stored = (
            (
                await connection.execute(
                    select(Repository.github_id, Repository.full_name, Repository.is_tracked)
                )
            )
            .tuples()
            .all()
        )
        holder = full_name_conflict(stored, record.github_id, record.full_name)
        if holder is not None:
            raise RepositoryConflict(record, holder)
        before = (
            await connection.execute(
                select(Repository.id, Repository.default_branch).where(
                    Repository.github_id == record.github_id
                )
            )
        ).first()
        await connection.execute(
            sqlite_insert(Repository)
            .values(github_id=record.github_id, **values)
            .on_conflict_do_update(index_elements=["github_id"], set_=values)
        )
        row_id = (
            await connection.execute(
                select(Repository.id).where(Repository.github_id == record.github_id)
            )
        ).scalar_one()
        await connection.execute(
            delete(RepositoryLabel).where(RepositoryLabel.repository_id == row_id)
        )
        if record.labels:
            await connection.execute(
                insert(RepositoryLabel),
                [
                    {
                        "repository_id": row_id,
                        "node_id": label.node_id,
                        "name": label.name,
                        "color": label.color,
                        "description": label.description,
                        "is_default": label.is_default,
                        "created_at": label.created_at,
                        "updated_at": label.updated_at,
                    }
                    for label in record.labels
                ],
            )
        changed = before is not None and before.default_branch != record.default_branch
        if changed:
            await connection.execute(
                update(PullRequest)
                .where(PullRequest.repository_id == row_id)
                .values(targets_default_branch=PullRequest.base_ref == record.default_branch)
            )
    return RepositoryWrite(row_id=row_id, inserted=before is None, default_branch_changed=changed)


async def mark_untracked(
    connection: AsyncConnection, run_id: int, github_ids: Iterable[int]
) -> None:
    """Mark stored repositories ``is_tracked = false`` (D27); their rows stay."""
    ids = sorted(set(github_ids))
    if not ids:
        return
    async with transaction(connection, run_id):
        await connection.execute(
            update(Repository).where(Repository.github_id.in_(ids)).values(is_tracked=False)
        )


# --------------------------------------------------------------------------------------------
# Step 3: in-place updates
# --------------------------------------------------------------------------------------------
async def apply_in_place_updates(
    connection: AsyncConnection, run_id: int, updates: Sequence[InPlaceUpdate]
) -> None:
    """The index's ``is_draft`` and ``author_association`` for items not planned for hydration;
    nothing else changes, ``github_updated_at`` included (5.3 step 3)."""
    if not updates:
        return
    async with transaction(connection, run_id):
        for change in updates:
            await connection.execute(
                update(PullRequest)
                .where(PullRequest.id == change.row_id)
                .values(is_draft=change.is_draft, author_association=change.author_association)
            )


# --------------------------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------------------------
class _Accounts:
    """Account rows one transaction resolves, inserts, and updates.

    ``number`` is the item the transaction writes, None for a lookup's own transaction: an
    identity conflict is then a ``LookupConflict``, and an ``ItemConflict`` for an item.
    """

    def __init__(self, connection: AsyncConnection, number: int | None) -> None:
        self.connection = connection
        self.number = number
        self.rows: dict[str, int] = {}  # node id -> row id, for this transaction
        self.inserted: list[str] = []
        self.second_node_ids: list[SecondNodeId] = []

    def _conflict(self, message: str) -> WriteRefused:
        if self.number is None:
            return LookupConflict(message)
        return ItemConflict(self.number, message)

    async def _row(self, account: Account) -> Any:
        if account.type == _ENTERPRISE or account.github_id is None:
            key = GitHubUser.node_id == account.node_id
        else:
            key = GitHubUser.github_id == account.github_id
        return (
            await self.connection.execute(
                select(GitHubUser.id, GitHubUser.github_id, GitHubUser.node_id).where(key)
            )
        ).first()

    async def _node_holder(self, node_id: str) -> Any:
        return (
            await self.connection.execute(
                select(GitHubUser.id, GitHubUser.github_id).where(GitHubUser.node_id == node_id)
            )
        ).first()

    async def sighted(self, account: Account, seen_at: datetime) -> int:
        """An actor sighting: insert it when not stored, and clear ``not_found_at`` (D28)."""
        if account.node_id in self.rows:
            return self.rows[account.node_id]
        row = await self._row(account)
        if row is not None:
            if row.node_id != account.node_id:
                await self._check_node_free(account)
                assert account.github_id is not None
                self.second_node_ids.append(
                    SecondNodeId(account.github_id, row.node_id, account.node_id)
                )
            elif account.type == _ENTERPRISE and row.github_id is not None:
                raise self._conflict(
                    f"node {account.node_id} is stored as github_id {row.github_id}, not as an "
                    "EnterpriseUserAccount"
                )
            await self.connection.execute(
                update(GitHubUser).where(GitHubUser.id == row.id).values(not_found_at=None)
            )
            self.rows[account.node_id] = row.id
            return int(row.id)
        await self._check_node_free(account)
        result = await self.connection.execute(
            insert(GitHubUser).values(
                github_id=account.github_id,
                node_id=account.node_id,
                login=account.login,
                type=account.type,
                linked_user_github_id=account.linked_user_github_id,
                refreshed_at=seen_at,
            )
        )
        assert result.inserted_primary_key is not None
        row_id = int(result.inserted_primary_key[0])
        self.rows[account.node_id] = row_id
        self.inserted.append(account.node_id)
        return row_id

    async def _check_node_free(self, account: Account) -> None:
        holder = await self._node_holder(account.node_id)
        if holder is not None and holder.github_id != account.github_id:
            raise self._conflict(
                f"node {account.node_id} is stored under github_id {holder.github_id}, GitHub "
                f"reports {account.github_id}"
            )

    async def found(self, looked: LookedUpAccount, looked_up_at: datetime) -> int:
        """A lookup that returned the account: login, type, and the looked-up fields, the
        profile, ``refreshed_at``, and ``not_found_at`` cleared (5.2, D31)."""
        account = looked.account
        row = await self._row(account)
        holder = await self._node_holder(account.node_id)
        if holder is not None and (row is None or holder.id != row.id):
            raise self._conflict(
                f"node {account.node_id} is stored under github_id {holder.github_id}, the lookup "
                f"returned {account.github_id}"
            )
        values = {
            "login": account.login,
            "type": account.type,
            "name": looked.name,
            "created_at": looked.created_at,
            "updated_at": looked.updated_at,
            "linked_user_github_id": account.linked_user_github_id,
            "claimant_github_id": looked.claimant_github_id,
            "refreshed_at": looked_up_at,
            "not_found_at": None,
        }
        if row is None:
            result = await self.connection.execute(
                insert(GitHubUser).values(
                    github_id=account.github_id, node_id=account.node_id, **values
                )
            )
            assert result.inserted_primary_key is not None
            row_id = int(result.inserted_primary_key[0])
            self.inserted.append(account.node_id)
        else:
            row_id = int(row.id)
            if row.node_id != account.node_id:
                assert account.github_id is not None
                self.second_node_ids.append(
                    SecondNodeId(account.github_id, row.node_id, account.node_id)
                )
            await self.connection.execute(
                update(GitHubUser).where(GitHubUser.id == row_id).values(**values)
            )
        await _replace_profile(self.connection, row_id, looked, looked_up_at)
        self.rows[account.node_id] = row_id
        return row_id


async def _replace_profile(
    connection: AsyncConnection, account_id: int, looked: LookedUpAccount, looked_up_at: datetime
) -> None:
    """The whole profile row as the lookup returned it; none for a type without one (D31)."""
    await connection.execute(delete(AccountProfile).where(AccountProfile.account_id == account_id))
    profile = looked.profile
    if profile is None:
        return
    values: dict[str, Any] = {"account_id": account_id, "refreshed_at": looked_up_at}
    if isinstance(profile, UserProfile):
        values.update(
            company=profile.company, location=profile.location, website_url=profile.website_url
        )
    elif isinstance(profile, OrganizationProfile):
        values.update(
            description=profile.description,
            email=profile.email,
            location=profile.location,
            website_url=profile.website_url,
            twitter_username=profile.twitter_username,
            is_verified=profile.is_verified,
            archived_at=profile.archived_at,
            has_sponsors_listing=profile.has_sponsors_listing,
            pinned_items_remaining=profile.pinned_items_remaining,
        )
    await connection.execute(insert(AccountProfile).values(**values))


@dataclass(frozen=True, slots=True)
class LookupWrite:
    """What a lookup wrote: the account's row, None when ``NOT_FOUND`` met no stored row."""

    row_id: int | None
    inserted: bool
    second_node_ids: tuple[SecondNodeId, ...] = ()


async def write_lookup(
    connection: AsyncConnection,
    run_id: int,
    node_id: str,
    result: Found | NotFound,
    *,
    expected_github_id: int | None = None,
) -> LookupWrite:
    """One lookup's answer for ``node_id``, in its own transaction (the refresh and gap 90).

    ``Found`` sets login, type, the looked-up fields, the profile, and ``refreshed_at``, and clears
    ``not_found_at``. ``NotFound`` sets ``not_found_at`` on a stored account that has none and
    leaves its values, its profile, and every reference to it; it writes nothing for an account
    never stored (D28).

    Raises:
        LookupConflict: the lookup answered for another node id, its ``databaseId`` is not
            ``expected_github_id``, or the database stores the node under another
            ``databaseId``.
    """
    async with transaction(connection, run_id):
        if isinstance(result, NotFound):
            row = (
                await connection.execute(select(GitHubUser.id).where(GitHubUser.node_id == node_id))
            ).first()
            if row is None:
                return LookupWrite(row_id=None, inserted=False)
            await connection.execute(
                update(GitHubUser)
                .where(GitHubUser.id == row.id, GitHubUser.not_found_at.is_(None))
                .values(not_found_at=result.looked_up_at)
            )
            return LookupWrite(row_id=int(row.id), inserted=False)
        account = result.account.account
        if account.node_id != node_id:
            raise LookupConflict(f"lookup for node {node_id} answered for node {account.node_id}")
        if expected_github_id is not None and account.github_id != expected_github_id:
            raise LookupConflict(
                f"node {node_id}: the lookup returned github_id {account.github_id}, expected "
                f"{expected_github_id}"
            )
        accounts = _Accounts(connection, number=None)
        row_id = await accounts.found(result.account, result.looked_up_at)
    return LookupWrite(
        row_id=row_id,
        inserted=bool(accounts.inserted),
        second_node_ids=tuple(accounts.second_node_ids),
    )


# --------------------------------------------------------------------------------------------
# Step 6: items
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ItemWrite:
    """A written pull request: its row, the accounts its transaction inserted (node ids), and
    the second node ids it met."""

    row_id: int
    inserted_accounts: tuple[str, ...]
    second_node_ids: tuple[SecondNodeId, ...]


async def write_pull_request(
    connection: AsyncConnection,
    run_id: int,
    record: PullRequestRecord,
    *,
    repository_github_id: int,
    links: Mapping[int, LinkAccount],
) -> ItemWrite:
    """Step 6 for a hydrated pull request, in one transaction (5.3 step 6, D28).

    ``links`` gives, by ``github_id``, the account of each commit link that is not stored: a
    sighting in the same batch or a lookup's answer. The accounts the item needs are written
    first; then the row, upserted by ``github_id`` so its row id and its ``pr_user_tags`` stay;
    then its child rows, replaced; and its ``sync_failures`` row is deleted.

    Raises:
        ItemConflict: the number is stored under another ``github_id``, the ``github_id`` under
            another repository or number, a review or comment id under another pull request,
            or an account identity differs from the stored one.
        AccountMissing: a commit link's account is neither stored nor in ``links``.
    """
    number = record.number
    async with transaction(connection, run_id):
        repository = (
            await connection.execute(
                select(Repository.id, Repository.default_branch).where(
                    Repository.github_id == repository_github_id
                )
            )
        ).one()
        stored = (
            await connection.execute(
                select(
                    PullRequest.id,
                    PullRequest.repository_id,
                    PullRequest.number,
                    PullRequest.author_id,
                    PullRequest.merged_by_id,
                ).where(PullRequest.github_id == record.github_id)
            )
        ).first()
        if stored is not None and (
            stored.repository_id != repository.id or stored.number != number
        ):
            raise ItemConflict(
                number, f"github_id {record.github_id} is stored as another pull request"
            )
        holder = (
            await connection.execute(
                select(PullRequest.github_id).where(
                    PullRequest.repository_id == repository.id, PullRequest.number == number
                )
            )
        ).scalar_one_or_none()
        if holder is not None and holder != record.github_id:
            raise ItemConflict(number, f"the number is stored under github_id {holder}")
        own_id = stored.id if stored is not None else None
        await _refuse_children_elsewhere(connection, number, own_id, record)

        accounts = _Accounts(connection, number)

        async def sighted(account: Account | None) -> int | None:
            return None if account is None else await accounts.sighted(account, record.hydrated_at)

        commit_rows = await _commit_accounts(connection, accounts, record, links)
        author_id = await sighted(record.author)
        merged_by_id = await sighted(record.merged_by)
        editor_id = await sighted(record.editor)
        values = _pull_request_values(record, repository.id, repository.default_branch)
        values.update(
            author_id=author_id
            if author_id is not None
            else (stored.author_id if stored else None),
            author_reported=record.author is not None,
            merged_by_id=(
                merged_by_id
                if merged_by_id is not None
                else (stored.merged_by_id if stored else None)
            ),
            merged_by_reported=record.merged_by is not None,
            editor_id=editor_id,
        )
        await connection.execute(
            sqlite_insert(PullRequest)
            .values(github_id=record.github_id, **values)
            .on_conflict_do_update(index_elements=["github_id"], set_=values)
        )
        pr_id = (
            await connection.execute(
                select(PullRequest.id).where(PullRequest.github_id == record.github_id)
            )
        ).scalar_one()
        await _replace_children(connection, pr_id, record, accounts, sighted, commit_rows)
        await connection.execute(
            delete(SyncFailure).where(
                SyncFailure.repository_id == repository.id,
                SyncFailure.item_kind == PULL_REQUEST_KIND,
                SyncFailure.number == number,
            )
        )
    return ItemWrite(
        row_id=pr_id,
        inserted_accounts=tuple(accounts.inserted),
        second_node_ids=tuple(accounts.second_node_ids),
    )


async def _refuse_children_elsewhere(
    connection: AsyncConnection, number: int, own_id: int | None, record: PullRequestRecord
) -> None:
    """A review or comment id the database stores under another pull request (5.3 step 5)."""
    for table, ids, kind in (
        (PullRequestReview, [review.github_id for review in record.reviews], "review"),
        (PullRequestComment, [comment.github_id for comment in record.comments], "comment"),
    ):
        if not ids:
            continue
        query = select(table.github_id).where(table.github_id.in_(ids))
        if own_id is not None:
            query = query.where(table.pr_id != own_id)
        elsewhere = sorted((await connection.execute(query)).scalars().all())
        if elsewhere:
            raise ItemConflict(
                number, f"{kind} ids {elsewhere} are stored under another pull request"
            )


async def _commit_accounts(
    connection: AsyncConnection,
    accounts: _Accounts,
    record: PullRequestRecord,
    links: Mapping[int, LinkAccount],
) -> dict[int, int | None]:
    """Each commit link's account row by ``github_id``; None for one a lookup answered
    ``NOT_FOUND`` and the database never stored (5.2).

    A link's account comes from ``links`` when given (a lookup's answer, or a sighting in another
    record of the batch), else from the stored row, else from an actor sighting in this record.
    """
    linked = sorted({commit_link.github_id for commit_link in record.commit_links()})
    if not linked:
        return {}
    stored = dict(
        (
            await connection.execute(
                select(GitHubUser.github_id, GitHubUser.id).where(GitHubUser.github_id.in_(linked))
            )
        )
        .tuples()
        .all()
    )
    own = {seen.github_id: seen for seen in record.accounts() if seen.github_id is not None}
    missing = [
        github_id
        for github_id in linked
        if github_id not in links and github_id not in stored and github_id not in own
    ]
    if missing:
        raise AccountMissing(record.number, missing)
    rows: dict[int, int | None] = {}
    for github_id in linked:
        given = links.get(github_id)
        if isinstance(given, Sighted):
            if given.account.github_id != github_id:
                raise ItemConflict(
                    record.number,
                    f"the sighting given for commit link {github_id} is of github_id "
                    f"{given.account.github_id}",
                )
            rows[github_id] = await accounts.sighted(given.account, given.seen_at)
        elif isinstance(given, Found):
            if given.account.account.github_id != github_id:
                raise ItemConflict(
                    record.number,
                    f"the lookup for commit link {github_id} returned github_id "
                    f"{given.account.account.github_id}",
                )
            rows[github_id] = await accounts.found(given.account, given.looked_up_at)
        elif isinstance(given, NotFound):
            rows[github_id] = stored.get(github_id)
            if github_id in stored:
                await connection.execute(
                    update(GitHubUser)
                    .where(GitHubUser.id == stored[github_id], GitHubUser.not_found_at.is_(None))
                    .values(not_found_at=given.looked_up_at)
                )
        elif github_id in stored:
            rows[github_id] = stored[github_id]
        else:
            rows[github_id] = await accounts.sighted(own[github_id], record.hydrated_at)
    return rows


def _pull_request_values(
    record: PullRequestRecord, repository_id: int, default_branch: str
) -> dict[str, Any]:
    totals = record.totals
    return {
        "repository_id": repository_id,
        "number": record.number,
        "node_id": record.node_id,
        "state": record.state,
        "is_draft": record.is_draft,
        "title": record.title,
        "body": record.body,
        "author_association": record.author_association,
        "created_at": record.created_at,
        "merged_at": record.merged_at,
        "closed_at": record.closed_at,
        "published_at": record.published_at,
        "last_edited_at": record.last_edited_at,
        "includes_created_edit": record.includes_created_edit,
        "maintainer_can_modify": record.maintainer_can_modify,
        "base_ref": record.base_ref,
        "base_ref_oid": record.base_ref_oid,
        "head_ref_name": record.head_ref_name,
        "head_ref_oid": record.head_ref_oid,
        "head_repo_github_id": record.head_repository.github_id if record.head_repository else None,
        "head_repo_full_name": record.head_repository.full_name if record.head_repository else None,
        "head_repo_owner_node_id": (
            record.head_repository_owner.node_id if record.head_repository_owner else None
        ),
        "head_repo_owner_login": (
            record.head_repository_owner.login if record.head_repository_owner else None
        ),
        "merge_commit_oid": record.merge_commit_oid,
        "targets_default_branch": record.base_ref == default_branch,
        "is_cross_repository": record.is_cross_repository,
        "milestone_number": record.milestone.number if record.milestone else None,
        "milestone_title": record.milestone.title if record.milestone else None,
        "review_decision": record.review_decision,
        "mergeable": record.mergeable,
        "merge_state_status": record.merge_state_status,
        "can_be_rebased": record.can_be_rebased,
        "potential_merge_commit_oid": record.potential_merge_commit_oid,
        "stack_node_id": record.stack.node_id if record.stack else None,
        "stack_number": record.stack.number if record.stack else None,
        "stack_size": record.stack.size if record.stack else None,
        "stack_base_ref_name": record.stack.base_ref_name if record.stack else None,
        "stack_entry_node_id": record.stack_entry.node_id if record.stack_entry else None,
        "stack_position": record.stack_entry.position if record.stack_entry else None,
        "additions": record.additions,
        "deletions": record.deletions,
        "changed_files": record.changed_files,
        "total_comments_count": record.total_comments_count,
        "commits_total": totals.commits,
        "files_total": totals.files,
        "reviews_total": totals.reviews,
        "comments_total": totals.comments,
        "labels_total": totals.labels,
        "closing_issues_total": totals.closing_issues,
        "user_linked_closing_issues_total": totals.user_linked_closing_issues,
        "review_requests_total": totals.review_requests,
        "assignees_total": totals.assignees,
        "github_updated_at": record.github_updated_at,
        "hydrated_at": record.hydrated_at,
        "hydration_version": record.hydration_version,
    }


async def _replace_children(
    connection: AsyncConnection,
    pr_id: int,
    record: PullRequestRecord,
    accounts: _Accounts,
    sighted: Any,
    commit_rows: Mapping[int, int | None],
) -> None:
    """Delete and insert the hydrated child rows; a reference D28 keeps is read first."""
    kept_reviewers = dict(
        (
            await connection.execute(
                select(PullRequestReview.github_id, PullRequestReview.reviewer_id).where(
                    PullRequestReview.pr_id == pr_id
                )
            )
        )
        .tuples()
        .all()
    )
    kept_commenters = dict(
        (
            await connection.execute(
                select(PullRequestComment.github_id, PullRequestComment.author_id).where(
                    PullRequestComment.pr_id == pr_id
                )
            )
        )
        .tuples()
        .all()
    )
    kept_commit_authors = dict(
        (
            await connection.execute(
                select(PullRequestCommit.sha, PullRequestCommit.author_id).where(
                    PullRequestCommit.pr_id == pr_id
                )
            )
        )
        .tuples()
        .all()
    )
    for table in (
        PullRequestLabel,
        PullRequestReview,
        PullRequestComment,
        PullRequestCommit,
        PullRequestFile,
        PullRequestClosingIssue,
        PullRequestReviewRequest,
        PullRequestAssignee,
    ):
        await connection.execute(delete(table).where(table.pr_id == pr_id))

    rows: list[tuple[Any, list[dict[str, Any]]]] = []
    rows.append(
        (
            PullRequestLabel,
            [
                {"pr_id": pr_id, "name": label.name, "label_node_id": label.node_id}
                for label in record.labels
            ],
        )
    )
    reviews = []
    for review in record.reviews:
        reviewer_id = await sighted(review.reviewer)
        reviews.append(
            {
                "github_id": review.github_id,
                "node_id": review.node_id,
                "pr_id": pr_id,
                "reviewer_id": reviewer_id
                if reviewer_id is not None
                else kept_reviewers.get(review.github_id),
                "reviewer_reported": review.reviewer is not None,
                "state": review.state,
                "created_at": review.created_at,
                "submitted_at": review.submitted_at,
                "published_at": review.published_at,
                "updated_at": review.updated_at,
                "last_edited_at": review.last_edited_at,
                "editor_id": await sighted(review.editor),
                "includes_created_edit": review.includes_created_edit,
                "commit_oid": review.commit_oid,
                "author_association": review.author_association,
                "author_can_push_to_repository": review.author_can_push_to_repository,
                "inline_comments_total": review.inline_comments_total,
            }
        )
    rows.append((PullRequestReview, reviews))
    comments = []
    for comment in record.comments:
        author_id = await sighted(comment.author)
        comments.append(
            {
                "github_id": comment.github_id,
                "node_id": comment.node_id,
                "pr_id": pr_id,
                "author_id": author_id
                if author_id is not None
                else kept_commenters.get(comment.github_id),
                "author_reported": comment.author is not None,
                "created_at": comment.created_at,
                "published_at": comment.published_at,
                "updated_at": comment.updated_at,
                "last_edited_at": comment.last_edited_at,
                "editor_id": await sighted(comment.editor),
                "created_via_email": comment.created_via_email,
                "includes_created_edit": comment.includes_created_edit,
                "author_association": comment.author_association,
            }
        )
    rows.append((PullRequestComment, comments))
    rows.append(
        (
            PullRequestCommit,
            [
                {
                    "pr_id": pr_id,
                    "sha": commit.sha,
                    "author_id": (
                        commit_rows.get(commit.author.github_id)
                        if commit.author is not None
                        else kept_commit_authors.get(commit.sha)
                    ),
                    "linked_github_id": commit.author.github_id
                    if commit.author is not None
                    else None,
                    "author_name": commit.author_name,
                    "author_email": commit.author_email,
                    "authored_at": commit.authored_at,
                }
                for commit in record.commits
            ],
        )
    )
    rows.append(
        (
            PullRequestFile,
            [
                {
                    "pr_id": pr_id,
                    "ordinal": ordinal,
                    "path": change.path,
                    "change_type": change.change_type,
                    "additions": change.additions,
                    "deletions": change.deletions,
                }
                for ordinal, change in enumerate(record.files)
            ],
        )
    )
    rows.append(
        (
            PullRequestClosingIssue,
            [
                {
                    "pr_id": pr_id,
                    "issue_repo_github_id": link.repo_github_id,
                    "issue_repo_full_name": link.repo_full_name,
                    "issue_number": link.number,
                    "issue_github_id": link.github_id,
                    "user_linked": link.user_linked,
                }
                for link in record.closing_issues
            ],
        )
    )
    requests = []
    for ordinal, request in enumerate(record.review_requests):
        account = request.account
        team = request.team
        requests.append(
            {
                "pr_id": pr_id,
                "ordinal": ordinal,
                "request_github_id": request.github_id,
                "request_node_id": request.node_id,
                "requested_type": request.requested_type,
                "requested_github_id": (
                    account.github_id if account is not None else team.github_id if team else None
                ),
                "requested_node_id": (
                    account.node_id if account is not None else team.node_id if team else None
                ),
                "account_id": await sighted(account),
                "team_slug": team.slug if team is not None else None,
                "as_code_owner": request.as_code_owner,
            }
        )
    rows.append((PullRequestReviewRequest, requests))
    assignees = [
        {"pr_id": pr_id, "account_id": await sighted(assignee)} for assignee in record.assignees
    ]
    rows.append((PullRequestAssignee, assignees))
    for table, values in rows:
        if values:
            await connection.execute(insert(table), values)


# --------------------------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------------------------
def error_text(error: BaseException) -> str:
    """``sync_failures.last_error``: the error's class and message (5.1)."""
    return f"{type(error).__name__}: {error}"


async def record_failure(
    connection: AsyncConnection,
    run_id: int,
    *,
    repository_github_id: int,
    number: int,
    error: BaseException,
    failed_at: datetime,
    run_started_at: datetime,
) -> None:
    """An item failure: ``attempts`` counts the runs in which it failed, so a second failure in
    the same run (its stored ``last_failed_at`` at or after ``run_started_at``) does not add one;
    ``first_failed_at`` stays (5.1)."""
    async with transaction(connection, run_id):
        repository_id = (
            await connection.execute(
                select(Repository.id).where(Repository.github_id == repository_github_id)
            )
        ).scalar_one()
        stored = (
            await connection.execute(
                select(SyncFailure.id, SyncFailure.attempts, SyncFailure.last_failed_at).where(
                    SyncFailure.repository_id == repository_id,
                    SyncFailure.item_kind == PULL_REQUEST_KIND,
                    SyncFailure.number == number,
                )
            )
        ).first()
        if stored is None:
            await connection.execute(
                insert(SyncFailure).values(
                    repository_id=repository_id,
                    item_kind=PULL_REQUEST_KIND,
                    number=number,
                    attempts=1,
                    last_error=error_text(error),
                    first_failed_at=failed_at,
                    last_failed_at=failed_at,
                )
            )
            return
        new_run = stored.last_failed_at < run_started_at
        await connection.execute(
            update(SyncFailure)
            .where(SyncFailure.id == stored.id)
            .values(
                attempts=stored.attempts + (1 if new_run else 0),
                last_error=error_text(error),
                last_failed_at=failed_at,
            )
        )


# --------------------------------------------------------------------------------------------
# Step 7: deletions and the prune
# --------------------------------------------------------------------------------------------
async def _merge_approval(connection: AsyncConnection, run_id: int, key: str, value: Any) -> None:
    current = (
        await connection.execute(select(SyncRun.deletions_approved).where(SyncRun.id == run_id))
    ).scalar_one()
    approved = json.loads(current) if current else {}
    if key == "prune_untracked":
        approved[key] = [*approved.get(key, []), *value]
    else:
        approved[key] = value
    await connection.execute(
        update(SyncRun)
        .where(SyncRun.id == run_id)
        .values(deletions_approved=json.dumps(approved, sort_keys=True, separators=(",", ":")))
    )


async def apply_item_deletions(
    connection: AsyncConnection,
    run_id: int,
    deletions: Deletions,
    *,
    approval: DeletionApproval | None = None,
) -> int:
    """Step 7 for one repository's items, in one transaction (5.3 step 7).

    When the guard lets them through, the candidates are deleted (their child rows and
    ``pr_user_tags`` by cascade); the ``sync_failures`` rows of items absent from the in-coverage
    set are deleted either way. The deleted rows add to ``sync_runs.items_deleted``, and an
    ``--allow-deletions`` approval naming this repository is recorded in ``deletions_approved``,
    in the same transaction. Returns the number of item rows deleted.

    Raises:
        ValueError: the approval's N is not the N the guard decided with; nothing is written.
    """
    decision = deletions.decision
    if approval is not None and decision.approved_limit not in (None, approval.limit):
        raise ValueError(
            f"approval N {approval.limit} is not the guard's {decision.approved_limit} for "
            f"repository {deletions.repository_github_id}"
        )
    async with transaction(connection, run_id):
        repository_id = (
            await connection.execute(
                select(Repository.id).where(Repository.github_id == deletions.repository_github_id)
            )
        ).scalar_one()
        deleted = 0
        if decision.runs and deletions.candidates:
            result = await connection.execute(
                delete(PullRequest).where(
                    PullRequest.repository_id == repository_id,
                    PullRequest.id.in_([candidate.row_id for candidate in deletions.candidates]),
                    PullRequest.github_id.in_(
                        [candidate.github_id for candidate in deletions.candidates]
                    ),
                )
            )
            deleted = result.rowcount
        for kind, number in deletions.failures_removed:
            await connection.execute(
                delete(SyncFailure).where(
                    SyncFailure.repository_id == repository_id,
                    SyncFailure.item_kind == kind,
                    SyncFailure.number == number,
                )
            )
        if deleted:
            await connection.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(items_deleted=SyncRun.items_deleted + deleted)
            )
        if approval is not None and decision.approved_limit is not None:
            await _merge_approval(
                connection,
                run_id,
                "allow_deletions",
                {
                    "repository": approval.repository.full_name,
                    "github_id": deletions.repository_github_id,
                    "approved": approval.limit,
                    "sets": {
                        "items": {"candidates": decision.candidates, "deleted": decision.runs}
                    },
                },
            )
    return deleted


@dataclass(frozen=True, slots=True)
class Pruned:
    github_id: int
    full_name: str
    items_deleted: int


async def prune_repositories(
    connection: AsyncConnection, run_id: int, github_ids: Iterable[int]
) -> tuple[Pruned, ...]:
    """``--prune-untracked``: delete untracked repositories and, by cascade, everything stored
    under them (``github_users`` stay), outside the delete guard; their items add to
    ``items_deleted`` and each is listed in ``deletions_approved``, in the same transaction (D27).

    Raises:
        ValueError: a repository named is tracked or not stored; nothing is deleted.
    """
    ids = sorted(set(github_ids))
    if not ids:
        return ()
    async with transaction(connection, run_id):
        rows = (
            await connection.execute(
                select(
                    Repository.id, Repository.github_id, Repository.full_name, Repository.is_tracked
                ).where(Repository.github_id.in_(ids))
            )
        ).all()
        by_id = {row.github_id: row for row in rows}
        refused = [
            github_id for github_id in ids if github_id not in by_id or by_id[github_id].is_tracked
        ]
        if refused:
            raise ValueError(f"prune refused for repositories not stored untracked: {refused}")
        pruned: list[Pruned] = []
        for github_id in ids:
            row = by_id[github_id]
            items = (
                await connection.execute(
                    select(func.count())
                    .select_from(PullRequest)
                    .where(PullRequest.repository_id == row.id)
                )
            ).scalar_one()
            await connection.execute(delete(Repository).where(Repository.id == row.id))
            pruned.append(Pruned(github_id, row.full_name, int(items)))
        total = sum(entry.items_deleted for entry in pruned)
        if total:
            await connection.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(items_deleted=SyncRun.items_deleted + total)
            )
        await _merge_approval(
            connection,
            run_id,
            "prune_untracked",
            [
                {
                    "github_id": entry.github_id,
                    "full_name": entry.full_name,
                    "items_deleted": entry.items_deleted,
                }
                for entry in pruned
            ],
        )
    return tuple(pruned)
