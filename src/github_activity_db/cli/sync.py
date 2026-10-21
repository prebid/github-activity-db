"""`ghactivity sync` (redesign plan 5.3, 5.7): a run of the engine, or with --dry-run its plan.

Every usage error exits 1, as a run that never starts does (gap 69): ``ExitCodeGroup`` turns a
``click.UsageError`` into that, and a failure before the claim (a missing or unmigrated database
file, a refused claim, a database error) is printed and exits 1. A run that starts prints its
report and exits with the engine's code: 0, 2, or 1 (5.3 step 8).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console

import github_activity_db
from github_activity_db.cli import common
from github_activity_db.cli.common import console, run_async_command
from github_activity_db.config import get_settings
from github_activity_db.source import RepositoryRecord
from github_activity_db.sync import engine, runs
from github_activity_db.sync.deletions import AllowDeletionsError, parse_allow_deletions
from github_activity_db.sync.engine import RepositoryReport, SyncOptions, SyncReport
from github_activity_db.tracking import TrackedRepository, TrackingConfig


def sync(
    repo: Annotated[
        list[str] | None,
        typer.Option(
            "--repo",
            "-r",
            metavar="OWNER/NAME",
            help="Sync only this configured repository; give it once per repository.",
        ),
    ] = None,
    max_minutes: Annotated[
        list[int] | None,
        typer.Option(
            "--max-minutes",
            min=1,
            metavar="N",
            help="Issue no request after N minutes; the run records what it did not reach.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Resolve, index, and plan; write nothing and take no lock."),
    ] = False,
    allow_deletions: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-deletions",
            metavar="OWNER/NAME=N",
            help="Delete all of this configured repository's deletion candidates when there "
            "are at most N, past the delete guard.",
        ),
    ] = None,
    prune_untracked: Annotated[
        bool,
        typer.Option(
            "--prune-untracked",
            help="Delete the repositories already untracked when the run starts.",
        ),
    ] = False,
    break_lock: Annotated[
        bool,
        typer.Option("--break-lock", help="Mark a running sync aborted and claim the run."),
    ] = False,
) -> None:
    """Sync the tracked repositories into the database (redesign plan 5.3)."""
    minutes = _once("--max-minutes", max_minutes)
    approval_value = _once("--allow-deletions", allow_deletions)
    if dry_run and break_lock:
        raise click.UsageError("--dry-run takes no lock, so --break-lock does not apply")
    settings = get_settings()
    file = settings.tracking_config()
    config = file.config
    repositories = _configured(repo, config) if repo else None
    try:
        approval = (
            parse_allow_deletions(approval_value, config) if approval_value is not None else None
        )
    except AllowDeletionsError as e:
        raise click.UsageError(str(e)) from None
    token = common.github_token()
    clock = common.clock()
    options = SyncOptions(
        database_url=settings.database_url,
        config=file,
        trigger=runs.trigger(os.environ),
        code_version=runs.code_version(Path(github_activity_db.__file__).parent, os.environ),
        repositories=repositories,
        max_minutes=minutes,
        break_lock=break_lock,
        allow_deletions=approval,
        prune_untracked=prune_untracked,
        dry_run=dry_run,
    )
    started = time.monotonic()
    report = run_async_command(
        engine.sync(
            options,
            open_source=lambda deadline: common.open_source(token, deadline=deadline, clock=clock),
            clock=clock,
        ),
        error_prefix="Sync did not start",
    )
    print_report(console, report, seconds=time.monotonic() - started, break_lock=break_lock)
    raise typer.Exit(report.exit_code)


def _once[T](option: str, values: list[T] | None) -> T | None:
    """A single-valued option's value; click keeps the last of two silently (5.3 step 8)."""
    if not values:
        return None
    if len(values) > 1:
        raise click.UsageError(f"{option} is given more than once")
    return values[0]


def _configured(names: list[str], config: TrackingConfig) -> tuple[TrackedRepository, ...]:
    """The configured repositories ``--repo`` names, ignoring ASCII case, in tracking.toml order.

    GitHub names are ASCII, and ``str.lower()`` folds some other characters into ASCII letters,
    so a name with any other character is refused.
    """
    wanted: set[str] = set()
    for name in names:
        if not name.isascii():
            raise click.UsageError(f"--repo takes an ASCII owner/name, not {name!r}")
        lowered = name.lower()
        if not any(entry.full_name.lower() == lowered for entry in config.repositories):
            raise click.UsageError(f"--repo names {name!r}, which tracking.toml does not list")
        wanted.add(lowered)
    return tuple(entry for entry in config.repositories if entry.full_name.lower() in wanted)


# --------------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------------
def print_report(
    console: Console, report: SyncReport, *, seconds: float, break_lock: bool = False
) -> None:
    """Print the report as plain text: no markup, emoji, highlighting, or wrapping, so GitHub's
    text (labels, names, error messages) is printed as written, on one line each."""
    for line in report_lines(report, seconds=seconds, break_lock=break_lock):
        style = "red" if line.startswith("Error: ") else None
        console.print(line, style=style, markup=False, emoji=False, highlight=False, soft_wrap=True)


def report_lines(report: SyncReport, *, seconds: float, break_lock: bool = False) -> Iterator[str]:
    """The report's lines. Each repository's line names step 1's updates to it (new, renamed,
    tracked again, group, archived, default branch, labels); a dry run also lists each planned
    hydration with its reasons, each in-place update, and each delete candidate (5.3 step 8).
    Other repository fields the resolve reads are written without being listed."""
    if report.dry_run:
        yield f"Dry run: the run would exit {report.exit_code}"
    else:
        yield f"Run {report.run_id}: {report.status}, exit {report.exit_code}"
    for run_id in report.reclaimed:
        yield f"Reclaimed stale run {run_id} (marked aborted)"
    for run_id in report.broken:
        yield f"--break-lock marked run {run_id} aborted"
    if break_lock and report.run_id is not None and not report.broken:
        # 5.3: --break-lock with no blocking row proceeds and says so.
        yield "--break-lock found no blocking run"
    deletions = {d.repository_github_id: d for d in report.deletions}
    for repository in report.repositories:
        yield _repository_line(repository)
        if repository.record is None:
            continue
        github_id = repository.record.github_id
        planned = [item for item in report.planned if item.repository_github_id == github_id]
        updates = [u for u in report.in_place_updates if u.repository_github_id == github_id]
        decision = deletions.get(github_id)
        candidates = len(decision.candidates) if decision is not None else 0
        yield (
            f"  planned {len(planned)}, in-place updates {len(updates)}, "
            f"delete candidates {candidates}"
        )
        if decision is not None and decision.decision.halt_reason is not None:
            yield f"  deletions halted: {decision.decision.halt_reason}"
        if not report.dry_run:
            continue
        for item in planned:
            reasons = ", ".join(sorted(reason.value for reason in item.reasons))
            pending = " (pending)" if item.pending else ""
            yield f"  hydrate #{item.number}{pending}: {reasons}"
        for update in updates:
            yield (
                f"  update #{update.number}: is_draft {update.is_draft}, "
                f"author_association {update.author_association}"
            )
        if decision is not None:
            for candidate in decision.candidates:
                yield f"  delete #{candidate.number} (github_id {candidate.github_id})"
    for full_name in report.untracked:
        yield f"Untracked: {full_name}"
    for pruned in report.pruned:
        verb = "Would prune" if report.dry_run else "Pruned"
        yield f"{verb}: {pruned.full_name} ({pruned.items_deleted} items)"
    if report.prune_refused is not None:
        yield f"Prune refused: {report.prune_refused}"
    for failure in report.failures:
        accepted = f" (accepted: {failure.accepted.reason})" if failure.accepted is not None else ""
        yield f"Failed: {failure.repository}#{failure.number}: {failure.error}{accepted}"
    for entry in report.accepted_not_failed:
        yield (f"Accepted failure did not fail: {entry.repository}#{entry.number} ({entry.reason})")
    for message in report.refresh_failures:
        yield f"Refresh failed: {message}"
    for second in report.second_node_ids:
        yield (
            f"Account {second.github_id} reported as node {second.reported_node_id}, "
            f"stored as {second.stored_node_id}"
        )
    yield (
        f"Hydrated {report.items_hydrated} items; GraphQL points {report.graphql_points}, "
        f"REST calls {report.rest_calls}, {seconds:.0f} s"
    )
    if report.error is not None:
        yield f"Error: {report.error}"


def _repository_line(repository: RepositoryReport) -> str:
    name = repository.configured.full_name
    record, stored = repository.record, repository.stored
    if record is None:
        outcome = repository.outcome or "no outcome"
        return f"{name}: {outcome}: {repository.reason or 'not resolved'}"
    notes: list[str] = []
    if stored is None:
        notes.append("new")
    else:
        if stored.full_name != record.full_name:
            notes.append(f"renamed from {stored.full_name}")
        if not stored.is_tracked:
            notes.append("tracked again")
        if stored.repo_group != repository.configured.group:
            notes.append(f"group {stored.repo_group} -> {repository.configured.group}")
        if stored.is_archived != record.is_archived:
            notes.append("archived" if record.is_archived else "no longer archived")
        if stored.default_branch != record.default_branch:
            notes.append(f"default branch {stored.default_branch} -> {record.default_branch}")
        notes.extend(_label_changes(dict(stored.labels), record))
    if record.full_name.lower() != repository.configured.full_name.lower():
        notes.append(f"GitHub name {record.full_name}")
    shown = f" ({', '.join(notes)})" if notes else ""
    reason = f": {repository.reason}" if repository.reason else ""
    return f"{name}: {repository.outcome or 'no outcome'}{shown}{reason}"


def _label_changes(stored: dict[str, str], record: RepositoryRecord) -> Iterator[str]:
    """The labels step 1 adds, removes, and renames, by node id (D8)."""
    resolved = {label.node_id: label.name for label in record.labels}
    for name in sorted(resolved[node] for node in resolved.keys() - stored.keys()):
        yield f"label added {name}"
    for name in sorted(stored[node] for node in stored.keys() - resolved.keys()):
        yield f"label removed {name}"
    for node in sorted(stored.keys() & resolved.keys(), key=lambda node: stored[node]):
        if stored[node] != resolved[node]:
            yield f"label renamed {stored[node]} -> {resolved[node]}"
