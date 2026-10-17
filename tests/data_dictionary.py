"""Reading docs/data-dictionary.md and comparing it with a database (redesign plan 5.1)."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from tests.db.schema_support import user_objects

ROOT = Path(__file__).resolve().parents[1]
DICTIONARY = ROOT / "docs" / "data-dictionary.md"
SCHEMA_LOCK = ROOT / "tests" / "schema_versions.json"
USES = ("consumer", "internal")

_HEADING = re.compile(r"^### `(?P<name>\w+)`$")
_ROW = re.compile(r"^\| `(?P<column>\w+)` \| (?P<use>\w+) \| (?P<meaning>.*\S) \|$")


class DictionaryError(ValueError):
    """The dictionary text has a shape this reader does not model."""


def read_dictionary(text: str) -> dict[str, dict[str, str]]:
    """``{object: {column: use}}`` from ``### `object` `` headings and their column rows.

    A row is ``| `column` | consumer or internal | meaning |``; any other line starting with
    ``| ` `` is an error rather than a skipped line, as is a row outside an object, a name listed
    twice, and an object without rows.
    """
    entries: dict[str, dict[str, str]] = {}
    current: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if heading := _HEADING.match(line):
            name = str(heading["name"])
            if name in entries:
                raise DictionaryError(f"line {number}: {name} is listed twice")
            entries[name] = {}
            current = name
        elif line.startswith("#"):
            current = None
        elif line.startswith("| `"):
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


def read_lock() -> dict[str, list[list[str]]]:
    lock: dict[str, list[list[str]]] = json.loads(SCHEMA_LOCK.read_text())
    return lock


def lock_differences(entry: list[list[str]], consumer: list[list[str]]) -> list[str]:
    """Pairs the lock entry lists that are not consumer at head, and consumer pairs it omits."""
    listed = {tuple(pair) for pair in entry}
    marked = {tuple(pair) for pair in consumer}
    found = [
        f"{'.'.join(pair)} is not a consumer column at head" for pair in sorted(listed - marked)
    ]
    found += [f"{'.'.join(pair)} is missing from the entry" for pair in sorted(marked - listed)]
    return found
