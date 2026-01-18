"""Sync commands for GitHub Activity DB."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import typer
from rich.console import Console

from github_activity_db.config import get_settings
from github_activity_db.db import (
    PullRequestRepository,
    RepositoryRepository,
    SyncFailureRepository,
    get_session,
)
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
from github_activity_db.github.sync import FailureRetryService, MultiRepoOrchestrator

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
        ghactivity -v sync pr prebid/prebid-server 4663  # Debug logging
    """
    # Validate repo format
    if "/" not in repo:
        console.print("[red]Error:[/red] Repository must be in owner/name format")
        raise typer.Exit(1)

    owner, name = repo.split("/", 1)

    async def _sync() -> dict[str, Any]:
        async with GitHubClient() as base_client:
            # Initialize pacing infrastructure for rate limit protection
            monitor = RateLimitMonitor(base_client._github)
            await monitor.initialize()
            pacer = RequestPacer(monitor)

            # Create paced client
            async with GitHubClient(
                rate_monitor=monitor, pacer=pacer
            ) as client:
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

    # Text output
    prefix = "[dim](dry-run)[/dim] " if dry_run else ""
    action = result.get("action", "unknown").title()
    title = result.get("title", "")

    # Truncate title if too long
    if len(title) > 60:
        title = title[:57] + "..."

    console.print(f"{prefix}[bold]{action}[/bold] PR #{pr_number}: {title}")


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
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Don't write to database, just show what would happen",
    ),
    auto_retry: bool = typer.Option(
        False,
        "--auto-retry",
        help="Retry any pending failures for this repo before syncing new PRs",
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
        ghactivity sync repo prebid/prebid-server --auto-retry --since 2024-10-01
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
        async with GitHubClient() as base_client:
            # Set up rate limiting infrastructure
            monitor = RateLimitMonitor(base_client._github)
            await monitor.initialize()  # Fetch initial rate limit state
            pacer = RequestPacer(monitor)
            scheduler = RequestScheduler(pacer, max_concurrent=config.concurrency)

            # Create paced client with integrated rate limiting
            async with GitHubClient(
                rate_monitor=monitor, pacer=pacer
            ) as client:
                async with get_session() as session:
                    # Create progress tracker
                    progress_tracker = ProgressTracker(name="PR Import")

                    # Shared lock to serialize database writes across concurrent operations
                    write_lock = asyncio.Lock()

                    repo_repository = RepositoryRepository(session, write_lock=write_lock)
                    pr_repository = PullRequestRepository(session, write_lock=write_lock)
                    failure_repository = SyncFailureRepository(
                        session, write_lock=write_lock
                    )

                    # Auto-retry pending failures before main sync
                    retry_dict: dict[str, Any] | None = None
                    if auto_retry:
                        repository = await repo_repository.get_by_owner_and_name(
                            owner, name
                        )
                        if repository:
                            retry_service = FailureRetryService(
                                ingestion_service=PRIngestionService(
                                    client=client,
                                    repo_repository=repo_repository,
                                    pr_repository=pr_repository,
                                ),
                                failure_repository=failure_repository,
                                repo_repository=repo_repository,
                            )
                            retry_svc_result = await retry_service.retry_failures(
                                repository_id=repository.id,
                                dry_run=dry_run,
                            )
                            retry_dict = retry_svc_result.to_dict()

                    service = BulkPRIngestionService(
                        client=client,
                        repo_repository=repo_repository,
                        pr_repository=pr_repository,
                        scheduler=scheduler,
                        progress=progress_tracker,
                        failure_repository=failure_repository,
                    )

                    # Start scheduler
                    await scheduler.start()

                    try:
                        result = await service.ingest_repository(owner, name, config)
                        sync_result = result.to_dict()
                        if retry_dict:
                            sync_result["retry_result"] = retry_dict
                        return sync_result
                    finally:
                        await scheduler.shutdown(wait=True)

    # Show progress info
    if output_format == OutputFormat.TEXT:
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

    # Text output
    prefix = "[dim](dry-run)[/dim] " if dry_run else ""

    # Show retry results if auto-retry was enabled
    retry_result = result.get("retry_result")
    if retry_result:
        console.print(f"{prefix}[bold]Auto-Retry Results[/bold]")
        console.print()
        console.print(f"  [green]Resolved:[/green]       {retry_result.get('succeeded', 0)}")
        console.print(f"  [yellow]Failed again:[/yellow]  {retry_result.get('failed_again', 0)}")
        console.print(f"  [red]Permanent:[/red]      {retry_result.get('marked_permanent', 0)}")
        console.print()

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

    # Show failed PRs if any
    if failed > 0:
        console.print()
        console.print("[bold]Failed PRs:[/bold]")
        for failed_pr in result.get("failed_prs", []):
            pr_num = failed_pr.get("pr_number", "?")
            error = failed_pr.get("error", "Unknown error")
            console.print(f"  PR #{pr_num}: {error}")


@app.command("all")
def sync_all_repositories(
    repos: str | None = typer.Option(
        None,
        "--repos",
        "-r",
        help="Comma-separated list of repos to sync (owner/repo). "
        "If not specified, syncs all tracked Prebid repositories.",
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
    max_per_repo: int | None = typer.Option(
        None,
        "--max-per-repo",
        "-m",
        help="Maximum number of PRs to sync per repository (useful for testing)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Don't write to database, just show what would happen",
    ),
    auto_retry: bool = typer.Option(
        False,
        "--auto-retry",
        help="Retry any pending failures across all repos before syncing new PRs",
    ),
    output_format: OutputFormat = typer.Option(  # noqa: B008
        OutputFormat.TEXT,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    """Sync all tracked repositories to the database.

    By default, syncs all 8 Prebid repositories. Use --repos to override.

    Examples:
        ghactivity sync all --since 2024-10-01
        ghactivity sync all --repos prebid/prebid-server,prebid/Prebid.js
        ghactivity sync all --max-per-repo 10 --dry-run
        ghactivity sync all --auto-retry --since 2024-10-01
    """
    # Parse repos list
    repo_list: list[str] | None = None
    if repos:
        repo_list = [r.strip() for r in repos.split(",") if r.strip()]
        # Validate repo format
        for r in repo_list:
            if "/" not in r:
                console.print(
                    f"[red]Error:[/red] Repository '{r}' must be in owner/name format"
                )
                raise typer.Exit(1)

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
        max_prs=max_per_repo,
        dry_run=dry_run,
    )

    # Get repo list for display
    display_repos = repo_list if repo_list else get_settings().tracked_repos

    async def _sync() -> dict[str, Any]:
        async with GitHubClient() as base_client:
            # Set up rate limiting infrastructure
            monitor = RateLimitMonitor(base_client._github)
            await monitor.initialize()  # Fetch initial rate limit state
            pacer = RequestPacer(monitor)
            scheduler = RequestScheduler(pacer, max_concurrent=config.concurrency)

            # Create paced client with integrated rate limiting
            async with GitHubClient(
                rate_monitor=monitor, pacer=pacer
            ) as client:
                async with get_session() as session:
                    # Shared lock to serialize database writes across concurrent operations
                    write_lock = asyncio.Lock()

                    repo_repository = RepositoryRepository(session, write_lock=write_lock)
                    pr_repository = PullRequestRepository(session, write_lock=write_lock)
                    failure_repository = SyncFailureRepository(
                        session, write_lock=write_lock
                    )

                    # Auto-retry pending failures before main sync
                    retry_dict: dict[str, Any] | None = None
                    if auto_retry:
                        retry_service = FailureRetryService(
                            ingestion_service=PRIngestionService(
                                client=client,
                                repo_repository=repo_repository,
                                pr_repository=pr_repository,
                            ),
                            failure_repository=failure_repository,
                            repo_repository=repo_repository,
                        )
                        # Retry ALL pending failures across all repos
                        retry_svc_result = await retry_service.retry_failures(
                            dry_run=dry_run,
                        )
                        retry_dict = retry_svc_result.to_dict()

                    orchestrator = MultiRepoOrchestrator(
                        client=client,
                        repo_repository=repo_repository,
                        pr_repository=pr_repository,
                        scheduler=scheduler,
                        failure_repository=failure_repository,
                    )

                    # Start scheduler
                    await scheduler.start()

                    try:
                        result = await orchestrator.sync_all(config, repo_list)
                        sync_result = result.to_dict()
                        if retry_dict:
                            sync_result["retry_result"] = retry_dict
                        return sync_result
                    finally:
                        await scheduler.shutdown(wait=True)

    # Show progress info
    if output_format == OutputFormat.TEXT:
        console.print(f"[dim]Syncing {len(display_repos)} repositories...[/dim]")
        if since_dt:
            console.print(f"[dim]  Since: {since_dt.date()}[/dim]")
        if until_dt:
            console.print(f"[dim]  Until: {until_dt.date()}[/dim]")
        if max_per_repo:
            console.print(f"[dim]  Max PRs per repo: {max_per_repo}[/dim]")
        if dry_run:
            console.print("[dim]  Mode: dry-run (no database writes)[/dim]")
        console.print()
        console.print("[dim]Repositories:[/dim]")
        for r in display_repos:
            console.print(f"[dim]  - {r}[/dim]")
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

    # Text output
    summary = result.get("summary", {})
    prefix = "[dim](dry-run)[/dim] " if dry_run else ""

    console.print(f"{prefix}[bold]Multi-Repository Sync Complete[/bold]")
    console.print()

    # Repository summary
    total_repos = summary.get("total_repos", 0)
    repos_succeeded = summary.get("repos_succeeded", 0)
    repos_failed = summary.get("repos_with_failures", 0)

    console.print(f"  [bold]Repositories:[/bold]      {total_repos}")
    console.print(f"    [green]Succeeded:[/green]       {repos_succeeded}")
    if repos_failed > 0:
        console.print(f"    [red]With failures:[/red]   {repos_failed}")

    console.print()

    # PR summary
    console.print(f"  [green]PRs Created:[/green]       {summary.get('total_created', 0)}")
    console.print(f"  [blue]PRs Updated:[/blue]        {summary.get('total_updated', 0)}")
    console.print(f"  [dim]PRs Skipped:[/dim]        {summary.get('total_skipped', 0)}")

    total_failed = summary.get("total_failed", 0)
    if total_failed > 0:
        console.print(f"  [red]PRs Failed:[/red]         {total_failed}")

    console.print()
    console.print(f"  Total discovered: {summary.get('total_discovered', 0)}")
    console.print(f"  Duration: {summary.get('duration_seconds', 0):.1f}s")

    # Show per-repository details
    console.print()
    console.print("[bold]Per-Repository Details:[/bold]")
    for repo_data in result.get("repositories", []):
        repo_name = repo_data.get("repository", "?")
        created = repo_data.get("created", 0)
        updated = repo_data.get("updated", 0)
        failed = repo_data.get("failed", 0)
        duration = repo_data.get("duration_seconds", 0)

        status = "[green]OK[/green]" if failed == 0 else f"[red]{failed} failed[/red]"
        console.print(
            f"  {repo_name}: +{created} ~{updated} ({status}) [{duration:.1f}s]"
        )

    # Show failed PRs if any (for retry guidance)
    total_failed = summary.get("total_failed", 0)
    if total_failed > 0:
        console.print()
        console.print("[bold red]Failed PRs (retry with: ghactivity sync retry):[/bold red]")
        for repo_data in result.get("repositories", []):
            repo_name = repo_data.get("repository", "?")
            failed_prs = repo_data.get("failed_prs", [])
            for failed_pr in failed_prs:
                pr_num = failed_pr.get("pr_number", "?")
                error = failed_pr.get("error", "Unknown error")[:80]
                console.print(f"  {repo_name} #{pr_num}: {error}")


@app.command("retry")
def sync_retry(
    repo: str | None = typer.Option(
        None,
        "--repo",
        "-r",
        help="Filter by repository (owner/name format)",
    ),
    max_items: int | None = typer.Option(
        None,
        "--max",
        "-m",
        help="Maximum number of failures to retry",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview what would be retried without making changes",
    ),
    output_format: OutputFormat = typer.Option(  # noqa: B008
        OutputFormat.TEXT,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    """Retry previously failed PR syncs.

    Retrieves pending failures from the database and attempts to re-sync them.
    Failures are tracked automatically during sync operations.

    Examples:
        ghactivity sync retry                         # Retry all pending failures
        ghactivity sync retry --repo prebid/Prebid.js # Retry failures for specific repo
        ghactivity sync retry --max 10                # Retry up to 10 failures
        ghactivity sync retry --dry-run               # Preview without changes
        ghactivity sync retry --format json           # JSON output
    """
    # Get repository ID if filtering by repo
    repository_id: int | None = None

    async def _get_repo_id() -> int | None:
        if repo is None:
            return None

        if "/" not in repo:
            console.print("[red]Error:[/red] Repository must be in owner/name format")
            raise typer.Exit(1)

        owner, name = repo.split("/", 1)

        async with get_session() as session:
            repo_repository = RepositoryRepository(session)
            repository = await repo_repository.get_by_owner_and_name(owner, name)
            if repository is None:
                console.print(f"[red]Error:[/red] Repository {repo} not found in database")
                raise typer.Exit(1)
            return repository.id

    async def _retry() -> dict[str, Any]:
        async with GitHubClient() as client:
            async with get_session() as session:
                service = FailureRetryService(
                    ingestion_service=PRIngestionService(
                        client=client,
                        repo_repository=RepositoryRepository(session),
                        pr_repository=PullRequestRepository(session),
                    ),
                    failure_repository=SyncFailureRepository(session),
                    repo_repository=RepositoryRepository(session),
                )

                result = await service.retry_failures(
                    repository_id=repository_id,
                    max_items=max_items,
                    dry_run=dry_run,
                )

                return result.to_dict()

    try:
        if repo is not None:
            repository_id = asyncio.get_event_loop().run_until_complete(_get_repo_id())

        result: dict[str, Any] = asyncio.get_event_loop().run_until_complete(_retry())
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    # JSON output
    if output_format == OutputFormat.JSON:
        console.print_json(json.dumps(result))
        return

    # Text output
    total_pending = result.get("total_pending", 0)

    if total_pending == 0:
        console.print("[dim]No pending failures to retry[/dim]")
        return

    prefix = "[dim](dry-run)[/dim] " if dry_run else ""
    console.print(f"{prefix}[bold]Retry Results[/bold]")
    console.print()
    console.print(f"  Pending failures:   {total_pending}")
    console.print(f"  [green]Succeeded:[/green]          {result.get('succeeded', 0)}")
    console.print(f"  [yellow]Failed again:[/yellow]       {result.get('failed_again', 0)}")
    console.print(f"  [red]Marked permanent:[/red]   {result.get('marked_permanent', 0)}")
    console.print()
    console.print(f"  Duration: {result.get('duration_seconds', 0):.1f}s")

    # Show individual results
    results = result.get("results", [])
    if results:
        console.print()
        console.print("[bold]Individual Results:[/bold]")
        for item in results[:20]:  # Limit to first 20
            pr_num = item.get("pr_number", "?")
            success = item.get("success", False)
            action = item.get("action", "unknown")
            error = item.get("error")

            if success:
                console.print(f"  [green]✓[/green] PR #{pr_num}: {action}")
            else:
                error_msg = error[:60] + "..." if error and len(error) > 60 else error
                console.print(f"  [red]✗[/red] PR #{pr_num}: {error_msg}")

        if len(results) > 20:
            console.print(f"  ... and {len(results) - 20} more")
