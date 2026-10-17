"""Entry point of the source layer: one githubkit client, one transport, the operations."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import httpx
from githubkit import GitHub

from .clock import Clock
from .hydrate import PullRequestHydrator
from .index import index_pull_requests
from .limits import CONNECT_TIMEOUT, READ_TIMEOUT
from .resolve import resolve_repository
from .transport import Transport
from .users import lookup_user_batches

if TYPE_CHECKING:
    from githubkit import BaseAuthStrategy

    from .budget import Usage
    from .records import Account, PullRequestIndex, PullRequestOutcome, RepositoryRecord

USER_AGENT = "github-activity-db"


def build_github(
    auth: str | BaseAuthStrategy, *, http_transport: httpx.AsyncBaseTransport | None = None
) -> GitHub[Any]:
    """The githubkit client with every option the source layer relies on set explicitly.

    githubkit 0.14.2 defaults to retrying rate limits and 5xx itself, caching responses, and no
    timeout; the transport owns retries and waits, so all three are turned off or bounded
    (plan 5.7).
    """
    return GitHub(
        auth,
        auto_retry=False,
        http_cache=False,
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        async_transport=http_transport,
        user_agent=USER_AGENT,
    )


class GitHubSource:
    """The GitHub source layer: resolve, index, hydrate, and look up users.

    Use as ``async with GitHubSource(token) as source:`` and create every task that uses it
    inside the block (see ``Transport``).
    """

    def __init__(
        self,
        auth: str | BaseAuthStrategy,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        deadline: datetime | None = None,
    ) -> None:
        self._github = build_github(auth, http_transport=http_transport)
        self._transport = Transport(self._github, clock=clock, deadline=deadline)
        self._exit_stack = AsyncExitStack()

    async def __aenter__(self) -> Self:
        await self._exit_stack.enter_async_context(self._github)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._exit_stack.__aexit__(exc_type, exc, traceback)

    @property
    def usage(self) -> Usage:
        """GraphQL points, GraphQL requests, and REST calls spent so far."""
        return self._transport.usage

    @property
    def transport(self) -> Transport:
        """The low-level transport, for a document the operations below do not send."""
        return self._transport

    async def resolve(self, owner: str, name: str) -> RepositoryRecord:
        return await resolve_repository(self._transport, owner, name)

    async def index_pull_requests(self, repository: RepositoryRecord) -> PullRequestIndex:
        return await index_pull_requests(self._transport, repository)

    def hydrate_pull_requests(
        self, repository: RepositoryRecord, numbers: Sequence[int]
    ) -> AsyncIterator[list[PullRequestOutcome]]:
        """Outcomes batch by batch: ``async for outcomes in source.hydrate_pull_requests(...)``.

        Each batch arrives after its failed items were retried alone, so a deadline or a fatal
        error loses at most the batch in progress.
        """
        return PullRequestHydrator(self._transport, repository).batches(numbers)

    def lookup_users(self, node_ids: Sequence[str]) -> AsyncIterator[dict[str, Account | None]]:
        """Accounts batch by batch, keyed by node id; None where GitHub has no such account."""
        return lookup_user_batches(self._transport, node_ids)
