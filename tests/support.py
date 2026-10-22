"""Helpers that test modules in several packages share: files listed with os.walk, the SQLite
error under SQLAlchemy's, a git process's environment, and a listener on every engine or pool."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import Pool


def files_under(root: Path, suffixes: Collection[str] | None) -> list[Path]:
    """The files under the directory ``root`` whose suffix is one of ``suffixes``, or every file
    when it is None, sorted. Listed with os.walk rather than a glob, so a scan that reads the
    files a glob finds cannot pass on fewer of them."""
    return sorted(
        Path(directory, name)
        for directory, _, names in os.walk(root)
        for name in names
        if suffixes is None or Path(name).suffix in suffixes
    )


def sqlite_error_name(error: BaseException | None) -> str:
    """The SQLite result code of the driver error that ``error``, SQLAlchemy's error, wraps."""
    assert isinstance(error, DBAPIError), repr(error)
    driver_error = error.orig
    assert isinstance(driver_error, sqlite3.Error), repr(driver_error)
    return driver_error.sqlite_errorname


def git_environment(home: Path, **extra: str) -> dict[str, str]:
    """A git process's whole environment: this process's ``PATH``; ``home`` as its home, so no
    user-level configuration or ignore file applies; no global or system configuration; and the
    ``extra`` variables the caller names."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **extra,
    }


@contextmanager
def listening(
    name: str, listener: Callable[..., None], target: type[Engine | Pool] = Engine
) -> Iterator[None]:
    """``listener`` on the ``name`` event of every engine, or every pool, env.py's own included."""
    event.listen(target, name, listener)
    try:
        yield
    finally:
        event.remove(target, name, listener)
