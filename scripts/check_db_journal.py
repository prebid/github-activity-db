"""Fail unless each SQLite database file is in rollback-journal mode (redesign plan D20).

Header bytes 18-19 hold the file format write and read versions: 1 for the rollback
journal, 2 for WAL. A database tracked in git must not be in WAL mode, because committed
transactions can sit in the git-ignored *-wal file until a checkpoint.

Usage: python scripts/check_db_journal.py DATABASE...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SQLITE_MAGIC = b"SQLite format 3\x00"
ROLLBACK_JOURNAL = b"\x01\x01"


def problem(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(100)
    except OSError as e:
        return f"cannot read: {e}"
    if not header.startswith(SQLITE_MAGIC) or len(header) < 20:
        return "not an SQLite database"
    if header[18:20] != ROLLBACK_JOURNAL:
        return (
            f"journal format {header[18]}/{header[19]} (2 is WAL); switch back with "
            f"sqlite3 {path} 'PRAGMA journal_mode=DELETE'"
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path in args.databases:
        found = problem(path)
        print(f"{path}: {found or 'rollback journal'}")
        failed = failed or found is not None
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
