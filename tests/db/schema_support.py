"""Structural readings of a SQLite schema, for comparing a migrated database with the models."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


def normalized(sql: str) -> str:
    return " ".join(sql.split())


def create_table_items(sql: str) -> list[str]:
    """The comma-separated items of a CREATE TABLE body (columns and table constraints)."""
    items: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in sql[sql.index("(") :]:
        if quote is not None:
            current.append(char)
            if char == quote:  # a doubled quote closes and reopens, so it stays inside
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
            if depth == 1:
                continue
        elif char == ")":
            depth -= 1
            if depth == 0:
                items.append("".join(current))
                break
        elif char == "," and depth == 1:
            items.append("".join(current))
            current = []
            continue
        current.append(char)
    return [normalized(item) for item in items]


def named_constraints(database: Path, table: str) -> dict[str, str]:
    """``CONSTRAINT <name> ...`` items of the table's definition, by name."""
    (sql,) = _rows(
        database, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", table
    )[0]
    return {
        item.split()[1].strip('"'): item
        for item in create_table_items(str(sql))
        if item.upper().startswith("CONSTRAINT ")
    }


def _rows(database: Path, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return [tuple(row) for row in connection.execute(sql, parameters).fetchall()]


def schema_tables(database: Path) -> list[str]:
    """Tables a migration or ``create_all`` creates: not SQLite's own, not alembic's."""
    return [
        str(name)
        for (name,) in _rows(
            database,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND name <> 'alembic_version' ORDER BY name",
        )
    ]


def views(database: Path) -> dict[str, str]:
    return {
        str(name): str(sql)
        for name, sql in _rows(database, "SELECT name, sql FROM sqlite_master WHERE type = 'view'")
    }


def schema_of(database: Path) -> dict[str, dict[str, Any]]:
    """Per table: columns in order, indexes, foreign keys, and every item of its definition.

    Indexes behind UNIQUE and PRIMARY KEY constraints are compared by structure, since SQLite
    numbers them (``sqlite_autoindex_<table>_N``) in definition order; foreign keys by their
    columns and actions, since ``PRAGMA foreign_key_list`` numbers them too. Constraint names,
    which no pragma reports, and CHECK text come from the definition items.
    """
    schema: dict[str, dict[str, Any]] = {}
    for table in schema_tables(database):
        indexes = []
        for _seq, name, unique, origin, partial in _rows(database, f'PRAGMA index_list("{table}")'):
            keys = tuple(_rows(database, f'PRAGMA index_xinfo("{name}")'))
            indexes.append((name if origin == "c" else None, unique, origin, partial, keys))
        foreign_keys: dict[int, list[tuple[Any, ...]]] = {}
        for fk_id, *rest in _rows(database, f'PRAGMA foreign_key_list("{table}")'):
            foreign_keys.setdefault(fk_id, []).append(tuple(rest))
        (sql,) = _rows(database, "SELECT sql FROM sqlite_master WHERE name = ?", table)[0]
        schema[table] = {
            "columns": _rows(database, f'PRAGMA table_xinfo("{table}")'),
            "indexes": sorted(indexes, key=repr),
            "foreign_keys": sorted(
                (tuple(sorted(rows)) for rows in foreign_keys.values()), key=repr
            ),
            "definition": sorted(create_table_items(str(sql))),
        }
    return schema


def unindexed_foreign_keys(database: Path) -> set[tuple[str, tuple[str, ...]]]:
    """Foreign keys whose columns lead no index; a table's INTEGER PRIMARY KEY is its rowid."""
    missing: set[tuple[str, tuple[str, ...]]] = set()
    for table in schema_tables(database):
        leading = [
            tuple(row[2] for row in _rows(database, f'PRAGMA index_xinfo("{name}")') if row[5])
            for _seq, name, *_rest in _rows(database, f'PRAGMA index_list("{table}")')
        ]
        primary_key = sorted(
            (row[5], row[1], str(row[2]).upper())
            for row in _rows(database, f'PRAGMA table_xinfo("{table}")')
            if row[5]
        )
        if len(primary_key) == 1 and primary_key[0][2] == "INTEGER":
            leading.append((primary_key[0][1],))
        foreign_keys: dict[int, list[tuple[int, str]]] = {}
        for fk_id, seq, _parent, column, *_rest in _rows(
            database, f'PRAGMA foreign_key_list("{table}")'
        ):
            foreign_keys.setdefault(fk_id, []).append((seq, column))
        for parts in foreign_keys.values():
            columns = tuple(column for _seq, column in sorted(parts))
            if not any(keys[: len(columns)] == columns for keys in leading):
                missing.add((table, columns))
    return missing


def frozen_views(config: Config) -> dict[str, str]:
    """Each view's SQL from the newest revision that creates or replaces it (its ``VIEWS``)."""
    script = ScriptDirectory.from_config(config)
    frozen: dict[str, str] = {}
    for revision in reversed(list(script.walk_revisions())):
        for name, sql in getattr(revision.module, "VIEWS", ()):
            frozen[name] = sql
    return frozen


def view_differences(database: Path, frozen: dict[str, str]) -> list[str]:
    """Views missing, extra, or whose SQL differs from ``frozen``, whitespace aside."""
    present = views(database)
    differences = [f"missing {name}" for name in sorted(frozen.keys() - present.keys())]
    differences += [f"not frozen {name}" for name in sorted(present.keys() - frozen.keys())]
    differences += [
        f"differs {name}"
        for name in sorted(frozen.keys() & present.keys())
        if normalized(frozen[name]) != normalized(present[name])
    ]
    return differences
