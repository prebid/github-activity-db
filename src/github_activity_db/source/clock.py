"""Time for the source layer: injectable, so tests never sleep."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Protocol

from .limits import JITTER_FRACTION


class Clock(Protocol):
    """Current time and sleeping."""

    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds``."""
        ...

    def jitter(self, delay: float) -> float:
        """``delay`` plus a random fraction of it, so retries from many clients spread out."""
        ...


class SystemClock:
    """The real clock."""

    _random = secrets.SystemRandom()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def jitter(self, delay: float) -> float:
        return delay + self._random.uniform(0.0, delay * JITTER_FRACTION)
