"""The root CLI: global flags, exit codes for command lines that never run, and tracebacks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

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

    def test_only_the_github_commands_remain(self) -> None:
        help_text = _plain(runner.invoke(app, ["--help"]).stdout)
        assert "github" in help_text
        for removed in ("sync", "search", "tags"):
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
        planted = typer.Typer(cls=ExitCodeGroup)

        @planted.command()
        def partial() -> None:
            raise typer.Exit(2)

        @planted.command()
        def done() -> None:
            pass

        assert runner.invoke(planted, ["partial"]).exit_code == 2
        assert runner.invoke(planted, ["done"]).exit_code == 0
        assert runner.invoke(planted, ["--nope"]).exit_code == 1

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
        planted = typer.Typer(cls=ExitCodeGroup)

        @planted.command()
        def ask() -> None:
            raise error

        @planted.command()
        def other() -> None:
            pass

        result = runner.invoke(planted, ["ask"])
        assert result.exit_code == code, result.output
        assert isinstance(result.exception, SystemExit), result.exception


def _installed(
    *args: str, env: dict[str, str], cwd: Path, script: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installed console script, or ``script`` with the same interpreter."""
    base = {
        key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "SYSTEMROOT")
    }
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

    @pytest.mark.parametrize(
        ("dotenv", "mode"),
        [
            pytest.param(b"LOG_LEVEL=\xff\n", 0o644, id="not UTF-8"),
            pytest.param(b"", 0o000, id="unreadable"),
        ],
    )
    def test_an_unreadable_env_file_exits_1_without_a_traceback(
        self, tmp_path: Path, dotenv: bytes, mode: int
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_bytes(dotenv)
        env_file.chmod(mode)
        try:
            result = _installed("github", "rate-limit", env={}, cwd=tmp_path)
        finally:
            env_file.chmod(0o644)
        assert result.returncode == 1
        assert "invalid settings" in _words(result.stderr)
        assert "Traceback" not in result.stderr

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

    def test_a_traceback_shows_no_local_variables(self, tmp_path: Path) -> None:
        """The real app's pretty traceback, for a planted command that fails (gap 75)."""
        script = (
            "from github_activity_db.cli.app import app\n"
            "@app.command()\n"
            "def boom() -> None:\n"
            "    credential = 'sentinel-credential-7f3a'\n"
            "    raise RuntimeError('boom ' + str(len(credential)))\n"
            "app(['boom'], prog_name='ghactivity')\n"
        )
        result = _installed(env={}, cwd=tmp_path, script=script)
        assert result.returncode == 1
        assert "RuntimeError" in result.stderr
        assert "boom 24" in result.stderr
        assert "sentinel-credential-7f3a" not in result.stderr + result.stdout
