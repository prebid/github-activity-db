"""The tracked database file stays in rollback-journal mode (redesign plan D20).

scripts/check_db_journal.py also runs as a pre-commit hook on github_activity.db. Step 6
moves the database out of git and revisits this test.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_db_journal.py"


def _check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)


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
