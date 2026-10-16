"""The tracked database file stays in rollback-journal mode (redesign plan D20).

In WAL mode committed transactions can sit in a git-ignored *-wal file, so a committed
github_activity.db in WAL mode can be missing data. Step 6 moves the database out of git
and revisits this test.
"""

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLLBACK_JOURNAL = b"\x01\x01"
WAL = b"\x02\x02"


def journal_format(path: Path) -> bytes:
    """Header bytes 18-19: the file format write and read versions (1 rollback, 2 WAL)."""
    with path.open("rb") as handle:
        return handle.read(20)[18:20]


def test_tracked_database_is_in_rollback_journal_mode() -> None:
    assert journal_format(REPO_ROOT / "github_activity.db") == ROLLBACK_JOURNAL


def test_journal_format_detects_wal(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (x INTEGER)")
        connection.commit()
    finally:
        connection.close()
    assert journal_format(path) == WAL
