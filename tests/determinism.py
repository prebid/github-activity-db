"""What every test runs under (redesign plan D40), installed by the conftest from collection on.

Inside the test process, a connection to anything but a Unix socket and every host-name lookup the
``socket`` module offers are refused, and so are this project's real clock (``SystemClock.now`` and
``sleep``) and its unseeded random source (``SystemClock._random``, which an instance may replace
with a seeded one). ``no_proxy`` is ``*``, so an HTTP client that reads proxies from the
environment, or on macOS from the system settings, connects straight to the host it was given and
is refused there, whatever the machine's proxy settings. A refusal raises ``NotDeterministic``, a
``BaseException`` so that no ``except Exception`` in the code under test can turn it into a handled
failure, and is recorded in ``REFUSED``, which the conftest checks after every test and at the end
of the session. Out of scope: processes a test starts (they run without these patches), the
standard library's clocks and random modules called directly (``tests/test_suite_rules.py`` reads
test files for those, and the package for its own reads), and ``db.types.utc_now``, the default of
columns no producer writes.
"""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import pytest

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
            refuse("network", f"connect_ex {address!r}")
        return connect_ex(self, address)

    def looking_up(name: str) -> Any:
        def lookup(looked_up: Any, *args: Any, **kwargs: Any) -> NoReturn:
            refuse("network", f"{name} {looked_up!r}")

        return lookup

    def now(self: SystemClock) -> NoReturn:
        refuse("wall clock", "SystemClock.now()")

    async def sleep(self: SystemClock, seconds: float) -> NoReturn:
        refuse("wall clock", f"SystemClock.sleep({seconds})")

    patch.setenv("no_proxy", "*")
    patch.setenv("NO_PROXY", "*")
    patch.setattr(socket.socket, "connect", connecting)
    patch.setattr(socket.socket, "connect_ex", connecting_ex)
    for name in LOOKUPS:
        patch.setattr(socket, name, looking_up(name))
    patch.setattr(SystemClock, "now", now)
    patch.setattr(SystemClock, "sleep", sleep)
    patch.setattr(SystemClock, "_random", _NoSystemRandom())
