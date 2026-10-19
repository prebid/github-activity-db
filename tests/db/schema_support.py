"""Structural readings of a SQLite schema, for comparing a migrated database with the models.

Index, trigger, and view SQL is compared exactly, apart from leading and trailing whitespace; a
table's definition by the items of its body, sorted, each exact apart from its ends (column order
and table options are read by pragmas). Whitespace inside a quoted literal is data, so collapsing
it could hide a changed CHECK, index, trigger, or view.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


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
    return [item.strip() for item in items]


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


def user_objects(database: Path, kind: str) -> list[str]:
    """Tables or views, by name, except SQLite's own ``sqlite_`` objects (``_`` escaped, since
    LIKE reads it as any character)."""
    return [
        str(name)
        for (name,) in _rows(
            database,
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite\\_%' "
            "ESCAPE '\\' ORDER BY name",
            kind,
        )
    ]


def schema_tables(database: Path) -> list[str]:
    """Tables a migration or ``create_all`` creates: not SQLite's own, not alembic's."""
    return [name for name in user_objects(database, "table") if name != "alembic_version"]


def views(database: Path) -> dict[str, str]:
    return {
        str(name): str(sql)
        for name, sql in _rows(database, "SELECT name, sql FROM sqlite_master WHERE type = 'view'")
    }


def schema_of(database: Path) -> dict[str, dict[str, Any]]:
    """Per table: columns in order, indexes, foreign keys, every item of its definition, its
    STRICT and WITHOUT ROWID flags, and its triggers.

    Indexes behind UNIQUE and PRIMARY KEY constraints are compared by structure, since SQLite
    numbers them (``sqlite_autoindex_<table>_N``) in definition order; a created index also by
    its SQL, which holds an expression or a partial index's WHERE. Foreign keys are compared by
    their columns and actions, since ``PRAGMA foreign_key_list`` numbers them too. Constraint
    names, which no pragma reports, and CHECK text come from the definition items.
    """
    schema: dict[str, dict[str, Any]] = {}
    for table in schema_tables(database):
        indexes = []
        for _seq, name, unique, origin, partial in _rows(database, f'PRAGMA index_list("{table}")'):
            keys = tuple(_rows(database, f'PRAGMA index_xinfo("{name}")'))
            created = None
            if origin == "c":
                ((created,),) = _rows(
                    database, "SELECT sql FROM sqlite_master WHERE name = ?", name
                )
                created = (name, str(created).strip())
            indexes.append((created, unique, origin, partial, keys))
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
            # (type, ncol, without rowid, strict)
            "flags": [row[2:] for row in _rows(database, f'PRAGMA table_list("{table}")')],
            "triggers": sorted(
                (str(name), str(trigger).strip())
                for name, trigger in _rows(
                    database,
                    "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                    table,
                )
            ),
        }
    return schema


def nullable_columns(database: Path) -> set[tuple[str, str]]:
    """(table, column) for every column declared without NOT NULL, primary-key columns included:
    SQLite holds only an INTEGER PRIMARY KEY, or a WITHOUT ROWID table's key, NOT NULL itself."""
    return {
        (table, str(row[1]))
        for table in schema_tables(database)
        for row in _rows(database, f'PRAGMA table_xinfo("{table}")')
        if not row[3]
    }


def unique_sets(database: Path) -> dict[str, set[tuple[str, tuple[str, ...]]]]:
    """Per table, each unique index's origin (``u`` a UNIQUE, ``pk`` a composite or non-integer
    primary key) and its columns; an INTEGER PRIMARY KEY is the rowid, with no index."""
    return {
        table: {
            (
                str(origin),
                tuple(str(row[2]) for row in _rows(database, f'PRAGMA index_info("{name}")')),
            )
            for _seq, name, unique, origin, _partial in _rows(
                database, f'PRAGMA index_list("{table}")'
            )
            if unique
        }
        for table in schema_tables(database)
    }


def delete_actions(database: Path) -> set[tuple[str, str, str, str]]:
    """(table, column, referenced table, ON DELETE action) for every foreign-key column, each of
    a column's foreign keys counted."""
    return {
        (table, str(row[3]), str(row[2]), str(row[6]))
        for table in schema_tables(database)
        for row in _rows(database, f'PRAGMA foreign_key_list("{table}")')
    }


# alembic's own: the version table and the index behind its primary key.
ALEMBIC_OBJECTS = frozenset(
    {("table", "alembic_version"), ("index", "sqlite_autoindex_alembic_version_1")}
)


def objects_outside_the_comparison(database: Path, frozen: Iterable[str]) -> list[tuple[str, str]]:
    """Every ``sqlite_master`` row that neither ``schema_of`` nor ``view_differences`` reads:
    not a schema table, an index or trigger on one, a frozen view, or alembic's own. SQLite's
    ``sqlite_`` tables are not exempt, so an ``ANALYZE`` statistics table counts."""
    tables, views_frozen = set(schema_tables(database)), set(frozen)
    return [
        (str(kind), str(name))
        for kind, name, table in _rows(
            database, "SELECT type, name, tbl_name FROM sqlite_master ORDER BY type, name"
        )
        if (kind, name) not in ALEMBIC_OBJECTS
        and not (kind == "table" and name in tables)
        and not (kind in ("index", "trigger") and table in tables)
        and not (kind == "view" and name in views_frozen)
    ]


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
    """Each view at head with the SQL of the newest revision that creates or replaces it (its
    ``VIEWS``); a view a later revision lists in ``DROPPED_VIEWS`` is not at head."""
    script = ScriptDirectory.from_config(config)
    frozen: dict[str, str] = {}
    for revision in reversed(list(script.walk_revisions())):
        for name, sql in getattr(revision.module, "VIEWS", ()):
            frozen[name] = sql
        for name in getattr(revision.module, "DROPPED_VIEWS", ()):
            del frozen[name]
    return frozen


def view_differences(database: Path, frozen: dict[str, str]) -> list[str]:
    """Views missing, extra, or whose SQL differs from ``frozen``. SQLite stores a view's SQL as
    written, without its leading and trailing whitespace; nothing else is normalized, since
    whitespace inside a literal changes a view's rows."""
    present = views(database)
    differences = [f"missing {name}" for name in sorted(frozen.keys() - present.keys())]
    differences += [f"not frozen {name}" for name in sorted(present.keys() - frozen.keys())]
    differences += [
        f"differs {name}"
        for name in sorted(frozen.keys() & present.keys())
        if frozen[name].strip() != present[name]
    ]
    return differences
