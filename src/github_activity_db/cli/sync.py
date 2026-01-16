"""Sync commands for GitHub Activity DB."""

import asyncio
import json
from typing import Any

import typer
from rich.console import Console

from github_activity_db.db import PullRequestRepository, RepositoryRepository, get_session
from github_activity_db.github import GitHubClient, OutputFormat, PRIngestionService

app = typer.Typer(help="Sync PR data from GitHub")
console = Console()


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
