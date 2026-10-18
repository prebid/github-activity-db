"""The tracked database file: rollback-journal mode (D20) and frozen content (D26).

scripts/check_db_journal.py and scripts/check_frozen_db.py also run as pre-commit hooks on
github_activity.db. Step 6 moves the database out of git and revisits these tests.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_db_journal.py"


def _check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, timeout=60
    )


def test_tracked_database_is_in_rollback_journal_mode() -> None:
    result = _check(REPO_ROOT / "github_activity.db")
    assert result.returncode == 0, result.stdout
    assert "rollback journal" in result.stdout


def test_wal_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (x INTEGER)")
        connection.commit()
    finally:
        connection.close()
    result = _check(path)
    assert result.returncode == 1
    assert "journal format 2/2 (2 is WAL)" in result.stdout


def test_non_database_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    path.write_text("not a database")
    result = _check(path)
    assert result.returncode == 1
    assert "not an SQLite database" in result.stdout


FROZEN = REPO_ROOT / "scripts" / "check_frozen_db.py"
GIT = shutil.which("git")


def _frozen(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FROZEN), *map(str, paths)], capture_output=True, text=True, timeout=60
    )


class TestFrozenContent:
    """scripts/check_frozen_db.py pins the tracked file's pre-Step-3 content (D26)."""

    def test_the_tracked_file_has_the_pinned_digest(self) -> None:
        result = _frozen(REPO_ROOT / "github_activity.db")
        assert result.returncode == 0, result.stdout
        assert "the pinned frozen content" in result.stdout

    def test_an_identical_copy_passes(self, tmp_path: Path) -> None:
        copy = tmp_path / "copy.db"
        shutil.copyfile(REPO_ROOT / "github_activity.db", copy)
        assert _frozen(copy).returncode == 0

    def test_a_changed_copy_fails(self, tmp_path: Path) -> None:
        copy = tmp_path / "changed.db"
        shutil.copyfile(REPO_ROOT / "github_activity.db", copy)
        connection = sqlite3.connect(copy)
        try:
            connection.execute("PRAGMA user_version = 1")
        finally:
            connection.close()
        result = _frozen(REPO_ROOT / "github_activity.db", copy)
        assert result.returncode == 1
        assert f"{copy}: sha256 " in result.stdout
        assert "differs from the pinned" in result.stdout

    def test_a_missing_file_fails(self, tmp_path: Path) -> None:
        result = _frozen(tmp_path / "absent.db")
        assert result.returncode == 1
        assert "cannot read" in result.stdout

    def test_a_symlink_to_an_identical_copy_fails(self, tmp_path: Path) -> None:
        """git would store the link text, not the database."""
        copy = tmp_path / "copy.db"
        shutil.copyfile(REPO_ROOT / "github_activity.db", copy)
        (tmp_path / "github_activity.db").symlink_to(copy)
        result = _frozen(tmp_path / "github_activity.db")
        assert result.returncode == 1
        assert "is a symlink" in result.stdout

    def test_git_stores_the_tracked_file_as_a_regular_file(self) -> None:
        """What a clone or a raw download gets: deleted, renamed, or committed as a symlink, the
        file is not this one entry."""
        assert GIT is not None
        result = subprocess.run(
            [GIT, "ls-files", "--stage", "--", "github_activity.db"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        (entry,) = result.stdout.splitlines()
        mode, _blob, stage_and_path = entry.split(" ")
        assert (mode, stage_and_path) == ("100644", "0\tgithub_activity.db")


class TestGitignore:
    """Only the repository-root github_activity.db escapes the *.db rule (D26)."""

    @staticmethod
    def _ignored(path: str) -> bool:
        assert GIT is not None
        result = subprocess.run(
            [GIT, "check-ignore", "--no-index", "--quiet", path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    @pytest.mark.parametrize(
        "path", ["github_activity.local.db", "data/github_activity.db", "scratch.db"]
    )
    def test_other_database_files_are_ignored(self, path: str) -> None:
        assert self._ignored(path)

    @pytest.mark.parametrize(
        "path", ["x.db-journal", "x.sqlite-journal", "x.sqlite3-wal", "x.sqlite-shm"]
    )
    def test_transient_files_are_ignored(self, path: str) -> None:
        assert self._ignored(path)

    def test_the_tracked_file_is_not_ignored(self) -> None:
        assert not self._ignored("github_activity.db")


def _git(repo: Path, *args: str) -> None:
    assert GIT is not None
    subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True, timeout=60)


class TestNoSidecarFiles:
    """A journal, WAL, or shared-memory file beside github_activity.db would change what SQLite
    reads from it on the next open, past the pinned digest. On a case-insensitive file system
    SQLite finds it under any letter case."""

    SIDECARS = ("github_activity.db-journal", "github_activity.db-wal", "github_activity.db-shm")

    def test_none_is_tracked(self) -> None:
        assert GIT is not None
        result = subprocess.run(
            [GIT, "ls-files", "--", *(f":(icase){name}" for name in self.SIDECARS)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        assert result.stdout == ""

    @staticmethod
    def _run_hook(tmp_path: Path, name: str, *, symlink: bool = False) -> int:
        """pre-commit itself, on a scratch repository holding ``name``, with the hook's lines
        copied from the real config (a local hook, so nothing is fetched)."""
        config = (REPO_ROOT / ".pre-commit-config.yaml").read_text()
        start = config.index("      - id: tracked-db-no-sidecar\n")
        hook = config[start : config.index("\n      - id: ", start)]
        hooks = tmp_path / "hooks.yaml"
        hooks.write_text(f"repos:\n  - repo: local\n    hooks:\n{hook}\n")
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        if symlink:
            (repo / "target").write_text("journal bytes")
            (repo / name).symlink_to("target")
        else:
            (repo / name).write_text("journal bytes")
        _git(repo, "add", "-f", "--", name)
        result = subprocess.run(
            [sys.executable, "-m", "pre_commit", "run", "--all-files", "--config", str(hooks)],
            cwd=repo,
            env={**os.environ, "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-home")},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "no journal, WAL, or shared-memory file" in result.stdout, result.stdout
        return result.returncode

    @pytest.mark.parametrize(
        ("name", "symlink"),
        [
            *((name, False) for name in SIDECARS),
            ("github_activity.db-journal", True),
            ("GITHUB_ACTIVITY.DB-JOURNAL", False),
            ("GitHub_Activity.db-wal", False),
        ],
    )
    def test_the_hook_refuses_each(self, tmp_path: Path, name: str, symlink: bool) -> None:
        assert self._run_hook(tmp_path, name, symlink=symlink) == 1

    @pytest.mark.parametrize("name", ["github_activity.db", "github_activity.local.db-journal"])
    def test_the_hook_passes_other_files(self, tmp_path: Path, name: str) -> None:
        assert self._run_hook(tmp_path, name) == 0
