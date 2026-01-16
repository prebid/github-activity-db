"""Main CLI application for GitHub Activity DB."""

import typer
from rich.console import Console

from github_activity_db import __version__
from github_activity_db.cli import github as github_cmd
from github_activity_db.cli import sync as sync_cmd

app = typer.Typer(
    name="ghactivity",
    help="Searchable data store for GitHub PR data with custom tagging.",
    add_completion=False,
)
console = Console()


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"ghactivity version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """GitHub Activity DB - Store and search GitHub PR data."""
    pass


@app.command()
def search(query: str = typer.Argument(default="", help="Search query")) -> None:
    """Search stored PR data."""
    console.print(f"[yellow]Search for '{query}' not yet implemented.[/yellow]")


@app.command()
def tags() -> None:
    """Manage custom tags on PRs."""
    console.print("[yellow]Tags command not yet implemented.[/yellow]")


# Register subcommands
app.add_typer(github_cmd.app, name="github")
app.add_typer(sync_cmd.app, name="sync")


if __name__ == "__main__":
    app()
