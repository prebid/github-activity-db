"""What every test runs under (redesign plan D40), installed for the session by the conftest.

Inside the test process, a connection to anything but a Unix socket and a host-name lookup are
refused, and so are this project's real clock (``SystemClock.now`` and ``sleep``) and its unseeded
random source (``SystemClock._random``, which an instance may replace with a seeded one). A refusal
raises ``NotDeterministic``, a ``BaseException`` so that no ``except Exception`` in the code under
test can turn it into a handled failure, and is recorded in ``REFUSED``, which the conftest checks
after every test. Out of scope: processes a test starts (they run without these patches), the
standard library's clocks and random modules called directly (``tests/test_suite_rules.py`` reads
test files for those), and ``db.types.utc_now``, the default of columns no producer writes.
"""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import pytest

from github_activity_db.source.clock import SystemClock


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


class _NoSystemRandom:
    def uniform(self, low: float, high: float) -> NoReturn:
        refuse("randomness", f"SystemClock._random.uniform({low}, {high})")


def install(patch: pytest.MonkeyPatch) -> None:
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def connecting(self: socket.socket, address: Any) -> None:
        if self.family != socket.AF_UNIX:
            refuse("network", f"connect {address!r}")
        connect(self, address)

    def connecting_ex(self: socket.socket, address: Any) -> int:
        if self.family != socket.AF_UNIX:
            refuse("network", f"connect {address!r}")
        return connect_ex(self, address)

    def looking_up(host: Any, port: Any, *args: Any, **kwargs: Any) -> NoReturn:
        refuse("network", f"getaddrinfo {host!r} {port!r}")

    def now(self: SystemClock) -> NoReturn:
        refuse("wall clock", "SystemClock.now()")

    async def sleep(self: SystemClock, seconds: float) -> NoReturn:
        refuse("wall clock", f"SystemClock.sleep({seconds})")

    patch.setattr(socket.socket, "connect", connecting)
    patch.setattr(socket.socket, "connect_ex", connecting_ex)
    patch.setattr(socket, "getaddrinfo", looking_up)
    patch.setattr(SystemClock, "now", now)
    patch.setattr(SystemClock, "sleep", sleep)
    patch.setattr(SystemClock, "_random", _NoSystemRandom())
