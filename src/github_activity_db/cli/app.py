"""Main CLI application for GitHub Activity DB."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn

import click
import typer
from rich.console import Console
from rich.markup import escape
from typer import rich_utils
from typer.core import TyperGroup

from github_activity_db import __version__
from github_activity_db.cli import github as github_cmd
from github_activity_db.cli import sync as sync_cmd
from github_activity_db.cli.common import error_console
from github_activity_db.config import get_settings
from github_activity_db.logging import setup_logging
from github_activity_db.tracking import TrackingConfigError


class ExitCodeGroup(TyperGroup):
    """A command line that never starts its work exits 1 (redesign plan 5.3 step 8, gap 69).

    click exits 2 on a usage error, the code a scheduled run publishes on (D25). Usage errors, an
    invalid tracking.toml, invalid settings, and a log file that cannot be set up exit 1 here; a
    command's own exit code passes through, and an interrupt exits 130 (typer turns
    KeyboardInterrupt into it before click's Abort). Output that cannot be written (stdout or
    stderr a closed pipe) never changes the exit code. CliRunner tests and the installed script
    both run this ``main``.
    """

    def main(self, *args: Any, **kwargs: Any) -> NoReturn:
        kwargs["standalone_mode"] = False
        try:
            try:
                code = super().main(*args, **kwargs)
            except click.ClickException as e:
                rich_utils.rich_format_error(e)
                code = 1
            except click.Abort:
                rich_utils.rich_abort_error()
                code = 1
            except TrackingConfigError as e:
                error_console.print(f"[red]Error:[/red] {escape(str(e))}", highlight=False)
                code = 1
        finally:
            _discard_unwritable_output()
        sys.exit(code if isinstance(code, int) else 0)


def _discard_unwritable_output() -> None:
    """Flush stdout and stderr, pointing one that cannot take its buffered output (a closed
    pipe) at /dev/null: the interpreter flushes both again at exit, and a failure there would
    replace the exit code with 120."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            # ValueError: the stream is closed, or has no file descriptor to point elsewhere.
            with contextlib.suppress(OSError, ValueError):
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, stream.fileno())
                os.close(devnull)


app = typer.Typer(
    name="ghactivity",
    cls=ExitCodeGroup,
    help="Searchable data store for GitHub PR data from Prebid repositories.",
    add_completion=False,
    # Local variables in a traceback can hold credentials; Step 6 logs are public (gap 75).
    pretty_exceptions_show_locals=False,
)
console = Console()


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"ghactivity version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable debug logging.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Suppress non-error output (WARNING level).",
        ),
    ] = False,
) -> None:
    """GitHub Activity DB - Store and search GitHub PR data."""
    try:
        settings = get_settings()
    # ValueError: pydantic's ValidationError, an unparsable JSON value (SettingsError), and a
    # .env that is not UTF-8; OSError: a .env that cannot be read.
    except (ValueError, OSError) as e:
        error_console.print(
            f"[red]Error:[/red] invalid settings:\n{escape(str(e))}", highlight=False
        )
        raise typer.Exit(1) from None
    log_config = settings.logging

    try:
        setup_logging(
            level=settings.log_level,
            verbose=verbose,
            quiet=quiet,
            log_file=Path(log_config.log_file) if log_config.log_file else None,
            rotation=log_config.rotation,
            retention=log_config.retention,
            serialize=log_config.serialize,
        )
    # loguru's ValueError for a rotation or retention it cannot parse, OSError for a log file
    # it cannot open.
    except (ValueError, OSError) as e:
        error_console.print(
            f"[red]Error:[/red] cannot set up logging: {escape(str(e))}", highlight=False
        )
        raise typer.Exit(1) from None


app.add_typer(github_cmd.app, name="github")
app.command("sync")(sync_cmd.sync)


if __name__ == "__main__":
    app()
