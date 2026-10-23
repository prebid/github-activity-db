"""UtcDateTime: the timestamp type of every model column (redesign plan 5.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest
from sqlalchemy import DateTime, insert, select, update
from sqlalchemy.exc import StatementError

from github_activity_db.db import types
from github_activity_db.db.models import Base, GitHubUser, UserTag
from github_activity_db.db.types import UtcDateTime, utc_now
from tests.db.alembic_support import query
from tests.db.seed import Seed, connect

KOLKATA = timezone(timedelta(hours=5, minutes=30))


class NoOffset(tzinfo):
    """A tzinfo whose utcoffset is None: Python treats such a datetime as naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


def _write_refreshed_at(database: Path, value: object) -> None:
    with connect(database) as connection:
        Seed(connection).user("someone", refreshed_at=value)


def _stored_refreshed_at(database: Path) -> str:
    ((text,),) = query(database, "SELECT refreshed_at FROM github_users")
    return str(text)


def test_no_column_uses_a_bare_datetime() -> None:
    types = {
        f"{table.name}.{column.name}": column.type
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    assert [name for name, type_ in types.items() if isinstance(type_, DateTime)] == []
    assert "pull_requests.merged_at" in [
        name for name, type_ in types.items() if isinstance(type_, UtcDateTime)
    ]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(datetime(2026, 1, 1, 12, 0), id="naive"),
        pytest.param(datetime(2026, 1, 1, 12, 0, tzinfo=NoOffset()), id="tzinfo without offset"),
    ],
)
def test_a_naive_value_is_rejected(migrated_database: Path, value: datetime) -> None:
    with pytest.raises(StatementError) as error:
        _write_refreshed_at(migrated_database, value)
    assert type(error.value.orig) is ValueError
    assert query(migrated_database, "SELECT COUNT(*) FROM github_users") == [(0,)]


def test_a_value_that_is_not_a_datetime_is_rejected(migrated_database: Path) -> None:
    with pytest.raises(StatementError) as error:
        _write_refreshed_at(migrated_database, "2026-01-01 12:00:00")
    assert type(error.value.orig) is TypeError
    assert query(migrated_database, "SELECT COUNT(*) FROM github_users") == [(0,)]


def test_an_aware_value_is_stored_as_utc_and_read_back_aware(migrated_database: Path) -> None:
    written = datetime(2026, 1, 1, 5, 30, 0, 123, tzinfo=KOLKATA)
    _write_refreshed_at(migrated_database, written)
    # SQLAlchemy's fixed-width text, holding the UTC wall clock.
    assert _stored_refreshed_at(migrated_database) == "2026-01-01 00:00:00.000123"
    with connect(migrated_database) as connection:
        read = connection.execute(select(GitHubUser.refreshed_at)).scalar_one()
    assert read == written
    assert read.tzinfo is UTC
    assert read.isoformat() == "2026-01-01T00:00:00.000123+00:00"


def test_stored_text_orders_as_time(migrated_database: Path) -> None:
    """Fixed width, microseconds included, so a text comparison is a time comparison."""
    earlier = datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)
    later = datetime(2026, 1, 2, 4, 0, 0, tzinfo=timezone(timedelta(hours=5)))  # 23:00 UTC Jan 1
    _write_refreshed_at(migrated_database, later)
    later_text = _stored_refreshed_at(migrated_database)
    with connect(migrated_database) as connection:
        connection.execute(update(GitHubUser).values(refreshed_at=earlier))
    earlier_text = _stored_refreshed_at(migrated_database)
    assert (later_text, earlier_text) == (
        "2026-01-01 23:00:00.000000",
        "2026-01-01 23:59:59.000000",
    )
    assert later_text < earlier_text
    assert len(later_text) == len(earlier_text) == 26


def test_null_passes_through(migrated_database: Path) -> None:
    with connect(migrated_database) as connection:
        Seed(connection).user("someone", not_found_at=None)
        assert connection.execute(select(GitHubUser.not_found_at)).scalar_one() is None


# A fixed instant for the clock the column default reads.
NOW = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)


def test_the_default_is_now_in_utc(
    migrated_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column default over a fixed wall clock, which answers NOW in the zone asked for; the
    clock alone is replaced, so an explicit timestamp written beside the default is still a
    datetime to UtcDateTime."""
    asked: list[tzinfo] = []

    def wall_clock(tz: tzinfo) -> datetime:
        asked.append(tz)
        return NOW.astimezone(tz)

    monkeypatch.setattr(types, "_wall_clock", wall_clock)
    assert utc_now() == NOW
    assert asked == [UTC]
    explicit = datetime(2026, 1, 1, 5, 30, tzinfo=KOLKATA)
    with connect(migrated_database) as connection:
        connection.execute(insert(UserTag).values(name="kept"))
        connection.execute(insert(UserTag).values(name="given", created_at=explicit))
        created = connection.execute(
            select(UserTag.name, UserTag.created_at).order_by(UserTag.id)
        ).all()
    assert [tuple(row) for row in created] == [("kept", NOW), ("given", explicit)]
    assert [when.tzinfo for _, when in created] == [UTC, UTC]
    assert asked == [UTC, UTC]
    assert query(migrated_database, "SELECT created_at FROM user_tags ORDER BY id") == [
        ("2026-01-02 03:04:05.678901",),
        ("2026-01-01 00:00:00.000000",),
    ]
