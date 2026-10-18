"""GitHub API diagnostics over the source layer (redesign plan 5.7, 5.8)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from github_activity_db.cli import common
from github_activity_db.cli.common import console, error_console, run_async_command
from github_activity_db.config import get_settings
from github_activity_db.source import (
    Failed,
    Gone,
    Hydrated,
    PullRequestOutcome,
    RateLimits,
    RepositoryRecord,
    Usage,
)

app = typer.Typer(help="GitHub API diagnostics.")

# The pools the sync reads: GraphQL for resolve, index, and hydration, core for compare.
DEFAULT_POOLS = frozenset({"core", "graphql"})


@app.command("rate-limit")
def show_rate_limit(
    all_pools: Annotated[
        bool,
        typer.Option("--all", "-a", help="Show every REST pool, not only core and graphql."),
    ] = False,
) -> None:
    """Show the GraphQL rateLimit reading and the REST /rate_limit pools."""
    token = common.github_token()

    async def read() -> RateLimits:
        async with common.open_source(token) as source:
            return await source.rate_limits()

    limits = run_async_command(read(), error_prefix="Rate limit check failed")
    console.print(
        f"GraphQL rateLimit: {limits.graphql_remaining} remaining, "
        f"resets {_time(limits.graphql_reset_at)}"
    )
    table = Table(title="REST /rate_limit")
    # Kept whole at 80 columns, where Rich would otherwise shorten the longest pool names.
    table.add_column("Pool", no_wrap=True)
    for column in ("Remaining", "Limit", "Used", "Resets"):
        table.add_column(column, justify="right")
    for pool in limits.rest:
        if all_pools or pool.resource in DEFAULT_POOLS:
            table.add_row(
                pool.resource,
                str(pool.remaining),
                str(pool.limit),
                str(pool.used),
                _time(pool.reset_at),
            )
    console.print(table)


@app.command("test")
def test_connection(
    pr: Annotated[int, typer.Option("--pr", "-p", min=1, help="Pull request number to hydrate.")],
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-r",
            help="Repository as owner/name (default: the first one in tracking.toml).",
        ),
    ] = None,
) -> None:
    """Resolve a repository and hydrate one pull request through the source layer."""
    token = common.github_token()
    full_name = repo if repo is not None else get_settings().tracked_repos[0]
    try:
        owner, name = common.parse_repo_string(full_name)
    except ValueError as e:
        error_console.print(f"[red]Error:[/red] {escape(str(e))}")
        raise typer.Exit(1) from None

    async def run() -> tuple[RepositoryRecord, PullRequestOutcome, Usage]:
        async with common.open_source(token) as source:
            repository = await source.resolve(owner, name)
            outcomes = [
                outcome
                async for batch in source.hydrate_pull_requests(repository, [pr])
                for outcome in batch
            ]
            return repository, outcomes[0], source.usage

    repository, outcome, usage = run_async_command(run(), error_prefix="GitHub test failed")
    console.print(
        f"{repository.full_name}: default branch {repository.default_branch}, "
        f"{len(repository.label_names)} labels"
        f"{', private' if repository.is_private else ''}"
        f"{', archived' if repository.is_archived else ''}"
    )
    match outcome:
        case Gone():
            error_console.print(
                f"[red]Error:[/red] #{pr} not found in {escape(repository.full_name)}"
            )
            raise typer.Exit(1)
        case Failed(error=error):
            error_console.print(f"[red]Error:[/red] {escape(str(error))}")
            raise typer.Exit(1)
        case Hydrated(record=record):
            author = record.author.login if record.author is not None else "none returned by GitHub"
            totals = record.totals
            console.print(f"#{record.number} {record.state}: {escape(record.title)}")
            console.print(f"  author {author}, created {_time(record.created_at)}")
            console.print(
                f"  labels {totals.labels}, closing issues {totals.closing_issues}, "
                f"reviews {totals.reviews}, comments {totals.comments}, "
                f"commits {totals.commits} ({record.commits_source}), files {totals.files}"
            )
            console.print(
                f"  GraphQL points {usage.graphql_points} in {usage.graphql_requests} "
                f"requests, REST calls {usage.rest_calls}"
            )


def _time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")
