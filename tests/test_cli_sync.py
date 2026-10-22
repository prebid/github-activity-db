"""`ghactivity sync` over the engine, from go-gdpr's first-sync recordings (redesign plan 5.3
step 8, 5.7; Step 3b-2a-2c).

In process through CliRunner, and through the installed package in a child interpreter
(``python -I -c``), each with the command's own source factory, whose HTTP transport the replay
replaces, and the fake clock: a run that never starts exits 1 before any
``sync_runs`` row, a run that starts exits with the engine's code, and a run killed mid-run leaves
its row ``running`` with its committed items for a resume.
"""

from __future__ import annotations

import functools
import hashlib
import io
import os
import re
import resource
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from github_activity_db.cli import common
from github_activity_db.cli import sync as sync_cli
from github_activity_db.cli.app import app
from github_activity_db.config import get_settings
from github_activity_db.db.schema import HEAD_REVISION, MIGRATION_REVISIONS
from github_activity_db.source import client
from github_activity_db.sync import engine
from github_activity_db.sync.deletions import parse_allow_deletions
from github_activity_db.sync.engine import SyncOptions, SyncReport
from tests.db.alembic_support import upgrade
from tests.source.conftest import FakeClock
from tests.source.recorder import FIRST_SYNC_REPOSITORY
from tests.source.recordings import FIRST_SYNC
from tests.source.replay import Replay, unanswered
from tests.sync.records import fetch

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX_DRAWING = re.compile(r"[─-╿]")
OWNER, NAME = FIRST_SYNC_REPOSITORY
TRACKING = f"""\
[coverage]
floor = 2023-01-01T00:00:00Z

[[repositories]]
owner = "{OWNER}"
name = "{NAME}"
group = "Server"
"""


def _plain(text: str) -> str:
    return " ".join(_BOX_DRAWING.sub(" ", _ANSI_ESCAPE.sub("", text)).split())


def _alias_error(body: dict[str, Any]) -> None:
    """The first pull request of a batch answers an error of its own: an item failure."""
    body["data"]["repository"]["p0"] = None
    body["errors"] = [
        {
            "type": "FORBIDDEN",
            "message": "Resource not accessible by integration",
            "path": ["repository", "p0"],
        }
    ]


def _runs(database: Path) -> list[dict[str, Any]]:
    return fetch(database, "SELECT id, status, exit_code FROM sync_runs ORDER BY id")


def _execute(database: Path, sql: str) -> None:
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute(sql)
    finally:
        connection.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def tracking(tmp_path: Path) -> Path:
    path = tmp_path / "tracking.toml"
    path.write_text(TRACKING)
    return path


@pytest.fixture
def replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_database: Path, tracking: Path
) -> Iterator[Replay]:
    """``ghactivity sync`` reads GitHub through go-gdpr's first-sync recordings, on the fake clock,
    into a migrated database; the replay must answer every request it gets. ``common.open_source``
    and ``GitHubSource`` are the command's own, with the replay as their HTTP transport; the
    report prints on a plain console, so ``_ReportConsole`` runs only in the child tests.
    ``GITHUB_SHA`` names the code version, so no git runs, and ``GITHUB_EVENT_NAME`` is unset, so
    the trigger is ``manual`` whatever the shell or workflow running the tests sets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "test")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{migrated_database}")
    monkeypatch.setenv("TRACKING_CONFIG_PATH", str(tracking))
    monkeypatch.setattr(sync_cli, "console", Console(width=200))
    get_settings.cache_clear()
    recorded = Replay.from_directory(FIRST_SYNC)
    clock = FakeClock()
    monkeypatch.setattr(common, "clock", lambda: clock)
    build_github = client.build_github
    monkeypatch.setattr(
        client,
        "build_github",
        lambda auth, *, http_transport=None: build_github(
            auth, http_transport=recorded.transport()
        ),
    )
    yield recorded
    get_settings.cache_clear()
    assert unanswered(recorded) == []


# The requests of go-gdpr's first sync after its resolve.
HYDRATING = ["PullRequestIndex", "PullRequestBatch", "UserLookup"]


def _sync(*args: str) -> Any:
    return runner.invoke(app, ["sync", *args])


# --------------------------------------------------------------------------------------------
# Runs that never start
# --------------------------------------------------------------------------------------------
USAGE_ERRORS = [
    pytest.param(
        ["--max-minutes", "1", "--max-minutes", "2"],
        "--max-minutes is given more than once",
        id="max-minutes twice",
    ),
    pytest.param(
        ["--allow-deletions", f"{OWNER}/{NAME}=1", "--allow-deletions", f"{OWNER}/{NAME}=2"],
        "--allow-deletions is given more than once",
        id="allow-deletions twice",
    ),
    pytest.param(
        ["--dry-run", "--break-lock"], "--break-lock does not apply", id="dry run with break lock"
    ),
    pytest.param(["--repo", "prebid/nope"], "which tracking.toml does not list", id="unknown repo"),
    pytest.param(
        ["--repo", "prebid/g\u043e-gdpr"], "--repo takes an ASCII owner/name", id="non-ASCII repo"
    ),
    pytest.param(
        ["--allow-deletions", "prebid/nope=3"],
        "which tracking.toml does not list",
        id="unknown approval",
    ),
    pytest.param(["--allow-deletions", f"{OWNER}/{NAME}=0"], "N of at least 1", id="approval of 0"),
    pytest.param(["--max-minutes", "0"], "0 is not in the range x>=1", id="max-minutes 0"),
    pytest.param(["--unknown"], "No such option: --unknown", id="unknown option"),
]


@pytest.mark.parametrize(("args", "message"), USAGE_ERRORS)
def test_a_usage_error_exits_1_before_any_request_or_row(
    replay: Replay, migrated_database: Path, args: list[str], message: str
) -> None:
    before = _digest(migrated_database)

    result = _sync(*args)

    assert result.exit_code == 1, result.output
    assert message in _plain(result.output)
    assert replay.seen == []
    assert _digest(migrated_database) == before


def test_a_missing_token_exits_1(replay: Replay, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    get_settings.cache_clear()

    result = _sync()

    assert result.exit_code == 1
    assert "GITHUB_TOKEN is not set" in _plain(result.output)
    assert replay.seen == []


def test_a_missing_database_file_exits_1_and_creates_none(
    replay: Replay, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{missing}")
    get_settings.cache_clear()

    for args in ([], ["--dry-run"]):
        result = _sync(*args)
        assert result.exit_code == 1, result.output
        assert "Sync did not start: database file not found" in _plain(result.output)

    assert not missing.exists()
    assert replay.seen == []


def test_a_database_not_at_head_exits_1(
    replay: Replay, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file the migrations built to the revision before head, as the code before it did."""
    older = tmp_path / "older.db"
    upgrade(older, MIGRATION_REVISIONS[-2])
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{older}")
    get_settings.cache_clear()

    result = _sync()

    assert result.exit_code == 1
    assert (
        f"Sync did not start: the database is at revision {MIGRATION_REVISIONS[-2]}, not the "
        f"code's head {HEAD_REVISION}" in _plain(result.output)
    )
    assert _runs(older) == []
    assert replay.seen == []


def test_a_refused_claim_exits_1_without_a_row(replay: Replay, migrated_database: Path) -> None:
    _execute(
        migrated_database,
        "INSERT INTO sync_runs (started_at, status, trigger, code_version, config_source, "
        "config_sha256, graphql_points, rest_calls, items_planned, items_hydrated, "
        "items_deleted, failures) VALUES ('2026-10-17', 'running', 'manual', 'x', 'test', "
        "'0', 0, 0, 0, 0, 0, 0)",
    )

    result = _sync()

    assert result.exit_code == 1
    assert "Sync did not start: run 1 is running; --break-lock marks it aborted" in _plain(
        result.output
    )
    assert _runs(migrated_database) == [{"id": 1, "status": "running", "exit_code": None}]
    assert replay.seen == []


# --------------------------------------------------------------------------------------------
# Runs that start
# --------------------------------------------------------------------------------------------
def test_a_first_sync_exits_0_and_prints_its_report(
    replay: Replay, migrated_database: Path
) -> None:
    result = _sync()

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Run 1: complete, exit 0" in output
    assert f"{OWNER}/{NAME}: complete (new) planned 11, in-place updates 0" in output
    # Resolve, index, the batch, and one account lookup, at the replay's cost of 1 each.
    assert "Hydrated 11 items; GraphQL points 4, REST calls 0" in output
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": 0}]
    assert fetch(
        migrated_database,
        "SELECT items_hydrated, graphql_points, rest_calls, trigger, code_version FROM sync_runs",
    ) == [
        {
            "items_hydrated": 11,
            "graphql_points": 4,
            "rest_calls": 0,
            "trigger": "manual",
            "code_version": "test",
        }
    ]
    assert fetch(migrated_database, "SELECT count(*) AS n FROM pull_requests") == [{"n": 11}]


def test_an_item_failure_exits_2_and_is_reported(replay: Replay, migrated_database: Path) -> None:
    replay.modify(_alias_error, when=lambda seen: seen.operation == "PullRequestBatch", times=2)

    result = _sync()

    assert result.exit_code == 2, result.output
    output = _plain(result.output)
    assert "Run 1: complete, exit 2" in output
    assert re.search(rf"Failed: {OWNER}/{NAME}#\d+: AliasError: ", output), output
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": 2}]


def test_a_dry_run_prints_its_plan_and_writes_nothing(
    replay: Replay, migrated_database: Path
) -> None:
    before = _digest(migrated_database)

    result = _sync("--dry-run")

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "Dry run: the run would exit 0" in output
    assert len(re.findall(r"hydrate #\d+: not stored", output)) == 11
    assert _digest(migrated_database) == before


def test_repo_names_a_configured_repository_ignoring_case(
    replay: Replay, migrated_database: Path
) -> None:
    result = _sync("--repo", f"{OWNER.upper()}/{NAME.upper()}")

    assert result.exit_code == 0, result.output
    assert f"{OWNER}/{NAME}: complete" in _plain(result.output)


# --------------------------------------------------------------------------------------------
# Through the installed package
# --------------------------------------------------------------------------------------------
CHILD = """\
import sys
sys.path.insert(0, {root!r})
from github_activity_db.cli import common
from github_activity_db.source import client
from tests.source.conftest import FakeClock
from tests.source.recordings import FIRST_SYNC
from tests.source.replay import Replay
replay = Replay.from_directory(FIRST_SYNC)
clock = FakeClock()
common.clock = lambda: clock
build_github = client.build_github
client.build_github = lambda auth, *, http_transport=None: build_github(
    auth, http_transport=replay.transport()
)
{setup}
from github_activity_db.cli.app import app
app({args!r}, prog_name="ghactivity")
"""

FAIL_ONE_ITEM = """\
from tests.test_cli_sync import _alias_error
replay.modify(_alias_error, when=lambda seen: seen.operation == "PullRequestBatch", times=2)
"""

# The writer commits each item in its own transaction; the child kills itself right after the
# third commit returns, as a crash or SIGKILL would.
KILL_AFTER_THIRD_ITEM = """\
import os, signal
from github_activity_db.sync import writer
written = writer.write_pull_request
count = 0
async def write_then_crash(*args, **kwargs):
    global count
    result = await written(*args, **kwargs)
    count += 1
    if count == 3:
        os.kill(os.getpid(), signal.SIGKILL)
    return result
writer.write_pull_request = write_then_crash
"""


def _child(
    args: list[str],
    *,
    database: Path,
    tracking: Path,
    cwd: Path,
    setup: str = "",
    stdout: int = subprocess.PIPE,
    stderr: int = subprocess.PIPE,
    preexec_fn: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the installed package in an isolated interpreter (``-I``: no user site, no
    PYTHONPATH, no working directory on the path), over the recordings, after ``setup``; its
    stdout and stderr are captured unless ``stdout`` or ``stderr`` names a file descriptor, and
    ``preexec_fn`` runs in the child before the interpreter starts. The environment is built
    from fixed values and ``PATH``: ``HOME`` is ``cwd``, and ``GITHUB_SHA`` names the code
    version, so no git runs."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GITHUB_SHA": "test",
        "GITHUB_TOKEN": "test-token",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "TRACKING_CONFIG_PATH": str(tracking),
    }
    script = CHILD.format(root=str(ROOT), setup=setup, args=["sync", *args])
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        stdout=stdout,
        stderr=stderr,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
        preexec_fn=preexec_fn,
    )


def test_the_installed_command_exits_with_the_engines_code(
    migrated_database: Path, tracking: Path, tmp_path: Path
) -> None:
    failed = _child(
        [], database=migrated_database, tracking=tracking, cwd=tmp_path, setup=FAIL_ONE_ITEM
    )
    assert failed.returncode == 2, failed.stderr
    assert "Run 1: complete, exit 2" in _plain(failed.stdout)
    assert fetch(migrated_database, "SELECT trigger, code_version FROM sync_runs") == [
        {"trigger": "manual", "code_version": "test"}
    ]

    usage = _child(
        ["--dry-run", "--break-lock"], database=migrated_database, tracking=tracking, cwd=tmp_path
    )
    assert usage.returncode == 1, usage.stderr
    assert "--break-lock does not apply" in _plain(usage.stderr)
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": 2}]


@pytest.mark.parametrize(("setup", "code"), [("", 0), (FAIL_ONE_ITEM, 2)], ids=["0", "2"])
def test_a_closed_stdout_keeps_the_runs_exit_code(
    migrated_database: Path, tracking: Path, tmp_path: Path, setup: str, code: int
) -> None:
    """A reader that closed the pipe before the report (``ghactivity sync | true``): the report
    is not printed, stderr says so, and the command exits with the run's code."""
    read, write = os.pipe()
    os.close(read)
    try:
        child = _child(
            [],
            database=migrated_database,
            tracking=tracking,
            cwd=tmp_path,
            setup=setup,
            stdout=write,
        )
    finally:
        os.close(write)

    assert child.returncode == code, child.stderr
    assert child.stderr == "The report could not be printed: BrokenPipeError\n"
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": code}]


@pytest.mark.parametrize(("setup", "code"), [("", 0), (FAIL_ONE_ITEM, 2)], ids=["0", "2"])
def test_closed_stdout_and_stderr_keep_the_runs_exit_code(
    migrated_database: Path, tracking: Path, tmp_path: Path, setup: str, code: int
) -> None:
    """``ghactivity sync 2>&1 | true``: neither stream can be written, and the command still
    exits with the run's code, not the interpreter's 120 for output it could not flush."""
    read, write = os.pipe()
    os.close(read)
    try:
        child = _child(
            [],
            database=migrated_database,
            tracking=tracking,
            cwd=tmp_path,
            setup=setup,
            stdout=write,
            stderr=write,
        )
    finally:
        os.close(write)

    assert child.returncode == code
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": code}]


@pytest.mark.parametrize(("setup", "code"), [("", 0), (FAIL_ONE_ITEM, 2)], ids=["0", "2"])
def test_a_stdout_closed_at_start_keeps_the_runs_exit_code(
    migrated_database: Path, tracking: Path, tmp_path: Path, setup: str, code: int
) -> None:
    """``ghactivity sync >&-``: with fd 1 closed when the interpreter starts, ``sys.stdout`` is
    None and the report goes nowhere; the command exits with the run's code."""
    child = _child(
        [],
        database=migrated_database,
        tracking=tracking,
        cwd=tmp_path,
        setup=setup,
        preexec_fn=functools.partial(os.close, 1),
    )

    assert child.returncode == code, child.stderr
    assert child.stderr == ""
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": code}]


@pytest.mark.parametrize(("setup", "code"), [("", 0), (FAIL_ONE_ITEM, 2)], ids=["0", "2"])
def test_a_stdout_file_that_cannot_grow_keeps_the_runs_exit_code(
    migrated_database: Path, tracking: Path, tmp_path: Path, setup: str, code: int
) -> None:
    """``ghactivity sync > report.txt`` where the file cannot grow (it is at the child's
    RLIMIT_FSIZE, so a write fails as on a full disk): what the failed write left buffered is
    dropped, stderr says so, and the command exits with the run's code, not the interpreter's
    120."""
    limit = 16 * 1024 * 1024
    report = tmp_path / "report.txt"
    with report.open("wb") as file:
        file.truncate(limit)
    stdout = os.open(report, os.O_WRONLY | os.O_APPEND)
    # macOS checks the limit against the descriptor's offset, not the append position.
    os.lseek(stdout, 0, os.SEEK_END)
    try:
        child = _child(
            [],
            database=migrated_database,
            tracking=tracking,
            cwd=tmp_path,
            setup=setup,
            stdout=stdout,
            preexec_fn=functools.partial(resource.setrlimit, resource.RLIMIT_FSIZE, (limit, limit)),
        )
    finally:
        os.close(stdout)

    assert child.returncode == code, child.stderr
    assert child.stderr == "The report could not be printed: OSError\n"
    assert report.stat().st_size == limit
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": code}]


def test_a_killed_run_stays_running_and_a_resume_keeps_its_writes(
    migrated_database: Path, tracking: Path, tmp_path: Path
) -> None:
    killed = _child(
        [], database=migrated_database, tracking=tracking, cwd=tmp_path, setup=KILL_AFTER_THIRD_ITEM
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert _runs(migrated_database) == [{"id": 1, "status": "running", "exit_code": None}]
    committed = fetch(
        migrated_database, "SELECT id, number, hydrated_at FROM pull_requests ORDER BY number"
    )
    assert len(committed) == 3

    # The row still holds the lock: a plain run never starts and adds no row.
    refused = _child([], database=migrated_database, tracking=tracking, cwd=tmp_path)
    assert refused.returncode == 1, refused.stderr
    assert "run 1 is running; --break-lock marks it aborted" in _plain(
        refused.stdout + refused.stderr
    )
    assert len(_runs(migrated_database)) == 1

    resumed = _child(["--break-lock"], database=migrated_database, tracking=tracking, cwd=tmp_path)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "--break-lock marked run 1 aborted" in _plain(resumed.stdout)
    assert _runs(migrated_database) == [
        {"id": 1, "status": "aborted", "exit_code": None},
        {"id": 2, "status": "complete", "exit_code": 0},
    ]
    rows = fetch(
        migrated_database, "SELECT id, number, hydrated_at FROM pull_requests ORDER BY number"
    )
    assert len(rows) == 11
    # The three items the killed run committed are the same rows, and the resume planned and
    # wrote only the other eight: it did not rewrite them.
    assert [row for row in rows if row["number"] in {r["number"] for r in committed}] == committed
    assert fetch(
        migrated_database, "SELECT items_planned, items_hydrated FROM sync_runs WHERE id = 2"
    ) == [{"items_planned": 8, "items_hydrated": 8}]


# A child started with SIGINT ignored (a suite started with & from a non-interactive shell)
# inherits that; Python then installs no handler of its own, so the setup installs Python's
# default, as at a terminal.
INTERRUPT_AFTER_THIRD_ITEM = """\
import os, signal
signal.signal(signal.SIGINT, signal.default_int_handler)
from github_activity_db.sync import writer
written = writer.write_pull_request
count = 0
async def write_then_interrupt(*args, **kwargs):
    global count
    result = await written(*args, **kwargs)
    count += 1
    if count == 3:
        os.kill(os.getpid(), signal.SIGINT)
    return result
writer.write_pull_request = write_then_interrupt
"""


def test_a_sigint_closes_the_run_aborted_and_exits_130(
    migrated_database: Path, tracking: Path, tmp_path: Path
) -> None:
    """A first SIGINT under asyncio.run cancels the run's task: the engine closes the row aborted
    with 130 and re-raises, and the command exits 130 (5.3); the next run claims normally."""
    interrupted = _child(
        [],
        database=migrated_database,
        tracking=tracking,
        cwd=tmp_path,
        setup=INTERRUPT_AFTER_THIRD_ITEM,
    )
    assert interrupted.returncode == 130, interrupted.stdout + interrupted.stderr
    assert _runs(migrated_database) == [{"id": 1, "status": "aborted", "exit_code": 130}]
    assert fetch(migrated_database, "SELECT count(*) AS n FROM pull_requests") == [{"n": 3}]

    resumed = _child([], database=migrated_database, tracking=tracking, cwd=tmp_path)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert _runs(migrated_database)[-1] == {"id": 2, "status": "complete", "exit_code": 0}
    assert fetch(migrated_database, "SELECT count(*) AS n FROM pull_requests") == [{"n": 11}]


# --------------------------------------------------------------------------------------------
# Options reaching the engine
# --------------------------------------------------------------------------------------------
def test_an_invalid_tracking_config_exits_1(replay: Replay, tracking: Path) -> None:
    tracking.write_text("not = [valid\n")

    result = _sync()

    assert result.exit_code == 1
    assert "not valid TOML" in _plain(result.output)
    assert replay.seen == []


def test_break_lock_without_a_running_sync_proceeds_and_says_so(
    replay: Replay, migrated_database: Path
) -> None:
    result = _sync("--break-lock")

    assert result.exit_code == 0, result.output
    assert "--break-lock found no blocking run" in _plain(result.output)
    assert _runs(migrated_database) == [{"id": 1, "status": "complete", "exit_code": 0}]


EMPTY_REPORT = SyncReport(
    dry_run=False,
    run_id=1,
    status="complete",
    exit_code=0,
    repositories=(),
    untracked=(),
    planned=(),
    in_place_updates=(),
    deletions=(),
    pruned=(),
    prune_refused=None,
    failures=(),
    accepted_not_failed=(),
    second_node_ids=(),
    refresh_failures=(),
    items_hydrated=0,
)


@pytest.mark.parametrize("dry_run", [False, True])
def test_every_option_reaches_the_engine(
    replay: Replay,
    monkeypatch: pytest.MonkeyPatch,
    tracking: Path,
    migrated_database: Path,
    dry_run: bool,
) -> None:
    """A spy in the engine's place records the options it is called with."""
    tracking.write_text(
        TRACKING + '\n[[repositories]]\nowner = "prebid"\nname = "other"\ngroup = "Client"\n'
    )
    called: list[SyncOptions] = []

    async def spy(options: SyncOptions, **kwargs: Any) -> SyncReport:
        called.append(options)
        return EMPTY_REPORT

    monkeypatch.setattr(engine, "sync", spy)
    approval = f"{OWNER}/{NAME}=5"
    args = ["--repo", "PREBID/OTHER", "--repo", f"{OWNER}/{NAME}", "--max-minutes", "30"]
    args += ["--allow-deletions", approval, "--prune-untracked"]
    args += ["--dry-run"] if dry_run else ["--break-lock"]

    result = _sync(*args)

    assert result.exit_code == 0, result.output
    (options,) = called
    config = get_settings().tracking_config().config
    entries = config.repositories
    assert options.repositories == entries  # tracking.toml order, both named
    assert (options.max_minutes, options.prune_untracked) == (30, True)
    assert options.allow_deletions == parse_allow_deletions(approval, config)
    assert (options.dry_run, options.break_lock) == (dry_run, not dry_run)
    assert (options.trigger, options.code_version, options.database_url) == (
        "manual",
        "test",
        f"sqlite+aiosqlite:///{migrated_database}",
    )
    assert options.config.source == str(tracking)


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry run"])
@pytest.mark.parametrize("seconds", [59, 60, 61])
def test_max_minutes_stops_requests_at_its_deadline(
    replay: Replay, migrated_database: Path, dry_run: bool, seconds: int
) -> None:
    """--max-minutes reaches the transport through the command's own source factory: when the
    resolve's answer arrives a minute after the run started, no further request is sent and the
    run exits 2; a second earlier, the run goes on to complete."""
    clock = common.clock()
    assert isinstance(clock, FakeClock)  # the replay fixture's
    replay.modify(
        lambda body: clock.advance(seconds), when=lambda seen: seen.operation == "Resolve"
    )

    result = _sync("--max-minutes", "1", *(["--dry-run"] if dry_run else []))

    reached = seconds >= 60
    code = 2 if reached else 0
    status = "time_limited" if reached else "complete"
    operations = ["Resolve"]
    if not reached:
        operations += ["PullRequestIndex"] if dry_run else HYDRATING
    assert result.exit_code == code, result.output
    assert [seen.operation for seen in replay.seen] == operations
    first = _plain(result.output.splitlines()[0])
    assert first == (
        f"Dry run: the run would exit {code}" if dry_run else f"Run 1: {status}, exit {code}"
    )
    assert _runs(migrated_database) == (
        [] if dry_run else [{"id": 1, "status": status, "exit_code": code}]
    )


def test_repo_attempts_only_the_repositories_it_names(
    replay: Replay, tracking: Path, migrated_database: Path
) -> None:
    """With a second configured repository the recordings cannot answer, --repo keeps the run to
    go-gdpr: no request names the other, and the run completes."""
    tracking.write_text(
        TRACKING + '\n[[repositories]]\nowner = "prebid"\nname = "other"\ngroup = "Client"\n'
    )

    result = _sync("--repo", f"{OWNER}/{NAME}")

    assert result.exit_code == 0, result.output
    assert {seen.variables.get("name") for seen in replay.seen} - {None} == {NAME}
    assert "prebid/other" not in _plain(result.output)
    assert fetch(migrated_database, "SELECT full_name FROM repositories") == [
        {"full_name": f"{OWNER}/{NAME}"}
    ]


def test_label_text_never_changes_the_exit_code(replay: Replay, migrated_database: Path) -> None:
    """Brackets split across two label names, which markup would read as a closing tag, are
    printed as written, and the command still exits with the run's code."""
    first = _sync()
    assert first.exit_code == 0, first.output

    def renamed(body: dict[str, Any]) -> None:
        nodes = body["data"]["repository"]["labels"]["nodes"]
        for node, name in zip(nodes, ("x[/", "y]"), strict=False):
            node["name"] = name

    replay.modify(renamed, when=lambda seen: seen.operation == "Resolve")

    result = _sync()

    assert result.exit_code == 0, result.output
    assert _runs(migrated_database)[-1] == {"id": 2, "status": "complete", "exit_code": 0}
    output = _plain(result.output)
    assert "-> x[/" in output and "-> y]" in output, output


def test_an_html_error_page_prints_as_one_line(replay: Replay) -> None:
    page = "<!DOCTYPE html>\n<html>\n<body>\nAccess denied\n</body>\n</html>\n"
    replay.fault(
        lambda request: httpx.Response(403, text=page, headers={"content-type": "text/html"}),
        when=lambda seen: seen.operation == "Resolve",
    )

    result = _sync()

    assert result.exit_code == 1, result.output
    lines = [_ANSI_ESCAPE.sub("", line) for line in result.output.splitlines()]
    (error,) = [line for line in lines if line.startswith("Error: ")]
    assert error.startswith("Error: Forbidden: ")
    assert "<!DOCTYPE html>\\n<html>\\n<body>" in error
    assert "<html>" not in lines


def test_an_output_that_cannot_take_the_report_keeps_the_exit_code(
    replay: Replay, monkeypatch: pytest.MonkeyPatch, migrated_database: Path
) -> None:
    """The run is recorded before the report prints; a report the output cannot encode (an
    emoji label on an ASCII stream) does not change the exit code."""
    first = _sync()
    assert first.exit_code == 0, first.output

    def renamed(body: dict[str, Any]) -> None:
        body["data"]["repository"]["labels"]["nodes"][0]["name"] = "bug \U0001f41b"

    replay.modify(renamed, when=lambda seen: seen.operation == "Resolve")
    ascii_out = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sync_cli, "console", Console(file=ascii_out, width=200))

    result = _sync()

    assert result.exit_code == 0, result.output
    assert "The report could not be printed: UnicodeEncodeError" in result.output
    assert _runs(migrated_database)[-1] == {"id": 2, "status": "complete", "exit_code": 0}
