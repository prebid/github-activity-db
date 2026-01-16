"""Sync commands for GitHub Activity DB."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import typer
from rich.console import Console

from github_activity_db.db import PullRequestRepository, RepositoryRepository, get_session
from github_activity_db.github import (
    BulkIngestionConfig,
    BulkPRIngestionService,
    GitHubClient,
    OutputFormat,
    PRIngestionService,
    RequestPacer,
    RequestScheduler,
)
from github_activity_db.github.pacing import ProgressTracker
from github_activity_db.github.rate_limit import RateLimitMonitor

app = typer.Typer(help="Sync PR data from GitHub")
console = Console()


def _parse_date(date_str: str | None) -> datetime | None:
    """Parse a date string into a datetime object.

    Supports formats:
    - YYYY-MM-DD
    - YYYY-MM-DDTHH:MM:SS
    - ISO format with timezone

    Args:
        date_str: Date string to parse, or None

    Returns:
        datetime object with UTC timezone, or None if input was None

    Raises:
        typer.BadParameter: If the date string is invalid
    """
    if date_str is None:
        return None

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            # Ensure UTC timezone
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue

    raise typer.BadParameter(
        f"Invalid date format: {date_str}. "
        "Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS format."
    )


@app.command("pr")
def sync_single_pr(
    repo: str = typer.Argument(
        ...,
        help="Repository in owner/name format (e.g., prebid/prebid-server)",
    ),
    pr_number: int = typer.Argument(
        ...,
        help="PR number to sync",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show detailed output",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Only output on error",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Don't write to database",
    ),
    output_format: OutputFormat = typer.Option(  # noqa: B008
        OutputFormat.TEXT,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    """Sync a single PR from GitHub to the database.

    Examples:
        ghactivity sync pr prebid/prebid-server 4663
        ghactivity sync pr prebid/prebid-server 4663 --dry-run
        ghactivity sync pr prebid/prebid-server 4663 --format json
        ghactivity sync pr prebid/prebid-server 4663 -v
    """
    # Validate repo format
    if "/" not in repo:
        console.print("[red]Error:[/red] Repository must be in owner/name format")
        raise typer.Exit(1)

    owner, name = repo.split("/", 1)

    async def _sync() -> dict[str, Any]:
        async with GitHubClient() as client:
            async with get_session() as session:
                service = PRIngestionService(
                    client=client,
                    repo_repository=RepositoryRepository(session),
                    pr_repository=PullRequestRepository(session),
                )

                result = await service.ingest_pr(
                    owner, name, pr_number, dry_run=dry_run
                )

                return result.to_dict()

    try:
        result: dict[str, Any] = asyncio.get_event_loop().run_until_complete(_sync())
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    # JSON output
    if output_format == OutputFormat.JSON:
        console.print_json(json.dumps(result))
        return

    # Handle errors
    if not result.get("success"):
        console.print(f"[red]Error:[/red] {result.get('error', 'Unknown error')}")
        raise typer.Exit(1)

    # Quiet mode - silent on success
    if quiet:
        return

    # Text output
    prefix = "[dim](dry-run)[/dim] " if dry_run else ""
    action = result.get("action", "unknown").title()
    title = result.get("title", "")

    # Truncate title if too long
    if len(title) > 60:
        title = title[:57] + "..."

    console.print(f"{prefix}[bold]{action}[/bold] PR #{pr_number}: {title}")

    # Verbose output
    if verbose:
        if result.get("pr_id"):
            console.print(f"  ID: {result['pr_id']}")
        if result.get("state"):
            console.print(f"  State: {result['state']}")
        console.print(f"  Repository: {repo}")


@app.command("repo")
def sync_repository(
    repo: str = typer.Argument(
        ...,
        help="Repository in owner/name format (e.g., prebid/prebid-server)",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only sync PRs created after this date (YYYY-MM-DD or ISO format)",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Only sync PRs created before this date (YYYY-MM-DD or ISO format)",
    ),
    state: str = typer.Option(
        "all",
        "--state",
        "-s",
        help="PR state filter: open, merged, all (excludes abandoned PRs)",
    ),
    max_prs: int | None = typer.Option(
        None,
        "--max",
        "-m",
        help="Maximum number of PRs to sync (useful for testing)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show detailed output including per-PR progress",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Only output final summary on success",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Don't write to database, just show what would happen",
    ),
    output_format: OutputFormat = typer.Option(  # noqa: B008
        OutputFormat.TEXT,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    """Sync all PRs from a repository to the database.

    Imports PRs matching the specified filters (date range, state) using
    intelligent rate limiting and parallel execution.

    By default, syncs both open and merged PRs, excluding abandoned PRs
    (closed but never merged).

    Examples:
        ghactivity sync repo prebid/prebid-server --since 2024-10-01
        ghactivity sync repo prebid/prebid-server --state open
        ghactivity sync repo prebid/prebid-server --max 10 --dry-run
        ghactivity sync repo prebid/prebid-server --since 2024-10-01 --format json
    """
    # Validate repo format
    if "/" not in repo:
        console.print("[red]Error:[/red] Repository must be in owner/name format")
        raise typer.Exit(1)

    owner, name = repo.split("/", 1)

    # Parse dates
    try:
        since_dt = _parse_date(since)
        until_dt = _parse_date(until)
    except typer.BadParameter as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    # Validate state
    valid_states = ("open", "merged", "all")
    if state not in valid_states:
        console.print(
            f"[red]Error:[/red] Invalid state '{state}'. "
            f"Must be one of: {', '.join(valid_states)}"
        )
        raise typer.Exit(1)

    # Create config
    config = BulkIngestionConfig(
        since=since_dt,
        until=until_dt,
        state=state,  # type: ignore[arg-type]
        max_prs=max_prs,
        dry_run=dry_run,
    )

    async def _sync() -> dict[str, Any]:
        async with GitHubClient() as client:
            async with get_session() as session:
                # Set up rate limiting infrastructure
                monitor = RateLimitMonitor(client._github)
                pacer = RequestPacer(monitor)
                scheduler = RequestScheduler(pacer, max_concurrent=config.concurrency)

                # Create progress tracker
                progress_tracker = ProgressTracker(name="PR Import")

                service = BulkPRIngestionService(
                    client=client,
                    repo_repository=RepositoryRepository(session),
                    pr_repository=PullRequestRepository(session),
                    scheduler=scheduler,
                    progress=progress_tracker,
                )

                # Start scheduler
                await scheduler.start()

                try:
                    result = await service.ingest_repository(owner, name, config)
                    return result.to_dict()
                finally:
                    await scheduler.shutdown(wait=True)

    # Show progress spinner for non-quiet mode
    if not quiet and output_format == OutputFormat.TEXT:
        console.print(f"[dim]Syncing PRs from {repo}...[/dim]")
        if since_dt:
            console.print(f"[dim]  Since: {since_dt.date()}[/dim]")
        if until_dt:
            console.print(f"[dim]  Until: {until_dt.date()}[/dim]")
        if max_prs:
            console.print(f"[dim]  Max PRs: {max_prs}[/dim]")
        if dry_run:
            console.print("[dim]  Mode: dry-run (no database writes)[/dim]")
        console.print()

    try:
        result: dict[str, Any] = asyncio.get_event_loop().run_until_complete(_sync())
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    # JSON output
    if output_format == OutputFormat.JSON:
        console.print_json(json.dumps(result))
        return

    # Quiet mode - just show summary
    if quiet:
        return

    # Text output
    prefix = "[dim](dry-run)[/dim] " if dry_run else ""

    console.print(f"{prefix}[bold]Sync Complete[/bold]")
    console.print()

    # Summary table
    console.print(f"  [green]Created:[/green]           {result.get('created', 0)}")
    console.print(f"  [blue]Updated:[/blue]            {result.get('updated', 0)}")
    console.print(
        f"  [dim]Skipped (frozen):[/dim]   {result.get('skipped_frozen', 0)}"
    )
    console.print(
        f"  [dim]Skipped (unchanged):[/dim] {result.get('skipped_unchanged', 0)}"
    )

    failed = result.get("failed", 0)
    if failed > 0:
        console.print(f"  [red]Failed:[/red]             {failed}")

    console.print()
    console.print(f"  Total discovered: {result.get('total_discovered', 0)}")
    console.print(f"  Duration: {result.get('duration_seconds', 0):.1f}s")
    console.print(f"  Success rate: {result.get('success_rate', 100):.1f}%")

    # Verbose output - show failed PRs
    if verbose and failed > 0:
        console.print()
        console.print("[bold]Failed PRs:[/bold]")
        for failed_pr in result.get("failed_prs", []):
            pr_num = failed_pr.get("pr_number", "?")
            error = failed_pr.get("error", "Unknown error")
            console.print(f"  PR #{pr_num}: {error}")
