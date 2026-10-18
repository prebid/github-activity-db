"""Column types shared by the models (redesign plan 5.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """The Python-side default of every timestamp column."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A UTC timestamp: aware datetimes in, aware datetimes out.

    SQLAlchemy's SQLite ``DateTime`` stores a value's wall-clock fields and drops its offset, so
    an aware value is converted to UTC before it is written, and a naive one is rejected rather
    than guessed at. The stored text keeps SQLAlchemy's fixed-width format
    (``YYYY-MM-DD HH:MM:SS.ffffff``), so text order is time order; ``CURRENT_TIMESTAMP``'s
    19-character text would not order with it, which is why defaults are ``utc_now``.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected a datetime, got {type(value).__name__}: {value!r}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"naive datetime {value.isoformat()}; pass a timezone-aware value")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        # SQLite's DateTime result processor returns a naive datetime or None.
        return None if value is None else value.replace(tzinfo=UTC)
