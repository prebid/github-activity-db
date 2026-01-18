"""GitHub API verification commands."""

from datetime import datetime

import typer
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

from github_activity_db.cli.common import console, run_async_command
from github_activity_db.config import get_settings
from github_activity_db.github import (
    GitHubAuthenticationError,
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitError,
    RateLimitMonitor,
    RateLimitPool,
    RateLimitStatus,
)
from github_activity_db.schemas import parse_repo_string

app = typer.Typer(help="GitHub API commands")


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
                    f"  Rate limit: {rate['remaining']}/{rate['limit']} " f"(resets at {reset_str})"
                )

                if isinstance(rate["remaining"], int) and rate["remaining"] < 10:
                    console.print("[yellow]Warning:[/yellow] Low rate limit remaining")

                # 2. Parse repo
                try:
                    owner, name = parse_repo_string(repo)
                except ValueError:
                    console.print("[red]Error:[/red] Invalid repo format. Use owner/name")
                    raise typer.Exit(1) from None

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
                        title = pr.title[:47] + "..." if len(pr.title) > 50 else pr.title
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
                        f"  Stats: +{pr.additions}/-{pr.deletions} " f"in {pr.changed_files} files"
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

    run_async_command(_test())


def _get_status_style(status: RateLimitStatus) -> str:
    """Get rich style for status."""
    match status:
        case RateLimitStatus.HEALTHY:
            return "[green]HEALTHY[/green]"
        case RateLimitStatus.WARNING:
            return "[yellow]WARNING[/yellow]"
        case RateLimitStatus.CRITICAL:
            return "[red]CRITICAL[/red]"
        case RateLimitStatus.EXHAUSTED:
            return "[bold red]EXHAUSTED[/bold red]"
        case _:
            return str(status)


def _format_time_remaining(seconds: int) -> str:
    """Format seconds as human-readable time."""
    if seconds <= 0:
        return "Now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"


@app.command("rate-limit")
def show_rate_limit(
    all_pools: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Show all rate limit pools (not just core)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show detailed information",
    ),
) -> None:
    """Show current GitHub API rate limit status.

    Examples:
        ghactivity github rate-limit
        ghactivity github rate-limit --all
        ghactivity github rate-limit -v
    """

    async def _check() -> None:
        settings = get_settings()

        if not settings.github_token:
            console.print("[red]Error:[/red] GITHUB_TOKEN not set in environment")
            raise typer.Exit(1)

        try:
            async with GitHubClient() as client:
                monitor = RateLimitMonitor(client._github)
                await monitor.initialize()

                # Token verification
                is_pat = monitor.verify_pat()
                if is_pat:
                    console.print("[green]✓[/green] Authenticated with PAT (5,000 requests/hour)")
                else:
                    console.print(
                        "[yellow]⚠[/yellow] Unauthenticated or limited token " "(60 requests/hour)"
                    )

                # Determine which pools to show
                pools_to_show: list[RateLimitPool] = (
                    list(RateLimitPool) if all_pools else [RateLimitPool.CORE]
                )

                # Build table
                table = Table(title="GitHub API Rate Limits")
                table.add_column("Pool", style="bold")
                table.add_column("Status")
                table.add_column("Remaining", justify="right")
                table.add_column("Limit", justify="right")
                table.add_column("Used %", justify="right")
                table.add_column("Resets In", justify="right")

                for pool in pools_to_show:
                    pool_limit = monitor.get_pool_limit(pool)
                    if pool_limit is None:
                        continue

                    status = monitor.get_status(pool)
                    time_remaining = monitor.time_until_reset(pool)

                    # Format usage percentage with color
                    usage_pct = pool_limit.usage_percent
                    if usage_pct < 50:
                        usage_str = f"[green]{usage_pct:.1f}%[/green]"
                    elif usage_pct < 80:
                        usage_str = f"[yellow]{usage_pct:.1f}%[/yellow]"
                    else:
                        usage_str = f"[red]{usage_pct:.1f}%[/red]"

                    table.add_row(
                        pool.value,
                        _get_status_style(status),
                        str(pool_limit.remaining),
                        str(pool_limit.limit),
                        usage_str,
                        _format_time_remaining(time_remaining),
                    )

                console.print()
                console.print(table)

                # Visual progress bar for core pool
                core_limit = monitor.get_pool_limit(RateLimitPool.CORE)
                if core_limit:
                    console.print()
                    remaining_pct = core_limit.remaining_percent
                    with Progress(
                        TextColumn("[bold]Core quota:[/bold]"),
                        BarColumn(bar_width=40, complete_style="green", finished_style="green"),
                        TextColumn(f"{remaining_pct:.1f}% remaining"),
                        console=console,
                        transient=True,
                    ) as progress:
                        task = progress.add_task("", total=100)
                        progress.update(task, completed=remaining_pct)
                        # Force display since transient
                        progress.refresh()

                # Verbose mode: additional details
                if verbose:
                    console.print("\n[bold]Detailed Information[/bold]")
                    snapshot = monitor._snapshot
                    if snapshot:
                        console.print(f"  Last updated: {snapshot.timestamp:%Y-%m-%d %H:%M:%S UTC}")
                        for pool in pools_to_show:
                            pool_limit = monitor.get_pool_limit(pool)
                            if pool_limit and pool_limit.reset_at:
                                console.print(
                                    f"  {pool.value} resets at: "
                                    f"{pool_limit.reset_at:%Y-%m-%d %H:%M:%S UTC}"
                                )

                # Recommendations
                core_status = monitor.get_status(RateLimitPool.CORE)
                if core_status == RateLimitStatus.CRITICAL:
                    console.print(
                        "\n[yellow]Recommendation:[/yellow] Rate limit is low. "
                        "Consider waiting before making more API calls."
                    )
                elif core_status == RateLimitStatus.EXHAUSTED:
                    time_left = monitor.time_until_reset(RateLimitPool.CORE)
                    console.print(
                        f"\n[red]Rate limit exhausted![/red] "
                        f"Wait {_format_time_remaining(time_left)} before making API calls."
                    )

        except GitHubAuthenticationError:
            console.print("[red]Error:[/red] Invalid GitHub token")
            raise typer.Exit(1) from None
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1) from None

    run_async_command(_check())
