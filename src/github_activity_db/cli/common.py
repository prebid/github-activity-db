"""Common CLI option factories and helpers.

This module centralizes reusable CLI options to reduce duplication
and consolidate noqa comments for Typer's required function call pattern.

It also provides:
- `run_async_command`: Unified async execution with error handling for CLI commands
- Repository argument type aliases for consistent repo input handling
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Annotated, TypeVar

import typer
from rich.console import Console

from github_activity_db.github.sync.enums import OutputFormat

# Shared console instance for CLI output
console = Console()

T = TypeVar("T")


def run_async_command(
    coro: Coroutine[object, object, T],
    *,
    error_prefix: str = "Error",
) -> T:
    """Execute async code from synchronous CLI command with unified error handling.

    Uses asyncio.run() for clean event loop management. Catches exceptions,
    prints user-friendly error messages, and exits with code 1.

    Args:
        coro: Async coroutine to execute
        error_prefix: Prefix for error messages (default: "Error")

    Returns:
        Result from the coroutine

    Raises:
        typer.Exit: Re-raised from deliberate exits, or raised with code 1 on error

    Example:
        async def _sync() -> dict[str, Any]:
            async with GitHubClient() as client:
                return await client.get_rate_limit()

        result = run_async_command(_sync(), error_prefix="Sync failed")
    """
    try:
        return asyncio.run(coro)
    except typer.Exit:
        # Re-raise deliberate exits (e.g., from validation helpers)
        raise
    except Exception as e:
        console.print(f"[red]{error_prefix}:[/red] {e}")
        raise typer.Exit(1) from None

# Typer requires function calls as default arguments, which triggers B008.
# Using Annotated with a centralized type alias keeps the noqa in one place.

OutputFormatOption = Annotated[
    OutputFormat,
    typer.Option(
        "--format",
        "-f",
        help="Output format",
    ),
]
"""Output format option type for CLI commands.

Usage:
    def command(output_format: OutputFormatOption = OutputFormat.TEXT):
"""

DryRunOption = Annotated[
    bool,
    typer.Option(
        "--dry-run",
        help="Don't write to database, just show what would happen",
    ),
]
"""Dry-run option type for CLI commands.

Usage:
    def command(dry_run: DryRunOption = False):
"""

# -----------------------------------------------------------------------------
# Repository Argument/Option Factories
# -----------------------------------------------------------------------------

RepoArgument = Annotated[
    str,
    typer.Argument(
        help="Repository in owner/name format (e.g., prebid/prebid-server)",
    ),
]
"""Required positional repository argument.

Usage:
    def sync_pr(repo: RepoArgument, pr_number: int) -> None:
"""

RepoFilterOption = Annotated[
    str | None,
    typer.Option(
        "--repo",
        "-r",
        help="Filter by repository (owner/name format)",
    ),
]
"""Optional repository filtering option.

Usage:
    def sync_retry(repo: RepoFilterOption = None) -> None:
"""

ReposListOption = Annotated[
    str | None,
    typer.Option(
        "--repos",
        "-r",
        help="Comma-separated list of repos (owner/repo). "
        "If not specified, uses tracked repositories.",
    ),
]
"""Comma-separated repository list override option.

Usage:
    def sync_all(repos: ReposListOption = None) -> None:
"""


# -----------------------------------------------------------------------------
# Repository Validation Helpers
# -----------------------------------------------------------------------------


def validate_repo(repo: str) -> tuple[str, str]:
    """Parse and validate a single repository string.

    Args:
        repo: Repository string in owner/name format

    Returns:
        Tuple of (owner, name)

    Raises:
        typer.Exit(1): If format is invalid
    """
    from github_activity_db.schemas import parse_repo_string

    try:
        return parse_repo_string(repo)
    except ValueError:
        console.print("[red]Error:[/red] Repository must be in owner/name format")
        raise typer.Exit(1) from None


def validate_repo_list(repos_str: str | None) -> list[str] | None:
    """Parse and validate comma-separated repository list.

    Args:
        repos_str: Comma-separated repos or None

    Returns:
        List of validated repo strings, or None if input was None

    Raises:
        typer.Exit(1): If any repo format is invalid
    """
    if repos_str is None:
        return None

    from github_activity_db.schemas import parse_repo_string

    repo_list = [r.strip() for r in repos_str.split(",") if r.strip()]

    for repo in repo_list:
        try:
            parse_repo_string(repo)
        except ValueError:
            console.print(
                f"[red]Error:[/red] Repository '{repo}' must be in owner/name format"
            )
            raise typer.Exit(1) from None

    return repo_list
