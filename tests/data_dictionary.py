"""Reading docs/data-dictionary.md and comparing it with a database (redesign plan 5.1)."""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from types import ModuleType

from tests.db.schema_support import user_objects

ROOT = Path(__file__).resolve().parents[1]
DICTIONARY = ROOT / "docs" / "data-dictionary.md"
SCHEMA_LOCK = ROOT / "tests" / "schema_versions.json"
CHECKER = ROOT / "scripts" / "check_query_lock.py"
USES = ("consumer", "internal")

_HEADING = re.compile(r"^### `(?P<name>\w+)`$")
_ROW = re.compile(r"^\| `(?P<column>\w+)` \| (?P<use>\w+) \| (?P<meaning>.*\S) \|$")
_TABLE_HEADER = ("| Column | Use | Meaning |", "|--------|-----|---------|")


class DictionaryError(ValueError):
    """The dictionary text has a shape this reader does not model."""


def read_dictionary(text: str) -> dict[str, dict[str, str]]:
    """``{object: {column: use}}`` from ``### `object` `` headings and their column rows.

    A row is ``| `column` | consumer or internal | meaning |``; any other line that starts with
    ``|`` after its indentation, except the table's header and separator rows, is an error
    rather than a skipped line, as is a non-blank line right after a table line that does not
    start with ``|`` (GFM reads it as another row), a row outside an object, a name listed
    twice, and an object without rows.
    """
    entries: dict[str, dict[str, str]] = {}
    current: str | None = None
    in_table = False
    for number, line in enumerate(text.splitlines(), start=1):
        if in_table and line.strip() and not line.lstrip().startswith("|"):
            raise DictionaryError(f"line {number}: continues a table without a leading |")
        in_table = line.lstrip().startswith("|")
        if heading := _HEADING.match(line):
            name = str(heading["name"])
            if name in entries:
                raise DictionaryError(f"line {number}: {name} is listed twice")
            entries[name] = {}
            current = name
        elif line.startswith("#"):
            current = None
        elif line.lstrip().startswith("|") and line not in _TABLE_HEADER:
            row = _ROW.match(line)
            if row is None:
                raise DictionaryError(f"line {number}: not a `| column | use | meaning |` row")
            if current is None:
                raise DictionaryError(f"line {number}: a column row outside an object")
            if row["use"] not in USES:
                raise DictionaryError(f"line {number}: use {row['use']!r} is not one of {USES}")
            if row["column"] in entries[current]:
                raise DictionaryError(f"line {number}: {current}.{row['column']} is listed twice")
            entries[current][row["column"]] = row["use"]
    if empty := sorted(name for name, columns in entries.items() if not columns):
        raise DictionaryError(f"objects without columns: {', '.join(empty)}")
    return entries


def database_columns(database: Path) -> dict[str, list[str]]:
    """Every table and view with its columns, SQLite's own ``sqlite_*`` tables excluded.

    ``table_xinfo``, unlike ``table_info``, also lists generated and hidden columns.
    """
    names = user_objects(database, "table") + user_objects(database, "view")
    with closing(sqlite3.connect(database)) as connection:
        return {
            name: [row[1] for row in connection.execute(f'PRAGMA table_xinfo("{name}")')]
            for name in names
        }


def differences(entries: dict[str, dict[str, str]], database: dict[str, list[str]]) -> list[str]:
    """Objects and columns at head without an entry, and entries naming none at head."""
    found = [f"no entry for {name}" for name in sorted(database.keys() - entries.keys())]
    found += [f"{name} is not at head" for name in sorted(entries.keys() - database.keys())]
    for name in sorted(database.keys() & entries.keys()):
        found += [f"no entry for {name}.{c}" for c in database[name] if c not in entries[name]]
        found += [f"{name}.{c} is not at head" for c in entries[name] if c not in database[name]]
    return found


def consumer_pairs(entries: dict[str, dict[str, str]]) -> list[list[str]]:
    """The (table or view, column) pairs marked consumer, sorted, as the lock lists them."""
    return sorted(
        [name, column]
        for name, columns in entries.items()
        for column, use in columns.items()
        if use == "consumer"
    )


# An entry as scripts/check_query_lock.py reads it: its consumer pairs and its view digests.
LockEntry = tuple[set[tuple[str, str]], dict[str, str]]


@functools.cache
def lock_checker() -> ModuleType:
    """scripts/check_query_lock.py, the one reader of the lock, loaded from its file: CI runs it
    standalone, so it is not a package module."""
    spec = importlib.util.spec_from_file_location("check_query_lock", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_lock(text: str) -> dict[str, LockEntry]:
    """Each version's entry, read by the checker's rule; raises its CannotCheck on a shape it
    refuses (a key listed twice included)."""
    lock: dict[str, LockEntry] = lock_checker().schema_entries(text, str(SCHEMA_LOCK))
    return lock


def read_lock() -> dict[str, LockEntry]:
    return parse_lock(SCHEMA_LOCK.read_text())


def lock_columns(entry: LockEntry) -> list[list[str]]:
    """An entry's consumer pairs, sorted, as the dictionary's consumer pairs are."""
    return sorted([table, column] for table, column in entry[0])


def lock_views(entry: LockEntry) -> dict[str, str]:
    """An entry's view digests; version 1 has none."""
    return entry[1]


def view_digest(sql: str) -> str:
    """SHA-256 of a view's frozen SQL, whitespace runs collapsed as the view comparison does."""
    return hashlib.sha256(" ".join(sql.split()).encode()).hexdigest()


def lock_differences(entry: list[list[str]], consumer: list[list[str]]) -> list[str]:
    """Pairs the lock entry lists that are not consumer at head, and consumer pairs it omits."""
    listed = {tuple(pair) for pair in entry}
    marked = {tuple(pair) for pair in consumer}
    found = [
        f"{'.'.join(pair)} is not a consumer column at head" for pair in sorted(listed - marked)
    ]
    found += [f"{'.'.join(pair)} is missing from the entry" for pair in sorted(marked - listed)]
    return found
