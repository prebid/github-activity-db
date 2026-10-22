"""The root CLI: global flags, exit codes for command lines that never run, and tracebacks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

import pydantic_settings.sources.providers.dotenv as dotenv_source
import pytest
import typer
from typer.testing import CliRunner

from github_activity_db.cli import common
from github_activity_db.cli.app import ExitCodeGroup, app
from github_activity_db.config import get_settings

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
GHACTIVITY = Path(sys.executable).parent / "ghactivity"

# typer forces Rich terminal styling when GITHUB_ACTIONS, FORCE_COLOR, or PY_COLORS is set,
# which splits option names with ANSI escape sequences. Assert on the text without styling.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX_DRAWING = re.compile(r"[\u2500-\u257f]")


def _plain(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


def _words(text: str) -> str:
    """The output's words, without styling, Rich panel borders, or line layout."""
    return " ".join(_BOX_DRAWING.sub(" ", _plain(text)).split())


# A usage error is click's own: its message names the problem. Each case asserts the message,
# so an exit 1 from the token check or from a crash does not pass for one.
USAGE_ERRORS = [
    pytest.param(["--no-such-option"], "No such option: --no-such-option", id="unknown option"),
    pytest.param(["no-such-command"], "No such command 'no-such-command'", id="unknown command"),
    pytest.param(["github", "test"], "Missing option '--pr'", id="missing required option"),
    pytest.param(["github", "test", "--pr", "0"], "Invalid value for '--pr'", id="out of range"),
    pytest.param(
        ["github", "test", "--pr", "1", "--pr", "x"],
        "Invalid value for '--pr'",
        id="value not a number",
    ),
]


def _has_option(help_text: str, option: str) -> bool:
    """True when the option appears as a whole token (so "-v" is not matched inside "--verbose")."""
    return re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", help_text) is not None


def _no_requests(token: str) -> object:
    raise AssertionError("this command line must not reach GitHub")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No .env and fresh settings; tests/conftest.py unsets the shell's settings variables."""
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestGlobalFlags:
    def test_help_lists_verbose_and_quiet(self) -> None:
        result = runner.invoke(app, ["--help"])
        help_text = _plain(result.stdout)
        assert result.exit_code == 0
        for option in ("-v", "--verbose", "-q", "--quiet"):
            assert _has_option(help_text, option), option

    def test_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "ghactivity version" in result.stdout

    def test_the_github_and_sync_commands_are_listed_and_the_removed_ones_are_not(self) -> None:
        help_text = _plain(runner.invoke(app, ["--help"]).stdout)
        for command in ("github", "sync"):
            assert _has_option(help_text, command), command
        for removed in ("search", "tags"):
            assert not _has_option(help_text, removed), removed


class TestExitCodes:
    """A command line that never starts its work exits 1, not click's 2 (plan 5.3 step 8)."""

    @pytest.mark.parametrize(("args", "message"), USAGE_ERRORS)
    def test_usage_errors_exit_1(
        self, args: list[str], message: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A token is set and GitHub unreachable, so neither the token check nor a request
        # can be what exits.
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        monkeypatch.setattr(common, "open_source", _no_requests)
        result = runner.invoke(app, args)
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit), result.exception
        assert message in _words(result.stderr)

    def test_a_missing_token_exits_1(self) -> None:
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 1
        assert "GITHUB_TOKEN is not set" in _plain(result.stderr)

    @pytest.mark.parametrize("separator", ["\n", " ", "\t", "\x1b"])
    def test_a_token_with_whitespace_or_a_control_character_exits_1_unshown(
        self, separator: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", f"ghp_SECRETPART1{separator}SECRETPART2")
        monkeypatch.setattr(common, "open_source", _no_requests)
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 1
        assert "GITHUB_TOKEN contains whitespace or a control character" in _words(result.stderr)
        assert "SECRETPART" not in result.stdout + result.stderr

    def test_an_invalid_tracking_config_exits_1_without_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = tmp_path / "tracking.toml"
        config.write_text("[[repositories]]\nowner = 'prebid'\n")
        monkeypatch.setenv("TRACKING_CONFIG_PATH", str(config))
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        result = runner.invoke(app, ["github", "test", "--pr", "1"])
        assert result.exit_code == 1
        stderr = _plain(result.stderr)
        assert "invalid tracking config" in stderr
        assert "[type=missing" in stderr  # pydantic's text survives, not read as markup
        assert isinstance(result.exception, SystemExit), result.exception

    @pytest.mark.parametrize(
        ("name", "value"),
        [("LOG_LEVEL", "LOUD"), ("LOGGING", "notjson"), ("LOGGING__SERIALIZE", "maybe")],
        ids=["invalid value", "unparsable JSON", "invalid nested value"],
    )
    def test_invalid_settings_exit_1(
        self, name: str, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, value)
        result = runner.invoke(app, ["github", "rate-limit"])
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit), result.exception
        assert "invalid settings" in _plain(result.stderr)

    def test_a_commands_own_exit_code_passes_through(self) -> None:
        commands = typer.Typer(cls=ExitCodeGroup)

        @commands.command()
        def partial() -> None:
            raise typer.Exit(2)

        @commands.command()
        def done() -> None:
            pass

        assert runner.invoke(commands, ["partial"]).exit_code == 2
        assert runner.invoke(commands, ["done"]).exit_code == 0
        assert runner.invoke(commands, ["--nope"]).exit_code == 1

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            pytest.param(EOFError, 1, id="end of input"),
            pytest.param(KeyboardInterrupt, 130, id="interrupt"),
        ],
    )
    def test_an_aborted_command_line(self, error: type[BaseException], code: int) -> None:
        """click turns end of input into Abort, which exits 1 here; typer turns an interrupt
        into exit 130 before click sees it."""
        commands = typer.Typer(cls=ExitCodeGroup)

        @commands.command()
        def ask() -> None:
            raise error

        @commands.command()
        def other() -> None:
            pass

        result = runner.invoke(commands, ["ask"])
        assert result.exit_code == code, result.output
        assert isinstance(result.exception, SystemExit), result.exception


def _installed(
    *args: str, env: dict[str, str], cwd: Path, script: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installed console script, or ``script`` with the same interpreter, in an
    environment built from ``env``, ``PATH``, and ``cwd`` as ``HOME``."""
    base = {"PATH": os.environ["PATH"], "HOME": str(cwd)}
    command = [str(GHACTIVITY), *args] if script is None else [sys.executable, "-I", "-c", script]
    return subprocess.run(
        command, capture_output=True, text=True, env={**base, **env}, cwd=cwd, timeout=60
    )


class TestInstalledEntryPoint:
    """The console script runs the same group: the same exit codes outside CliRunner."""

    def test_the_console_script_is_installed(self) -> None:
        assert GHACTIVITY.exists(), GHACTIVITY

    @pytest.mark.parametrize(("args", "message"), USAGE_ERRORS)
    def test_usage_errors_exit_1(self, args: list[str], message: str, tmp_path: Path) -> None:
        result = _installed(*args, env={}, cwd=tmp_path)
        assert result.returncode == 1, result.stderr
        assert message in _words(result.stderr)
        assert "Traceback" not in result.stderr

    def test_unparsable_settings_exit_1_without_a_traceback(self, tmp_path: Path) -> None:
        result = _installed("github", "rate-limit", env={"LOGGING": "notjson"}, cwd=tmp_path)
        assert result.returncode == 1
        assert "invalid settings" in _words(result.stderr)
        assert "Traceback" not in result.stderr

    def test_an_env_file_that_is_not_utf8_exits_1_without_a_traceback(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_bytes(b"LOG_LEVEL=\xff\n")
        result = _installed("github", "rate-limit", env={}, cwd=tmp_path)
        assert result.returncode == 1
        assert "invalid settings" in _words(result.stderr)
        assert "Traceback" not in result.stderr


def test_an_env_file_the_os_refuses_to_read_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env the OS refuses to read: the read answers PermissionError, as it does for a file
    the user may not read, whatever uid runs the tests (D40), and the command exits 1."""
    (tmp_path / ".env").write_text("LOG_LEVEL=INFO\n")
    read: list[Path] = []

    def refused(path: Path, **_: object) -> dict[str, str | None]:
        read.append(Path(path))
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dotenv_source, "dotenv_values", refused)
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["github", "rate-limit"])
    finally:
        get_settings.cache_clear()
    assert read == [Path(".env")]
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param({"LOGGING__ROTATION": "garbage"}, id="rotation"),
            pytest.param({"LOGGING__RETENTION": "garbage"}, id="retention"),
            pytest.param({"LOGGING__LOG_FILE": "/dev/null/x.log"}, id="log file"),
        ],
    )
    def test_a_log_file_that_cannot_be_set_up_exits_1_without_a_traceback(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        settings = {"LOGGING__LOG_FILE": str(tmp_path / "x.log"), **env}
        result = _installed("github", "rate-limit", env=settings, cwd=tmp_path)
        assert result.returncode == 1
        assert "cannot set up logging" in _words(result.stderr)
        assert "Traceback" not in result.stderr

    def test_a_missing_token_exits_1(self, tmp_path: Path) -> None:
        result = _installed("github", "rate-limit", env={}, cwd=tmp_path)
        assert result.returncode == 1
        assert "GITHUB_TOKEN is not set" in _plain(result.stderr)

    def test_an_invalid_tracking_config_exits_1_without_a_traceback(self, tmp_path: Path) -> None:
        config = tmp_path / "tracking.toml"
        config.write_text("not = [valid\n")
        result = _installed(
            "github",
            "test",
            "--pr",
            "1",
            env={"TRACKING_CONFIG_PATH": str(config), "GITHUB_TOKEN": "test-token"},
            cwd=tmp_path,
        )
        assert result.returncode == 1
        assert "not valid TOML" in _plain(result.stderr)
        assert "Traceback" not in result.stderr

    def test_a_traceback_shows_no_local_variables(self) -> None:
        """An uncaught exception's traceback is typer's, set up by the app it was called
        through: the console script calls the root app, whose setting keeps local variables out
        whatever group the command is in (gap 75). The 2 values compared: the script's target
        and the setting."""
        (script,) = metadata.entry_points(group="console_scripts", name="ghactivity")
        assert script.load() is app
        assert app.pretty_exceptions_show_locals is False
