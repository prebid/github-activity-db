"""What every test runs under (redesign plan D40), installed by the conftest from collection on.

Inside the test process, a connection to anything but a Unix socket and every host-name lookup the
``socket`` module offers are refused, called with positional or keyword arguments; so are the
package's two real clocks: ``source/clock.py``'s ``datetime.now`` and ``asyncio.sleep`` (behind
``SystemClock.now`` and ``sleep``), replaced at those names so a test can give ``SystemClock`` a
fixed clock of its own, and ``db.types._wall_clock`` (behind ``utc_now``, the default of the
timestamp columns); and so is every draw from the unseeded random source ``SystemClock._random``,
which an instance may replace with a seeded one. ``no_proxy`` is ``*``, so an HTTP client that reads
proxies from the environment, or on macOS from the system settings, connects straight to the host
it was given and is refused there, whatever the machine's proxy settings. A refusal raises
``NotDeterministic``, a ``BaseException`` so that no ``except Exception`` in the code under test can
turn it into a handled failure, and is recorded in ``REFUSED``, which the conftest checks after
every test and once more when it undoes these patches. Out of scope: processes a test starts
(they run without these patches), the socket's other ways to reach a host (``sendto``,
``sendmsg``, ``bind``, ``listen``, and sockets made in C), and the standard library's clocks and
random modules called directly (``tests/test_suite_rules.py`` reads test files, the package, and
``alembic/`` for those).
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, tzinfo
from typing import Any, NoReturn

import pytest

from github_activity_db.db import types
from github_activity_db.source import clock
from github_activity_db.source.clock import SystemClock

# The host-name lookups the socket module offers; getfqdn goes through gethostbyaddr.
LOOKUPS = ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo")


class NotDeterministic(BaseException):
    """A test reached the network, the wall clock, or unseeded randomness."""

    def __init__(self, what: str, detail: str) -> None:
        super().__init__(f"{what}: {detail}")
        self.what = what
        self.detail = detail


REFUSED: list[NotDeterministic] = []


def refuse(what: str, detail: str) -> NoReturn:
    refusal = NotDeterministic(what, detail)
    REFUSED.append(refusal)
    raise refusal


def drain() -> list[tuple[str, str]]:
    """Each refusal recorded since the last drain, as (what, detail), and an empty record."""
    refused = [(refusal.what, refusal.detail) for refusal in REFUSED]
    REFUSED.clear()
    return refused


class _NoWallClock(datetime):
    """``datetime`` as ``source/clock.py`` sees it: a datetime, except that reading the time is
    refused."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> NoReturn:
        refuse("wall clock", f"source.clock: datetime.now({tz})")

    @classmethod
    def utcnow(cls) -> NoReturn:
        refuse("wall clock", "source.clock: datetime.utcnow()")

    @classmethod
    def today(cls) -> NoReturn:
        refuse("wall clock", "source.clock: datetime.today()")


class _NoSleep:
    """``asyncio`` as ``source/clock.py`` sees it: asyncio, except that sleeping is refused."""

    @staticmethod
    async def sleep(delay: float, result: Any = None) -> NoReturn:
        refuse("wall clock", f"source.clock: asyncio.sleep({delay})")

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


class _NoSystemRandom:
    """The random source as ``SystemClock`` sees it: a call of any of its methods is refused. A
    dunder name is not answered, so ``copy`` and ``pickle`` find what an object has."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def draw(*args: Any, **kwargs: Any) -> NoReturn:
            given = [repr(arg) for arg in args] + [f"{key}={kwargs[key]!r}" for key in kwargs]
            refuse("randomness", f"SystemClock._random.{name}({', '.join(given)})")

        return draw


def install(patch: pytest.MonkeyPatch) -> None:
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def connecting(self: socket.socket, address: Any) -> None:
        if self.family != socket.AF_UNIX:
            refuse("network", f"connect {address!r}")
        connect(self, address)

    def connecting_ex(self: socket.socket, address: Any) -> int:
        if self.family != socket.AF_UNIX:
            refuse("network", f"connect_ex {address!r}")
        return connect_ex(self, address)

    def looking_up(name: str) -> Any:
        def lookup(*args: Any, **kwargs: Any) -> NoReturn:
            refuse("network", f"{name} {(args[0] if args else kwargs)!r}")

        return lookup

    def wall_clock(tz: tzinfo) -> NoReturn:
        refuse("wall clock", f"db.types: datetime.now({tz})")

    patch.setenv("no_proxy", "*")
    patch.setenv("NO_PROXY", "*")
    patch.setattr(socket.socket, "connect", connecting)
    patch.setattr(socket.socket, "connect_ex", connecting_ex)
    for name in LOOKUPS:
        patch.setattr(socket, name, looking_up(name))
    patch.setattr(clock, "datetime", _NoWallClock)
    patch.setattr(clock, "asyncio", _NoSleep())
    patch.setattr(SystemClock, "_random", _NoSystemRandom())
    patch.setattr(types, "_wall_clock", wall_clock)
