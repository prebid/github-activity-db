"""Run records: the head check, the run claim and its lock, and the close (redesign plan 5.3).

The engine passes every time it stamps; nothing here reads the clock.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Final, Literal

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection

from github_activity_db.db.models import (
    AutomationAccount,
    AutomationEmail,
    Repository,
    RepoSyncState,
    SyncPending,
    SyncRun,
)
from github_activity_db.db.schema import HEAD_REVISION, SCHEMA_VERSION
from github_activity_db.source.documents import PULL_REQUEST_KIND
from github_activity_db.sync.connection import open_reader, transaction
from github_activity_db.tracking import TrackingConfigFile

# A running row whose deadline_at is this far past no longer blocks a claim (plan 5.3).
STALE_AFTER: Final = timedelta(minutes=10)

# Bounds the git calls code_version makes (plan Step 3: every subprocess wait has a timeout).
GIT_TIMEOUT_SECONDS: Final = 5.0

RunStatus = Literal["complete", "time_limited", "failed", "aborted"]
LastOutcome = Literal[
    "complete", "time_limited", "failed", "incomplete_index", "halted", "untracked"
]
Trigger = Literal["schedule", "workflow_dispatch", "manual"]


class HeadCheckError(Exception):
    """The database file is missing or is not at the code's migration head (gap 74)."""


class ClaimRefused(Exception):
    """Another run holds the lock (plan 5.3): nothing was inserted."""

    def __init__(self, blocking: tuple[int, ...]) -> None:
        shown = ", ".join(str(run_id) for run_id in blocking)
        super().__init__(f"run {shown} is running; --break-lock marks it aborted")
        self.blocking = blocking


def database_path(database_url: str) -> Path:
    """The file an SQLite database URL names.

    Raises:
        HeadCheckError: the URL names no file (another dialect, or an in-memory database).
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        raise HeadCheckError(f"the sync writes SQLite files only, not {url.drivername}")
    if not url.database or url.database == ":memory:":
        raise HeadCheckError("the database URL names no file")
    return Path(url.database)


async def check_head(database_url: str) -> None:
    """Refuse a missing file before connecting (a connect would create it), then read the
    migration revision and ``PRAGMA user_version`` through a connection that takes no lock.

    Raises:
        HeadCheckError: the file is missing, has no migration revision, is at another revision,
            or its ``user_version`` is not ``SCHEMA_VERSION``.
    """
    path = database_path(database_url)
    if not path.is_file():
        raise HeadCheckError(
            f"database file not found: {path}; build it with `alembic upgrade head`"
        )
    async with open_reader(database_url) as connection, transaction(connection):
        await check_head_state(connection)


async def check_head_state(connection: AsyncConnection) -> None:
    """The head check on an open connection; the claim runs it again inside its transaction."""
    has_revision = (
        await connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'")
        )
    ).first()
    if has_revision is None:
        raise HeadCheckError(
            "the database has no migration revision; build it with `alembic upgrade head`"
        )
    revisions = [
        row[0]
        for row in (await connection.execute(text("SELECT version_num FROM alembic_version")))
    ]
    if revisions != [HEAD_REVISION]:
        raise HeadCheckError(
            f"the database is at revision {', '.join(revisions) or 'none'}, not the code's head "
            f"{HEAD_REVISION}; run `alembic upgrade head`"
        )
    user_version = (await connection.exec_driver_sql("PRAGMA user_version")).scalar_one()
    if user_version != SCHEMA_VERSION:
        raise HeadCheckError(
            f"PRAGMA user_version is {user_version}, not schema_version {SCHEMA_VERSION}"
        )


@dataclass(frozen=True, slots=True)
class Claim:
    """A claimed run: its id, and the running rows the claim marked ``aborted``."""

    run_id: int
    # Running rows whose deadline_at was 10 or more minutes past.
    reclaimed: tuple[int, ...]
    # Blocking rows --break-lock marked aborted; empty when it found none.
    broken: tuple[int, ...]


async def claim_run(
    connection: AsyncConnection,
    *,
    started_at: datetime,
    deadline_at: datetime | None,
    break_lock: bool,
    config: TrackingConfigFile,
    trigger: Trigger,
    code_version: str,
) -> Claim:
    """Claim ``sync_runs`` in one ``BEGIN IMMEDIATE`` transaction (plan 5.3).

    A ``running`` row blocks while its ``deadline_at`` is null or less than 10 minutes past;
    one 10 or more minutes past is marked ``aborted``, as are blocking rows with ``break_lock``.
    The claim re-checks the head inside the transaction, inserts the run's row, and replaces the
    automation tables from ``config``. Aborted rows keep a null ``exit_code`` and
    ``finished_at``: they never closed.

    Raises:
        ClaimRefused: a blocking row remains; nothing is written, stale rows included.
        HeadCheckError: the head check failed; nothing is written.
    """
    async with transaction(connection):
        await check_head_state(connection)
        running = (
            await connection.execute(
                select(SyncRun.id, SyncRun.deadline_at)
                .where(SyncRun.status == "running")
                .order_by(SyncRun.id)
            )
        ).all()
        stale = tuple(
            row.id
            for row in running
            if row.deadline_at is not None and row.deadline_at <= started_at - STALE_AFTER
        )
        blocking = tuple(row.id for row in running if row.id not in stale)
        if blocking and not break_lock:
            raise ClaimRefused(blocking)
        aborted = stale + blocking
        if aborted:
            await connection.execute(
                update(SyncRun)
                .where(SyncRun.id.in_(aborted), SyncRun.status == "running")
                .values(status="aborted")
            )
        result = await connection.execute(
            insert(SyncRun).values(
                started_at=started_at,
                deadline_at=deadline_at,
                status="running",
                trigger=trigger,
                code_version=code_version,
                config_source=config.source,
                config_sha256=config.sha256,
                graphql_points=0,
                rest_calls=0,
                items_planned=0,
                items_hydrated=0,
                items_deleted=0,
                failures=0,
            )
        )
        assert result.inserted_primary_key is not None
        run_id = int(result.inserted_primary_key[0])
        automation = config.config.automation
        await connection.execute(delete(AutomationAccount))
        await connection.execute(delete(AutomationEmail))
        if automation.accounts:
            await connection.execute(
                insert(AutomationAccount),
                [
                    {
                        "github_id": account.github_id,
                        "login": account.login,
                        "reason": account.reason,
                        "run_id": run_id,
                    }
                    for account in automation.accounts
                ],
            )
        if automation.emails:
            await connection.execute(
                insert(AutomationEmail),
                [
                    {"email": entry.email, "reason": entry.reason, "run_id": run_id}
                    for entry in automation.emails
                ],
            )
    return Claim(run_id=run_id, reclaimed=stale, broken=blocking)


@dataclass(frozen=True, slots=True)
class RunCounters:
    """The internal run counters the close writes; ``items_deleted`` grows with each deletion."""

    graphql_points: int
    rest_calls: int
    items_planned: int
    items_hydrated: int
    failures: int


@dataclass(frozen=True, slots=True)
class IndexState:
    """A completed index the run did not later fail (plan 5.1 ``repo_sync_state``)."""

    started_at: datetime
    pr_total: int


@dataclass(frozen=True, slots=True)
class RepositoryOutcome:
    """Step 8's record for one repository.

    ``index`` writes ``last_index_started_at`` and ``index_pr_total``; None leaves them.
    ``halt_reason`` applies only when ``step_7_ran``: the reason when it halted, None when it did
    not; a repository whose step 7 did not run keeps its stored value. ``pending`` replaces the
    repository's pull-request ``sync_pending`` rows; None leaves them (a repository that failed
    later in the run, or was not completely indexed). ``succeeded`` writes ``last_success_at``.
    """

    repository_github_id: int
    last_outcome: LastOutcome
    index: IndexState | None = None
    step_7_ran: bool = False
    halt_reason: str | None = None
    pending: tuple[int, ...] | None = None
    succeeded: bool = False


@dataclass(frozen=True, slots=True)
class CloseResult:
    """Whether this close wrote the row; when not, the status and exit code the row holds.

    A row another process aborted reads ``aborted``; a row this run's own earlier close already
    wrote (an interrupt arriving after that ``COMMIT``) reads that close's status.
    """

    closed: bool
    status: str | None
    exit_code: int | None
    # Outcomes for repositories no longer stored (a prune deleted them); nothing was written.
    skipped: tuple[int, ...] = ()


class _NotRunning(Exception):
    pass


# The outcomes of a repository whose index completed and that did not fail later in the run.
INDEXED_OUTCOMES: frozenset[LastOutcome] = frozenset({"complete", "time_limited", "halted"})


async def close_run(
    connection: AsyncConnection,
    run_id: int,
    *,
    status: RunStatus,
    exit_code: int,
    finished_at: datetime,
    counters: RunCounters,
    outcomes: Sequence[RepositoryOutcome],
) -> CloseResult:
    """Step 8 and the close in one transaction, written only while the row is ``running``.

    Each outcome writes ``last_run_id`` and ``last_outcome`` and the columns it carries for a
    repository still stored. Only a ``complete``, ``time_limited``, or ``halted`` repository
    carries an index and pending items: one that failed, was not completely indexed, or is
    untracked leaves ``last_index_started_at``, ``index_pr_total``, and ``sync_pending`` as they
    were (plan 5.1). A fatal or interrupted close passes ``failed`` outcomes carrying at most
    step 7's result, so a halt step 7 reached, or its clearing, is kept beside the deletions that
    step committed (plan 5.1 ``halt_reason``, 5.3 step 8).

    Raises:
        ValueError: an outcome names a repository twice, marks success without ``complete``,
            or carries an index or pending items with an outcome other than ``complete``,
            ``time_limited``, or ``halted``; or a ``failed`` or ``aborted`` close carries an
            outcome other than ``failed``.
    """
    ids = [outcome.repository_github_id for outcome in outcomes]
    if len(set(ids)) != len(ids):
        raise ValueError(f"outcomes name a repository twice: {ids}")
    for outcome in outcomes:
        if outcome.succeeded and outcome.last_outcome != "complete":
            raise ValueError(
                f"repository {outcome.repository_github_id}: success needs last_outcome complete"
            )
        if (
            outcome.index is not None or outcome.pending is not None
        ) and outcome.last_outcome not in INDEXED_OUTCOMES:
            raise ValueError(
                f"repository {outcome.repository_github_id}: a {outcome.last_outcome} repository "
                "writes no index and no pending items (plan 5.1)"
            )
        if status in ("failed", "aborted") and outcome.last_outcome != "failed":
            raise ValueError(
                f"repository {outcome.repository_github_id}: a {status} close writes "
                "last_outcome failed (plan 5.3 step 8)"
            )
    try:
        async with transaction(connection):
            updated = await connection.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id, SyncRun.status == "running")
                .values(
                    status=status,
                    exit_code=exit_code,
                    finished_at=finished_at,
                    graphql_points=counters.graphql_points,
                    rest_calls=counters.rest_calls,
                    items_planned=counters.items_planned,
                    items_hydrated=counters.items_hydrated,
                    failures=counters.failures,
                )
            )
            if updated.rowcount == 0:
                raise _NotRunning
            skipped = await _write_outcomes(connection, run_id, outcomes, finished_at)
    except _NotRunning:
        async with transaction(connection):
            row = (
                await connection.execute(
                    select(SyncRun.status, SyncRun.exit_code).where(SyncRun.id == run_id)
                )
            ).first()
        return CloseResult(
            closed=False,
            status=row.status if row is not None else None,
            exit_code=row.exit_code if row is not None else None,
        )
    return CloseResult(closed=True, status=status, exit_code=exit_code, skipped=skipped)


async def _write_outcomes(
    connection: AsyncConnection,
    run_id: int,
    outcomes: Sequence[RepositoryOutcome],
    finished_at: datetime,
) -> tuple[int, ...]:
    ids = [outcome.repository_github_id for outcome in outcomes]
    stored = dict(
        (
            await connection.execute(
                select(Repository.github_id, Repository.id).where(Repository.github_id.in_(ids))
            )
        )
        .tuples()
        .all()
    )
    skipped: list[int] = []
    for outcome in outcomes:
        repository_id = stored.get(outcome.repository_github_id)
        if repository_id is None:
            skipped.append(outcome.repository_github_id)
            continue
        values: dict[str, object] = {"last_run_id": run_id, "last_outcome": outcome.last_outcome}
        if outcome.index is not None:
            values["last_index_started_at"] = outcome.index.started_at
            values["index_pr_total"] = outcome.index.pr_total
        if outcome.step_7_ran:
            values["halt_reason"] = outcome.halt_reason
        if outcome.succeeded:
            values["last_success_at"] = finished_at
        await connection.execute(
            sqlite_insert(RepoSyncState)
            .values(repository_id=repository_id, **values)
            .on_conflict_do_update(index_elements=["repository_id"], set_=values)
        )
        if outcome.pending is not None:
            await connection.execute(
                delete(SyncPending).where(
                    SyncPending.repository_id == repository_id,
                    SyncPending.item_kind == PULL_REQUEST_KIND,
                )
            )
            if outcome.pending:
                await connection.execute(
                    insert(SyncPending),
                    [
                        {
                            "repository_id": repository_id,
                            "item_kind": PULL_REQUEST_KIND,
                            "number": number,
                            "run_id": run_id,
                        }
                        for number in sorted(set(outcome.pending))
                    ],
                )
    return tuple(skipped)


def trigger(environ: Mapping[str, str]) -> Trigger:
    """``schedule`` or ``workflow_dispatch`` from ``GITHUB_EVENT_NAME``, else ``manual``."""
    event_name = environ.get("GITHUB_EVENT_NAME")
    if event_name == "schedule":
        return "schedule"
    if event_name == "workflow_dispatch":
        return "workflow_dispatch"
    return "manual"


def code_version(package_dir: Path, environ: Mapping[str, str]) -> str:
    """``GITHUB_SHA``; else the commit of the git checkout the package runs from, with
    ``-dirty`` when its tracked files have changes; else the package version (plan 5.1).

    The package runs from a checkout when git tracks its ``__init__.py``, so an install into an
    ignored directory inside a checkout reports the package version.
    """
    sha = environ.get("GITHUB_SHA")
    if sha:
        return sha
    tracked = _git(package_dir, "ls-files", "--error-unmatch", "__init__.py")
    head = _git(package_dir, "rev-parse", "HEAD") if tracked is not None else None
    if head is None:
        return metadata.version("github-activity-db")
    changes = _git(package_dir, "status", "--porcelain", "--untracked-files=no")
    return f"{head.strip()}-dirty" if changes else head.strip()


def _git(directory: Path, *args: str) -> str | None:
    """git's output, or None when git is missing, fails, or exceeds the timeout."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        # A fixed argument list naming git by its resolved path; no shell.
        result = subprocess.run(  # noqa: S603
            [git, "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None
