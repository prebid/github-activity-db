"""The append-only check on tests/query_versions.json."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_query_lock.py"
GIT = shutil.which("git")
BASH = shutil.which("bash")
LOCK = "tests/query_versions.json"


def git(repo: Path, *args: str) -> None:
    assert GIT is not None
    subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "start")
    return tmp_path


def write_lock(repo: Path, entries: dict[str, str]) -> None:
    path = repo / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


def commit(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "lock")


def check(repo: Path, base: str = "main") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base], cwd=repo, capture_output=True, text=True
    )


def test_a_new_entry_passes(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "a" * 64, "pull_request:2": "b" * 64})
    result = check(repo)
    assert result.returncode == 0, result.stderr


def test_a_changed_entry_fails(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = check(repo)
    assert result.returncode == 1
    assert "pull_request:1 changed" in result.stderr


def test_a_removed_entry_fails(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64, "pull_request:2": "b" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:2": "b" * 64})
    result = check(repo)
    assert result.returncode == 1
    assert "pull_request:1 was removed" in result.stderr


def test_a_lock_absent_at_the_base_passes(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    result = check(repo)
    assert result.returncode == 0
    assert "does not exist at main" in result.stdout


def test_an_unknown_base_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    result = check(repo, base="origin/no-such-branch")
    assert result.returncode == 2


def test_a_deleted_lock_removes_every_entry(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).unlink()
    result = check(repo)
    assert result.returncode == 1
    assert "pull_request:1 was removed" in result.stderr


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"pull_request:1": 1}'])
def test_a_malformed_lock_is_an_error_not_a_pass(repo: Path, text: str) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).write_text(text)
    result = check(repo)
    assert result.returncode == 2, result.stderr


def test_an_absolute_lock_path_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main", "--lock", str(repo / LOCK)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "relative" in result.stderr


def test_the_checker_runs_isolated_from_site_packages(repo: Path) -> None:
    # CI runs the base copy with -I -S, outside the pull request's environment.
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "--base", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "pull_request:1 changed" in result.stderr


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe not utf-8", b"[" * 100_000 + b"]" * 100_000],
    ids=["not-utf-8", "nested-too-deeply"],
)
def test_an_unreadable_lock_is_an_error_not_a_pass(repo: Path, content: bytes) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).write_bytes(content)
    result = check(repo)
    assert result.returncode == 2, result.stderr


def test_an_integer_past_pythons_digit_limit_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).write_text('{"pull_request:1": ' + "1" * 5000 + "}")
    result = check(repo)
    assert result.returncode == 2, result.stderr


def test_the_verdict_does_not_depend_on_the_working_directory(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main"],
        cwd=repo / "tests",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pull_request:1 changed" in result.stderr


def test_a_malformed_lock_at_the_base_is_an_error_not_a_pass(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / LOCK).write_text("{not json")
    commit(repo)
    write_lock(repo, {"pull_request:1": "a" * 64})
    result = check(repo)
    assert result.returncode == 2
    assert "at main is not JSON" in result.stderr


def test_a_lock_path_outside_the_repository_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main", "--lock", "../query_versions.json"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "relative" in result.stderr


def test_without_git_the_check_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={"PATH": str(repo / "no-such-bin")},
    )
    assert result.returncode == 2
    assert "git is not on PATH" in result.stderr


# --------------------------------------------------------------------------------------------
# The CI step itself, run against shallow merge checkouts like the one actions/checkout makes
# --------------------------------------------------------------------------------------------
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "check.yml"
STEP = "Hydration-version lock entries are append-only"


def lock_step_script() -> str:
    """The step's ``run:`` script from check.yml, with this interpreter for the project's."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {STEP}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    script = "\n".join(body)
    assert script.count("uv run --no-sync python") == 1, script
    return script.replace("uv run --no-sync python", sys.executable)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    return bare


def work_clone(tmp_path: Path, origin: Path, name: str) -> Path:
    work = tmp_path / name
    git(tmp_path, "clone", "-q", f"file://{origin}", str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "test")
    return work


def push_main(work: Path, *, checker: bool, entries: dict[str, str] | None) -> None:
    if checker:
        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "check_query_lock.py").write_text(SCRIPT.read_text())
    if entries is not None:
        write_lock(work, entries)
    (work / "README").write_text("base\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main")


def run_step_on_merge(
    tmp_path: Path, origin: Path, work: Path, change
) -> subprocess.CompletedProcess[str]:
    """Commit ``change`` on a PR branch, push its merge into main as a ref, and run the step
    in a depth-1 checkout of that merge, as a pull_request run does."""
    git(work, "checkout", "-q", "-b", "pr")
    change(work)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "pr")
    git(work, "checkout", "-q", "main")
    git(work, "merge", "-q", "--no-ff", "pr", "-m", "merge")
    git(work, "push", "-q", "origin", "HEAD:refs/pull/1/merge")
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "-q", str(checkout))
    git(checkout, "remote", "add", "origin", f"file://{origin}")
    git(checkout, "fetch", "-q", "--depth=1", "origin", "refs/pull/1/merge")
    git(checkout, "checkout", "-q", "FETCH_HEAD")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    return run_step(checkout, runner_temp)


def run_step(checkout: Path, runner_temp: Path) -> subprocess.CompletedProcess[str]:
    """The step under ``bash -e``, as GitHub Actions runs a ``run:`` script."""
    assert BASH is not None
    return subprocess.run(
        [BASH, "-e", "-c", lock_step_script()],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
    )


def edit_entry(work: Path) -> None:
    write_lock(work, {"pull_request:1": "c" * 64})


def edit_entry_and_relax_checker(work: Path) -> None:
    edit_entry(work)
    checker = work / "scripts" / "check_query_lock.py"
    checker.write_text("raise SystemExit(0)\n")


def test_the_step_fails_an_edited_entry(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})
    result = run_step_on_merge(tmp_path, origin, work, edit_entry)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pull_request:1 changed" in result.stderr


def test_the_step_runs_the_base_checker_not_the_pull_requests(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})
    result = run_step_on_merge(tmp_path, origin, work, edit_entry_and_relax_checker)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "runner-temp/check_query_lock.py" in result.stdout


def test_the_step_passes_a_new_entry(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})

    def add_entry(target: Path) -> None:
        write_lock(target, {"pull_request:1": "a" * 64, "pull_request:2": "b" * 64})

    result = run_step_on_merge(tmp_path, origin, work, add_entry)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_step_uses_the_pull_requests_checker_where_the_base_has_none(
    tmp_path: Path, origin: Path
) -> None:
    # The first pull request with the lock: the base has neither the checker nor the lock.
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=False, entries=None)

    def add_lock(target: Path) -> None:
        (target / "scripts").mkdir(exist_ok=True)
        (target / "scripts" / "check_query_lock.py").write_text(SCRIPT.read_text())
        write_lock(target, {"pull_request:1": "a" * 64})

    result = run_step_on_merge(tmp_path, origin, work, add_lock)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "running scripts/check_query_lock.py" in result.stdout
    assert "does not exist at" in result.stdout


def test_the_step_fails_without_a_merge_commit(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))
    result = run_step(checkout, tmp_path)
    assert result.returncode != 0
