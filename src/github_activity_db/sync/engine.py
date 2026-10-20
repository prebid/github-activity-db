"""The sync engine: redesign plan 5.3 steps 1-8 over the source, the planner, and the writer.

``sync`` is the function ``ghactivity sync`` calls. A run that never starts raises before any
``sync_runs`` row: ``HeadCheckError``, ``ClaimRefused``, or a database error before the claim
returns. A run that starts returns its report, whose ``exit_code`` the command exits with, except
an interrupt, which closes the row ``aborted`` with 130 and is re-raised. A dry run does the head
check, then steps 1-3 as the run would, and writes nothing.

Each repository's step-8 outcome and the exit code are computed from one record of what happened
to it (``_Repository``), so the close, a dry run, and the report agree.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from github_activity_db.source import (
    PULL_REQUEST_KIND,
    Clock,
    DeadlineReached,
    Failed,
    GitHubSource,
    Gone,
    LinkedAccount,
    PullRequestIndex,
    PullRequestOutcome,
    PullRequestRecord,
    RepositoryError,
    RepositoryRecord,
    Usage,
    UserLookupFailed,
)
from github_activity_db.source.errors import IndexCountMismatch
from github_activity_db.sync import writer
from github_activity_db.sync.connection import open_reader, open_writer
from github_activity_db.sync.coverage import in_coverage
from github_activity_db.sync.deletions import DeletionApproval, Deletions, item_deletions
from github_activity_db.sync.plan import (
    AttemptedRepository,
    Batch,
    InPlaceUpdate,
    Plan,
    PlannedItem,
    plan,
)
from github_activity_db.sync.runs import (
    INDEXED_OUTCOMES,
    Claim,
    IndexState,
    LastOutcome,
    RepositoryOutcome,
    RunCounters,
    RunStatus,
    Trigger,
    check_head,
    claim_run,
    close_run,
)
from github_activity_db.sync.snapshot import Snapshot, StoredRepository, read_snapshot
from github_activity_db.sync.writer import (
    Found,
    LinkAccount,
    NotFound,
    Pruned,
    SecondNodeId,
    Sighted,
    error_text,
)
from github_activity_db.tracking import AcceptedFailure, TrackedRepository, TrackingConfigFile

# Plan 5.3 step 8; 130 is the code the CLI exits with on an interrupt.
EXIT_COMPLETE: Final = 0
EXIT_FATAL: Final = 1
EXIT_ATTENTION: Final = 2
EXIT_INTERRUPTED: Final = 130

# Opens the source with the run's deadline: ``GitHubSource(token, deadline=deadline)`` is one.
OpenSource = Callable[[datetime | None], AbstractAsyncContextManager[GitHubSource]]


class NoRepositoryResolved(Exception):
    """No configured repository resolved, and the deadline stopped none of the resolves: the run
    is fatal (5.3 step 1)."""


class HydratedIdMismatch(Exception):
    """Hydration returned another ``github_id`` than the index listed for the number (5.3 step
    5); the engine records it at once."""

    def __init__(self, number: int, indexed: int, hydrated: int) -> None:
        super().__init__(
            f"#{number}: the index listed github_id {indexed}, hydration returned {hydrated}"
        )


@dataclass(frozen=True, slots=True)
class SyncOptions:
    """A run's options (plan 5.7); ``ghactivity sync`` validates them before calling ``sync``."""

    database_url: str
    config: TrackingConfigFile
    trigger: Trigger
    code_version: str
    # --repo: the configured repositories the run attempts; None attempts every one.
    repositories: tuple[TrackedRepository, ...] | None = None
    max_minutes: float | None = None
    break_lock: bool = False
    allow_deletions: DeletionApproval | None = None
    prune_untracked: bool = False
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryReport:
    """One configured repository the run attempted.

    ``github_id`` and ``full_name`` are GitHub's, None when it did not resolve to a repository the
    run uses; ``stored_full_name`` is the stored row's name before the run, None when none was
    stored. ``outcome`` is what step 8 records (for a dry run, what the run would record), None
    when no stored row takes one. ``reason`` says why it failed or its index is incomplete.
    """

    configured: TrackedRepository
    github_id: int | None
    full_name: str | None
    stored_full_name: str | None
    outcome: LastOutcome | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ItemFailureReport:
    """An item failure of this run; ``accepted`` is its ``[sync].accepted_failures`` entry."""

    repository: str
    repository_github_id: int
    number: int
    error: str
    accepted: AcceptedFailure | None


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What a run did, or for a dry run what the run would do.

    ``status`` is the ``sync_runs`` row's status after the close (``running`` when the close
    failed), None for a dry run. ``untracked`` names the stored repositories step 8 records as
    untracked; ``pruned`` the repositories ``--prune-untracked`` deleted (for a dry run, would
    delete). ``accepted_not_failed`` lists the ``accepted_failures`` entries whose item did not
    fail in a repository whose outcome is ``complete``, ``time_limited``, or ``halted``.
    """

    dry_run: bool
    run_id: int | None
    status: str | None
    exit_code: int
    repositories: tuple[RepositoryReport, ...]
    untracked: tuple[str, ...]
    planned: tuple[PlannedItem, ...]
    in_place_updates: tuple[InPlaceUpdate, ...]
    deletions: tuple[Deletions, ...]
    pruned: tuple[Pruned, ...]
    prune_refused: str | None
    failures: tuple[ItemFailureReport, ...]
    accepted_not_failed: tuple[AcceptedFailure, ...]
    second_node_ids: tuple[SecondNodeId, ...]
    refresh_failures: tuple[str, ...]
    items_hydrated: int
    reclaimed: tuple[int, ...] = ()
    broken: tuple[int, ...] = ()
    error: str | None = None


async def sync(options: SyncOptions, *, open_source: OpenSource, clock: Clock) -> SyncReport:
    """Run the sync of plan 5.3, or with ``options.dry_run`` its steps 1-3 without writing.

    Raises:
        HeadCheckError: the database file is missing or not at head; nothing is written.
        ClaimRefused: another run holds the lock; nothing is written.
        LocalDatabaseError: the claim failed on the database (the busy timeout included).
        KeyboardInterrupt, asyncio.CancelledError: re-raised after the close.
    """
    await check_head(options.database_url)
    started_at = clock.now()
    deadline = (
        None if options.max_minutes is None else started_at + timedelta(minutes=options.max_minutes)
    )
    if options.dry_run:
        dry = _Sync(options, clock, deadline)
        try:
            async with open_source(deadline) as source:
                await dry.steps_1_to_3(source)
            await dry.delete(dry.snapshot)
            await dry.prune()
        except Exception as error:
            return dry.report(None, EXIT_FATAL, error_text(error))
        return dry.report(None, dry.exit_code())
    async with open_writer(options.database_url) as connection:
        claim = await claim_run(
            connection,
            started_at=started_at,
            deadline_at=deadline,
            break_lock=options.break_lock,
            config=options.config,
            trigger=options.trigger,
            code_version=options.code_version,
        )
        run = _Sync(options, clock, deadline, connection, claim)
        try:
            async with open_source(deadline) as source:
                await run.steps(source)
            await run.delete(await run.read_snapshot())
            await run.prune()
        except (KeyboardInterrupt, asyncio.CancelledError):
            await run.close_interrupted()
            raise
        except Exception as error:
            return await run.close("failed", EXIT_FATAL, run.failed_outcomes(), error_text(error))
        return await run.close(
            "time_limited" if run.deadline_reached else "complete",
            run.exit_code(),
            run.outcomes(),
        )


@dataclass(eq=False)
class _Repository:
    """A configured repository the run attempts, and what happened to it."""

    configured: TrackedRepository
    # The stored is_tracked row whose full_name equals the configured name, ignoring case,
    # before the run (5.3 step 1).
    stored: StoredRepository | None
    record: RepositoryRecord | None = None
    index: PullRequestIndex | None = None
    # Why it failed for the run (step 8's outcome failed).
    failed: str | None = None
    # Why its index is incomplete.
    incomplete: str | None = None
    # A later entry resolving to an earlier entry's github_id (gap 92): it writes nothing.
    duplicate: bool = False
    # Planned items it did not reach before the deadline.
    pending: set[int] = field(default_factory=set)
    # Step 7's result, when step 7 ran for it.
    deletions: Deletions | None = None

    @property
    def github_id(self) -> int | None:
        """The repository step 8 records; None when no stored row takes an outcome."""
        if self.duplicate:
            return None
        if self.record is not None:
            return self.record.github_id
        return self.stored.github_id if self.stored is not None else None

    @property
    def halted(self) -> bool:
        return self.deletions is not None and not self.deletions.decision.runs

    def outcome(self) -> LastOutcome | None:
        """The first that applies of failed, incomplete_index, halted, time_limited, complete."""
        if self.github_id is None:
            return None
        if self.failed is not None:
            return "failed"
        if self.index is None:
            return "incomplete_index"
        if self.halted:
            return "halted"
        if self.pending:
            return "time_limited"
        return "complete"

    def needs_attention(self) -> bool:
        """It makes the exit 2: it did not resolve, failed, was not completely indexed, or
        halted (5.3 step 8)."""
        return self.failed is not None or self.index is None or self.halted


class _Sync:
    """One run, or a dry run when there is no writer connection."""

    def __init__(
        self,
        options: SyncOptions,
        clock: Clock,
        deadline: datetime | None,
        connection: AsyncConnection | None = None,
        claim: Claim | None = None,
    ) -> None:
        self.options = options
        self.config = options.config.config
        self.clock = clock
        self.deadline = deadline
        self.connection = connection
        self.claim = claim
        self.snapshot = Snapshot(repositories=())
        self.repositories: list[_Repository] = []
        self.attempted: dict[int, AttemptedRepository] = {}
        self.plan = Plan(queue=(), batches=(), in_place_updates=(), failures=(), coverage=())
        self.deadline_reached = False
        self.usage = Usage()
        self.untracked: tuple[StoredRepository, ...] = ()
        self.pruned: tuple[Pruned, ...] = ()
        self.prune_refused: str | None = None
        self.failures: list[ItemFailureReport] = []
        self.second_node_ids: list[SecondNodeId] = []
        self.refresh_failures: list[str] = []
        self.items_hydrated = 0
        # Lookup answers by github_id: a link answered once is not looked up again in the run.
        self.answers: dict[int, Found | NotFound] = {}
        # Stored accounts with not_found_at set that a hydrated commit links (gap 90).
        self.not_found_links: set[int] = set()

    @property
    def run_id(self) -> int:
        assert self.claim is not None
        return self.claim.run_id

    def _writer(self) -> AsyncConnection:
        assert self.connection is not None
        return self.connection

    async def read_snapshot(self) -> Snapshot:
        async with open_reader(self.options.database_url) as connection:
            return await read_snapshot(connection)

    # ----------------------------------------------------------------------------------------
    # Steps 1-3
    # ----------------------------------------------------------------------------------------
    async def steps_1_to_3(self, source: GitHubSource) -> None:
        self.usage = source.usage
        self.snapshot = await self.read_snapshot()
        entries = (
            self.config.repositories
            if self.options.repositories is None
            else self.options.repositories
        )
        self.repositories = [
            _Repository(entry, _stored_named(self.snapshot, entry.full_name)) for entry in entries
        ]
        await self._resolve(source)
        marked, self.untracked = self._untracked()
        if self.connection is not None and marked:
            await writer.mark_untracked(self.connection, self.run_id, marked)
        await self._index(source)
        for repository in self.repositories:
            if repository.record is not None:
                self.attempted[repository.record.github_id] = AttemptedRepository.resolved(
                    repository.configured, repository.record, repository.index
                )
        self.plan = plan(self.snapshot, list(self.attempted.values()), self.config)
        for failure in self.plan.failures:
            await self._fail(
                self._by_id(failure.repository_github_id), failure.number, failure.error
            )

    async def _resolve(self, source: GitHubSource) -> None:
        """Step 1, in ``tracking.toml`` order."""
        resolved: dict[int, _Repository] = {}
        for repository in self.repositories:
            configured = repository.configured
            if self.deadline_reached:
                repository.incomplete = "the deadline came before its resolve"
                continue
            try:
                record = await source.resolve(configured.owner, configured.name)
            except DeadlineReached:
                self.deadline_reached = True
                repository.incomplete = "the deadline came before its resolve"
                continue
            except RepositoryError as error:
                repository.failed = error_text(error)
                continue
            earlier = resolved.get(record.github_id)
            if earlier is not None:
                repository.duplicate = True
                repository.failed = (
                    f"{configured.full_name} resolves to github_id {record.github_id}, as "
                    f"{earlier.configured.full_name} does"
                )
                continue
            stored = repository.stored
            if stored is not None and stored.github_id != record.github_id:
                repository.failed = (
                    f"{configured.full_name} resolves to github_id {record.github_id}; the stored "
                    f"repository {stored.full_name} has github_id {stored.github_id}"
                )
                continue
            if self.connection is None:
                holder = writer.full_name_conflict(
                    ((s.github_id, s.full_name, s.is_tracked) for s in self.snapshot.repositories),
                    record.github_id,
                    record.full_name,
                )
                if holder is not None:
                    repository.failed = error_text(writer.RepositoryConflict(record, holder))
                    continue
            else:
                try:
                    await writer.write_repository(
                        self.connection, self.run_id, record, group=configured.group
                    )
                except writer.RepositoryConflict as error:
                    repository.failed = error_text(error)
                    continue
            repository.record = record
            resolved[record.github_id] = repository
        if not resolved and not self.deadline_reached:
            raise NoRepositoryResolved("no configured repository resolved")

    def _resolved_ids(self) -> set[int]:
        return {r.record.github_id for r in self.repositories if r.record is not None}

    def _untracked(self) -> tuple[list[int], tuple[StoredRepository, ...]]:
        """The stored repositories step 1 marks untracked, and every one step 8 records as
        untracked; neither under ``--repo`` (D27)."""
        if self.options.repositories is not None:
            return [], ()
        resolved = self._resolved_ids()
        every = all(repository.record is not None for repository in self.repositories)
        marked = [
            stored.github_id
            for stored in self.snapshot.repositories
            if every and stored.is_tracked and stored.github_id not in resolved
        ]
        untracked = tuple(
            stored
            for stored in self.snapshot.repositories
            if stored.github_id not in resolved
            and (not stored.is_tracked or stored.github_id in marked)
        )
        return marked, untracked

    async def _index(self, source: GitHubSource) -> None:
        """Step 2, in the same order."""
        for repository in self.repositories:
            if repository.record is None:
                continue
            if self.deadline_reached:
                repository.incomplete = "the deadline came before its index finished"
                continue
            try:
                repository.index = await source.index_pull_requests(repository.record)
            except DeadlineReached:
                self.deadline_reached = True
                repository.incomplete = "the deadline came before its index finished"
            except IndexCountMismatch as error:
                repository.incomplete = error_text(error)
            except RepositoryError as error:
                repository.failed = error_text(error)

    def _by_id(self, github_id: int) -> _Repository:
        for repository in self.repositories:
            if repository.record is not None and repository.record.github_id == github_id:
                return repository
        raise KeyError(github_id)

    async def _fail(self, repository: _Repository, number: int, error: BaseException) -> None:
        """Record an item failure (5.3 step 4); a dry run only reports it."""
        github_id = repository.github_id
        assert github_id is not None
        if self.connection is not None:
            await writer.record_failure(
                self.connection,
                self.run_id,
                repository_github_id=github_id,
                number=number,
                error=error,
                failed_at=self.clock.now(),
            )
        self.failures.append(
            ItemFailureReport(
                repository=repository.configured.full_name,
                repository_github_id=github_id,
                number=number,
                error=error_text(error),
                accepted=self.config.accepted_failure(repository.configured, number),
            )
        )

    # ----------------------------------------------------------------------------------------
    # Steps 4-6 and the refresh
    # ----------------------------------------------------------------------------------------
    async def steps(self, source: GitHubSource) -> None:
        """Steps 1-6 and the refresh."""
        await self.steps_1_to_3(source)
        await writer.apply_in_place_updates(self._writer(), self.run_id, self.plan.in_place_updates)
        for batch in self.plan.batches:
            await self._batch(source, batch)
        await self._refresh(source)

    async def _batch(self, source: GitHubSource, batch: Batch) -> None:
        """Hydrate one batch, check it, look up its accounts, and write each item (5.3 step 6)."""
        repository = self._by_id(batch.repository_github_id)
        if repository.failed is not None:
            return
        if self.deadline_reached:
            repository.pending.update(batch.numbers)
            return
        assert repository.record is not None
        outcomes: list[PullRequestOutcome] = []
        try:
            async for group in source.hydrate_pull_requests(repository.record, batch.numbers):
                outcomes.extend(group)
        except DeadlineReached:
            self.deadline_reached = True
            repository.pending.update(batch.numbers)
            return
        except RepositoryError as error:
            # Gone during hydration (gap 79): its written items and sync_pending rows stay.
            repository.failed = error_text(error)
            return
        planned = {item.number: item for item in batch.items}
        records: list[PullRequestRecord] = []
        for outcome in outcomes:
            if isinstance(outcome, Gone):
                continue
            if isinstance(outcome, Failed):
                await self._fail(repository, outcome.number, outcome.error)
                continue
            record = outcome.record
            indexed = planned[record.number].github_id
            if record.github_id != indexed:
                await self._fail(
                    repository,
                    record.number,
                    HydratedIdMismatch(record.number, indexed, record.github_id),
                )
            elif in_coverage(record, self.config.coverage.floor):
                records.append(record)
        await self._write(source, repository, records)

    async def _write(
        self, source: GitHubSource, repository: _Repository, records: list[PullRequestRecord]
    ) -> None:
        """Look up the accounts the batch's commits link that are neither stored, nor answered
        earlier in the run, nor sighted in the batch; then write each item (5.2)."""
        connection = self._writer()
        repository_github_id = repository.github_id
        assert repository_github_id is not None
        links = {link.github_id: link for record in records for link in record.commit_links()}
        stored = await writer.stored_accounts(connection, links)
        self.not_found_links.update(
            github_id for github_id, account in stored.items() if account.not_found_at is not None
        )
        sighted = {
            account.github_id: Sighted(account, record.hydrated_at)
            for record in records
            for account in record.accounts()
            if account.github_id is not None
        }
        failed: dict[int, tuple[UserLookupFailed, frozenset[int]]] = {}
        try:
            failed = await self._look_up(
                source,
                [
                    link
                    for github_id, link in links.items()
                    if github_id not in stored
                    and github_id not in sighted
                    and github_id not in self.answers
                ],
            )
            for record in records:
                own = self._unanswered(record, stored, sighted)
                own_ids = {link.github_id for link in own}
                # One more lookup holding only its own ids, unless the failed request held only
                # those (5.3 step 4).
                retry = [
                    link
                    for link in own
                    if link.github_id in failed and not failed[link.github_id][1] <= own_ids
                ]
                for link in retry:
                    del failed[link.github_id]
                failed.update(await self._look_up(source, retry))
        except DeadlineReached:
            self.deadline_reached = True
        for record in records:
            missing = self._unanswered(record, stored, sighted)
            lookup_failed = [
                failed[link.github_id][0] for link in missing if link.github_id in failed
            ]
            if lookup_failed:
                await self._fail(repository, record.number, lookup_failed[0])
                continue
            if missing and self.deadline_reached:
                repository.pending.add(record.number)
                continue
            given: dict[int, LinkAccount] = {}
            own_sightings = {account.github_id for account in record.accounts()}
            for link in record.commit_links():
                github_id = link.github_id
                if github_id in stored or github_id in own_sightings:
                    continue
                answer = sighted.get(github_id) or self.answers.get(github_id)
                if answer is not None:
                    given[github_id] = answer
            try:
                written = await writer.write_pull_request(
                    connection,
                    self.run_id,
                    record,
                    repository_github_id=repository_github_id,
                    links=given,
                )
            except (writer.ItemConflict, writer.AccountMissing) as error:
                await self._fail(repository, record.number, error)
                continue
            self.items_hydrated += 1
            self.second_node_ids.extend(written.second_node_ids)

    def _unanswered(
        self,
        record: PullRequestRecord,
        stored: dict[int, writer.StoredAccount],
        sighted: dict[int, Sighted],
    ) -> list[LinkedAccount]:
        """The record's commit links with no account to write: none stored, sighted in the batch,
        or answered by a lookup."""
        distinct = {link.github_id: link for link in record.commit_links()}
        return [
            link
            for github_id, link in distinct.items()
            if github_id not in stored
            and github_id not in sighted
            and github_id not in self.answers
        ]

    async def _look_up(
        self, source: GitHubSource, links: Iterable[LinkedAccount]
    ) -> dict[int, tuple[UserLookupFailed, frozenset[int]]]:
        """Look up the links' accounts; answers go to ``self.answers``. Returns each failure by
        ``github_id``, with the ids its request held."""
        by_node = {link.node_id: link.github_id for link in links}
        failed: dict[int, tuple[UserLookupFailed, frozenset[int]]] = {}
        if not by_node:
            return failed
        async for lookup in source.lookup_users(list(by_node)):
            held = frozenset(by_node[node_id] for node_id in (*lookup.accounts, *lookup.failed))
            for node_id, account in lookup.accounts.items():
                assert lookup.looked_up_at is not None
                self.answers[by_node[node_id]] = (
                    NotFound(lookup.looked_up_at)
                    if account is None
                    else Found(account, lookup.looked_up_at)
                )
            for node_id, error in lookup.failed.items():
                failed[by_node[node_id]] = (error, held)
        return failed

    async def _refresh(self, source: GitHubSource) -> None:
        """Accounts last refreshed more than 24 hours ago, and the not-found accounts a hydrated
        commit links (gap 90), oldest first; a failure keeps the stored values (5.2)."""
        if self.deadline_reached:
            return
        connection = self._writer()
        targets = await writer.accounts_to_refresh(
            connection, now=self.clock.now(), not_found=self.not_found_links
        )
        expected = {target.node_id: target.github_id for target in targets}
        try:
            async for lookup in source.lookup_users(list(expected)):
                for node_id, account in lookup.accounts.items():
                    assert lookup.looked_up_at is not None
                    result = (
                        NotFound(lookup.looked_up_at)
                        if account is None
                        else Found(account, lookup.looked_up_at)
                    )
                    try:
                        written = await writer.write_lookup(
                            connection,
                            self.run_id,
                            node_id,
                            result,
                            expected_github_id=expected[node_id],
                        )
                    except writer.LookupConflict as error:
                        self.refresh_failures.append(error_text(error))
                        continue
                    self.second_node_ids.extend(written.second_node_ids)
                self.refresh_failures.extend(error_text(error) for error in lookup.failed.values())
        except DeadlineReached:
            self.deadline_reached = True

    # ----------------------------------------------------------------------------------------
    # Step 7
    # ----------------------------------------------------------------------------------------
    async def delete(self, current: Snapshot) -> None:
        """Step 7's item deletions over ``current``: the stored rows after the run's writes, or
        for a dry run the snapshot, which shows the candidates the run would delete."""
        for repository in self.repositories:
            if repository.record is None:
                continue
            github_id = repository.record.github_id
            before = self.snapshot.repository(github_id)
            deletions = item_deletions(
                current.repository(github_id),
                self.plan.coverage_of(github_id, PULL_REQUEST_KIND),
                attempted=self.attempted[github_id],
                failed_during_hydration=repository.failed is not None,
                stored_rows_before_run=before.item_rows if before is not None else 0,
                config=self.config.sync,
                approval=self.options.allow_deletions,
            )
            if deletions is None:
                continue
            if self.connection is not None:
                await writer.apply_item_deletions(self.connection, self.run_id, deletions)
            repository.deletions = deletions

    async def prune(self) -> None:
        """``--prune-untracked`` (D27): refused under ``--repo`` or when an entry did not
        resolve."""
        if not self.options.prune_untracked:
            return
        if self.options.repositories is not None:
            self.prune_refused = "--prune-untracked is refused with --repo"
            return
        unresolved = [r.configured.full_name for r in self.repositories if r.record is None]
        if unresolved:
            self.prune_refused = (
                "--prune-untracked is refused: configured repositories did not resolve: "
                + ", ".join(unresolved)
            )
            return
        resolved = self._resolved_ids()
        targets = [
            stored
            for stored in self.snapshot.repositories
            if not stored.is_tracked and stored.github_id not in resolved
        ]
        if self.connection is not None:
            self.pruned = await writer.prune_repositories(
                self.connection, self.run_id, [stored.github_id for stored in targets]
            )
        else:
            self.pruned = tuple(Pruned(s.github_id, s.full_name, s.item_rows) for s in targets)
        gone = {entry.github_id for entry in self.pruned}
        self.untracked = tuple(stored for stored in self.untracked if stored.github_id not in gone)

    # ----------------------------------------------------------------------------------------
    # Step 8
    # ----------------------------------------------------------------------------------------
    def _unaccepted(self, repository: _Repository | None = None) -> list[ItemFailureReport]:
        return [
            failure
            for failure in self.failures
            if failure.accepted is None
            and (repository is None or failure.repository_github_id == repository.github_id)
        ]

    def exit_code(self) -> int:
        """2 when a repository needs attention, a stored repository is untracked, a prune was
        refused, or an item failed outside ``accepted_failures``; else 0 (5.3 step 8)."""
        if (
            any(repository.needs_attention() for repository in self.repositories)
            or self.untracked
            or self.prune_refused is not None
            or self._unaccepted()
        ):
            return EXIT_ATTENTION
        return EXIT_COMPLETE

    def outcomes(self) -> list[RepositoryOutcome]:
        """Step 8's record of every repository the run attempted and every untracked one."""
        result: list[RepositoryOutcome] = []
        for repository in self._recorded():
            outcome = repository.outcome()
            assert outcome is not None and repository.github_id is not None
            indexed = outcome in INDEXED_OUTCOMES and repository.index is not None
            result.append(
                RepositoryOutcome(
                    repository_github_id=repository.github_id,
                    last_outcome=outcome,
                    index=(
                        IndexState(repository.index.started_at, repository.index.total_count)
                        if indexed and repository.index is not None
                        else None
                    ),
                    step_7_ran=repository.deletions is not None,
                    halt_reason=_halt_reason(repository),
                    pending=tuple(sorted(repository.pending)) if indexed else None,
                    succeeded=outcome == "complete" and not self._unaccepted(repository),
                )
            )
        result.extend(
            RepositoryOutcome(repository_github_id=stored.github_id, last_outcome="untracked")
            for stored in self.untracked
        )
        return result

    def failed_outcomes(self) -> list[RepositoryOutcome]:
        """A fatal or interrupted close: ``failed`` for every repository the run attempted, with
        step 7's result where it ran (5.3 step 8)."""
        return [
            RepositoryOutcome(
                repository_github_id=repository.github_id,
                last_outcome="failed",
                step_7_ran=repository.deletions is not None,
                halt_reason=_halt_reason(repository),
            )
            for repository in self._recorded()
            if repository.github_id is not None
        ]

    def _recorded(self) -> list[_Repository]:
        """The attempted repositories that take an outcome, each stored row once."""
        seen: set[int] = set()
        result: list[_Repository] = []
        for repository in self.repositories:
            github_id = repository.github_id
            if github_id is not None and github_id not in seen:
                seen.add(github_id)
                result.append(repository)
        return result

    async def close(
        self,
        status: RunStatus,
        exit_code: int,
        outcomes: list[RepositoryOutcome],
        error: str | None = None,
    ) -> SyncReport:
        """Close the row; a close that fails leaves it ``running`` and exits 1."""
        try:
            closed = await close_run(
                self._writer(),
                self.run_id,
                status=status,
                exit_code=exit_code,
                finished_at=self.clock.now(),
                counters=self._counters(),
                outcomes=outcomes,
            )
        except Exception as close_error:
            message = f"the close failed, so run {self.run_id} stays running: "
            return self.report(
                "running", EXIT_FATAL, _joined(error, message + error_text(close_error))
            )
        if not closed.closed:
            message = f"run {self.run_id} is {closed.status}: another process ended it"
            return self.report(closed.status, EXIT_FATAL, _joined(error, message))
        return self.report(status, exit_code, error)

    async def close_interrupted(self) -> None:
        """Close the row ``aborted`` with 130; the interrupt is re-raised whatever this finds."""
        with contextlib.suppress(Exception):
            await close_run(
                self._writer(),
                self.run_id,
                status="aborted",
                exit_code=EXIT_INTERRUPTED,
                finished_at=self.clock.now(),
                counters=self._counters(),
                outcomes=self.failed_outcomes(),
            )

    def _counters(self) -> RunCounters:
        return RunCounters(
            graphql_points=self.usage.graphql_points,
            rest_calls=self.usage.rest_calls,
            items_planned=len(self.plan.queue),
            items_hydrated=self.items_hydrated,
            failures=len(self.failures),
        )

    def _accepted_not_failed(self) -> list[AcceptedFailure]:
        result: list[AcceptedFailure] = []
        for entry in self.config.sync.accepted_failures:
            repository = next(
                (
                    r
                    for r in self.repositories
                    if r.configured.full_name.lower() == entry.repository.lower()
                ),
                None,
            )
            if repository is None or repository.outcome() not in INDEXED_OUTCOMES:
                continue
            failed = any(
                failure.repository_github_id == repository.github_id
                and failure.number == entry.number
                for failure in self.failures
            )
            if not failed and entry.number not in repository.pending:
                result.append(entry)
        return result

    def report(self, status: str | None, exit_code: int, error: str | None = None) -> SyncReport:
        return SyncReport(
            dry_run=self.connection is None,
            run_id=self.claim.run_id if self.claim is not None else None,
            status=status,
            exit_code=exit_code,
            repositories=tuple(
                RepositoryReport(
                    configured=r.configured,
                    github_id=r.record.github_id if r.record is not None else None,
                    full_name=r.record.full_name if r.record is not None else None,
                    stored_full_name=r.stored.full_name if r.stored is not None else None,
                    outcome=r.outcome(),
                    reason=r.failed or r.incomplete,
                )
                for r in self.repositories
            ),
            untracked=tuple(stored.full_name for stored in self.untracked),
            planned=self.plan.queue,
            in_place_updates=self.plan.in_place_updates,
            deletions=tuple(r.deletions for r in self.repositories if r.deletions is not None),
            pruned=self.pruned,
            prune_refused=self.prune_refused,
            failures=tuple(self.failures),
            accepted_not_failed=tuple(self._accepted_not_failed()),
            second_node_ids=tuple(self.second_node_ids),
            refresh_failures=tuple(self.refresh_failures),
            items_hydrated=self.items_hydrated,
            reclaimed=self.claim.reclaimed if self.claim is not None else (),
            broken=self.claim.broken if self.claim is not None else (),
            error=error,
        )


def _stored_named(snapshot: Snapshot, full_name: str) -> StoredRepository | None:
    name = full_name.lower()
    for stored in snapshot.repositories:
        if stored.is_tracked and stored.full_name.lower() == name:
            return stored
    return None


def _halt_reason(repository: _Repository) -> str | None:
    return repository.deletions.decision.halt_reason if repository.deletions is not None else None


def _joined(first: str | None, second: str) -> str:
    return second if first is None else f"{first}; {second}"
