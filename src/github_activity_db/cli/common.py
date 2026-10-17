"""Shared CLI helpers: async execution, repository names, the GitHub token, the source layer."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine

import typer
from rich.console import Console
from rich.markup import escape

from github_activity_db.config import get_settings
from github_activity_db.source import GitHubSource

console = Console()
error_console = Console(stderr=True)


def run_async_command[T](
    coro: Coroutine[object, object, T],
    *,
    error_prefix: str = "Error",
) -> T:
    """Run a command's coroutine; any exception other than ``typer.Exit`` prints and exits 1."""
    try:
        return asyncio.run(coro)
    except typer.Exit:
        raise
    except Exception as e:
        error_console.print(f"[red]{escape(error_prefix)}:[/red] {escape(str(e))}")
        raise typer.Exit(1) from None


def parse_repo_string(full_name: str) -> tuple[str, str]:
    """Split ``owner/name`` into its two segments.

    Raises:
        ValueError: not exactly two non-empty segments, or whitespace in either.
    """
    segments = full_name.split("/")
    if len(segments) != 2 or not all(segments) or any(c.isspace() for c in full_name):
        raise ValueError(f"repository must be owner/name: {full_name!r}")
    return segments[0], segments[1]


def github_token() -> str:
    """The configured GitHub token, for a command that calls GitHub; exits 1 when none is set."""
    token = get_settings().github_token
    value = token.get_secret_value().strip() if token is not None else ""
    if not value:
        error_console.print(
            "[red]Error:[/red] GITHUB_TOKEN is not set "
            '(for example GITHUB_TOKEN="$(gh auth token)")'
        )
        raise typer.Exit(1)
    return value


def open_source(token: str) -> GitHubSource:
    """The source layer a command reads GitHub through; tests replace it to inject a transport."""
    return GitHubSource(token)
