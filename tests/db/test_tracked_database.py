"""The tracked database file: rollback-journal mode (D20) and frozen content (D26).

scripts/check_db_journal.py and scripts/check_frozen_db.py also run as pre-commit hooks on
github_activity.db. Step 6 moves the database out of git and revisits these tests.
"""

import re
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


class TestNoSidecarFiles:
    """A journal, WAL, or shared-memory file beside github_activity.db would change what SQLite
    reads from it on the next open, past the pinned digest."""

    SIDECARS = ("github_activity.db-journal", "github_activity.db-wal", "github_activity.db-shm")

    def test_none_is_tracked(self) -> None:
        assert GIT is not None
        result = subprocess.run(
            [GIT, "ls-files", "--", *self.SIDECARS],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        assert result.stdout == ""

    def test_the_hook_refuses_each(self) -> None:
        config = (REPO_ROOT / ".pre-commit-config.yaml").read_text()
        (pattern,) = re.findall(
            r"- id: tracked-db-no-sidecar\n(?:\s+\w+: .*\n)*?\s+files: (\S+)\n", config
        )
        assert "language: fail" in config.split("- id: tracked-db-no-sidecar")[1].split("- id:")[0]
        for name in self.SIDECARS:
            assert re.search(pattern, name), name
        for name in ("github_activity.db", "github_activity.local.db-journal"):
            assert not re.search(pattern, name), name
