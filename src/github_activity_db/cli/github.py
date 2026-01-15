"""GitHub API verification commands."""

import asyncio
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from github_activity_db.config import get_settings
from github_activity_db.github import (
    GitHubAuthenticationError,
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitError,
)

app = typer.Typer(help="GitHub API commands")
console = Console()


def _run_async[T](coro: T) -> T:
    """Run an async function in the event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)  # type: ignore[arg-type,return-value]


@app.command("test")
def test_connection(
    repo: str = typer.Option(
        "prebid/prebid-server",
        "--repo",
        "-r",
        help="Repository to test (owner/name format)",
    ),
    pr_number: int | None = typer.Option(
        None,
        "--pr",
        "-p",
        help="Specific PR number to fetch (tests full PR details)",
    ),
) -> None:
    """Test GitHub API connectivity and token validity.

    Examples:
        ghactivity github test
        ghactivity github test --repo prebid/Prebid.js
        ghactivity github test --pr 1234
    """

    async def _test() -> None:
        settings = get_settings()

        # Validate token exists
        if not settings.github_token:
            console.print("[red]Error:[/red] GITHUB_TOKEN not set in environment")
            raise typer.Exit(1)

        try:
            async with GitHubClient() as client:
                # 1. Check rate limit
                console.print("[bold]Checking rate limit...[/bold]")
                rate = await client.get_rate_limit()
                reset_time = rate["reset"]
                if isinstance(reset_time, datetime):
                    reset_str = reset_time.strftime("%H:%M:%S UTC")
                else:
                    reset_str = str(reset_time)
                console.print(
                    f"  Rate limit: {rate['remaining']}/{rate['limit']} "
                    f"(resets at {reset_str})"
                )

                if isinstance(rate["remaining"], int) and rate["remaining"] < 10:
                    console.print("[yellow]Warning:[/yellow] Low rate limit remaining")

                # 2. Parse repo
                if "/" not in repo:
                    console.print(
                        "[red]Error:[/red] Invalid repo format. Use owner/name"
                    )
                    raise typer.Exit(1)
                owner, name = repo.split("/", 1)

                # 3. List open PRs
                console.print(f"\n[bold]Fetching PRs from {repo}...[/bold]")
                prs = await client.list_pull_requests(owner, name, state="open")
                console.print(f"  Found {len(prs)} open PR(s)")

                if prs:
                    # Show table of first 5
                    table = Table(title=f"Open PRs in {repo}")
                    table.add_column("Number", style="cyan")
                    table.add_column("Title", max_width=50)
                    table.add_column("Author")
                    table.add_column("Updated")

                    for pr in prs[:5]:
                        title = (
                            pr.title[:47] + "..." if len(pr.title) > 50 else pr.title
                        )
                        table.add_row(
                            str(pr.number),
                            title,
                            pr.user.login,
                            pr.updated_at.strftime("%Y-%m-%d"),
                        )

                    console.print(table)

                    if len(prs) > 5:
                        console.print(f"  ... and {len(prs) - 5} more")

                # 4. Optionally test full PR fetch
                if pr_number:
                    console.print(f"\n[bold]Fetching PR #{pr_number} details...[/bold]")
                    pr, files, commits, reviews = await client.get_full_pull_request(
                        owner, name, pr_number
                    )

                    console.print(f"  Title: {pr.title}")
                    console.print(f"  State: {pr.state} (merged: {pr.merged})")
                    console.print(
                        f"  Stats: +{pr.additions}/-{pr.deletions} "
                        f"in {pr.changed_files} files"
                    )
                    console.print(f"  Commits: {len(commits)}")
                    console.print(f"  Reviews: {len(reviews)}")
                    if files:
                        console.print("  Files:")
                        for f in files[:5]:
                            console.print(f"    - {f.filename} ({f.status})")
                        if len(files) > 5:
                            console.print(f"    ... and {len(files) - 5} more")

                console.print("\n[green]GitHub API connection verified![/green]")

        except GitHubAuthenticationError:
            console.print("[red]Error:[/red] Invalid GitHub token")
            raise typer.Exit(1) from None
        except GitHubRateLimitError as e:
            console.print("[red]Error:[/red] Rate limit exceeded")
            if e.reset_at:
                console.print(f"  Resets at: {e.reset_at.strftime('%H:%M:%S UTC')}")
            raise typer.Exit(1) from None
        except GitHubNotFoundError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1) from None
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1) from None

    _run_async(_test())


@app.command("rate-limit")
def show_rate_limit() -> None:
    """Show current GitHub API rate limit status."""

    async def _check() -> None:
        settings = get_settings()

        if not settings.github_token:
            console.print("[red]Error:[/red] GITHUB_TOKEN not set in environment")
            raise typer.Exit(1)

        try:
            async with GitHubClient() as client:
                rate = await client.get_rate_limit()

                table = Table(title="GitHub API Rate Limit")
                table.add_column("Metric", style="bold")
                table.add_column("Value")

                table.add_row("Limit", str(rate["limit"]))
                table.add_row("Used", str(rate["used"]))
                table.add_row("Remaining", str(rate["remaining"]))

                reset_time = rate["reset"]
                if isinstance(reset_time, datetime):
                    reset_str = reset_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    reset_str = str(reset_time)
                table.add_row("Resets At", reset_str)

                console.print(table)

                # Color-coded status
                limit = rate["limit"]
                remaining = rate["remaining"]
                if isinstance(limit, int) and isinstance(remaining, int) and limit > 0:
                    remaining_pct = remaining / limit * 100
                    if remaining_pct > 50:
                        status = "[green]Healthy[/green]"
                    elif remaining_pct > 10:
                        status = "[yellow]Moderate[/yellow]"
                    else:
                        status = "[red]Low - consider waiting[/red]"
                    console.print(f"\nStatus: {status}")

        except GitHubAuthenticationError:
            console.print("[red]Error:[/red] Invalid GitHub token")
            raise typer.Exit(1) from None

    _run_async(_check())
