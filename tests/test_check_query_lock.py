"""The append-only check on tests/query_versions.json."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_query_lock.py"
GIT = shutil.which("git")
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
