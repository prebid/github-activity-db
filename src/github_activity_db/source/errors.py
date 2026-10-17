"""Errors raised by the GitHub source layer.

By what the caller does next:

- ``FatalSourceError``: stop the run (exit 1). Bad credentials, a missing permission, a response
  this code does not understand, an HTTP client that fails outside the network, or a rate-limit
  wait no deadline bounds.
- ``RepositoryError``: this repository fails for the run; other repositories continue.
- ``ItemError``: one pull request or issue failed; the hydrator reports it as a failed outcome,
  never by raising.
- ``UserLookupFailed``: the lookup of one node id failed; reported in ``UserLookup.failed``, never
  raised, and the caller fails the items waiting on that account.
- ``DeadlineReached``: the run stops issuing requests and drains (redesign plan 5.3 step 4).

``RequestFailedError``: a request failed, after the transport's retries, or because GitHub reported
an error outside every item of it (``UnattributedErrors``, raised by the hydrator without a retry).
The operations turn it into one of the above; a caller of ``GitHubSource.transport`` handles it.
"""

from __future__ import annotations

from datetime import datetime


class SourceError(Exception):
    """Base class for every error the source layer raises."""


class DeadlineReached(SourceError):
    """A request would start, or a wait would end, at or after the ``--max-minutes`` deadline.

    A request already in flight is not cut short; the HTTP timeouts bound it.
    """

    def __init__(self, deadline: datetime) -> None:
        super().__init__(f"deadline {deadline.isoformat()} reached")
        self.deadline = deadline


# --------------------------------------------------------------------------------------------
# Fatal
# --------------------------------------------------------------------------------------------
class FatalSourceError(SourceError):
    """The run cannot continue."""


class Unauthorized(FatalSourceError):
    """The credentials do not work: GitHub answered 401, a credential request (an App
    installation-token mint) answered 4xx without a rate-limit signal, or the auth strategy
    rejected its credentials before any request."""


class Forbidden(FatalSourceError):
    """GitHub answered 403 without a rate-limit signal: a permission is missing."""


class UnexpectedResponse(FatalSourceError):
    """GitHub answered with a status or a request-level error this code does not handle."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ClientFailure(FatalSourceError):
    """The HTTP client failed in a way a retry is not expected to clear: a redirect loop, a proxy
    or URL problem, a request this side built wrong, or a missing dependency of an auth
    strategy."""


class RateLimitWaitTooLong(FatalSourceError):
    """Without a deadline, a rate-limit wait exceeded the longest wait the run accepts."""

    def __init__(self, seconds: float, limit: float) -> None:
        super().__init__(f"rate-limit wait of {seconds:.0f} s exceeds {limit:.0f} s")
        self.seconds = seconds
        self.limit = limit


# --------------------------------------------------------------------------------------------
# Request failures the transport hands to its caller after its own retries
# --------------------------------------------------------------------------------------------
class RequestFailedError(SourceError):
    """A request failed (see the module docstring); the caller splits or gives up."""


class ServerError(RequestFailedError):
    """5xx, a transport error, or a malformed response, still failing after the retries."""


class RequestTooHeavy(RequestFailedError):
    """GitHub timed out or hit a resource limit on the request; a smaller request may pass."""


class UnattributedErrors(RequestFailedError):
    """GitHub reported an error outside every item of a batch or continuation request.

    No single item can take it, so the request counts as failed: it is split down to single
    items, which then fail on their own.
    """


class LookupShapeError(SourceError):
    """A ``nodes(ids:)`` answer did not match the request or the typed models."""


class UserLookupFailed(SourceError):
    """The lookup of one node id failed: its request still failed after the transport's retries,
    or GitHub's answer for that id was an error or did not match.

    The source layer cannot tell which items wait on the account; the caller turns this into an
    item error for each of them (redesign plan 5.2, 5.3 step 4).
    """

    def __init__(self, node_id: str, cause: SourceError) -> None:
        super().__init__(f"user lookup for {node_id} failed: {cause}")
        self.node_id = node_id
        self.cause = cause


# --------------------------------------------------------------------------------------------
# Repository
# --------------------------------------------------------------------------------------------
class RepositoryError(SourceError):
    """A repository fails for this run."""

    def __init__(self, full_name: str, message: str) -> None:
        super().__init__(f"{full_name}: {message}")
        self.full_name = full_name


class RepositoryNotFound(RepositoryError):
    """``repository(owner:, name:)`` returned ``NOT_FOUND``."""

    def __init__(self, full_name: str) -> None:
        super().__init__(full_name, "repository not found")


class RepositoryWithoutDefaultBranch(RepositoryError):
    """The repository has no default branch (``defaultBranchRef`` is null)."""

    def __init__(self, full_name: str) -> None:
        super().__init__(full_name, "repository has no default branch")


class RepositoryRequestFailed(RepositoryError):
    """A resolve or index request still failed after its retries."""

    def __init__(self, full_name: str, cause: SourceError) -> None:
        super().__init__(full_name, f"request failed: {cause}")
        self.cause = cause


class RepositoryShapeError(RepositoryError):
    """A response for the repository did not match the typed models, or GitHub reported errors
    for it, or its paging did not advance."""


class IndexCountMismatch(RepositoryError):
    """The enumerated count differed from the connection's ``totalCount`` on two scans."""

    def __init__(self, full_name: str, connection: str, enumerated: int, total_count: int) -> None:
        super().__init__(
            full_name,
            f"{connection} index enumerated {enumerated} items, totalCount {total_count}",
        )
        self.connection = connection
        self.enumerated = enumerated
        self.total_count = total_count


class IndexOrderViolation(RepositoryError):
    """The index returned an item number twice or out of creation order."""

    def __init__(self, full_name: str, connection: str, number: int, problem: str) -> None:
        super().__init__(full_name, f"{connection} index: #{number} {problem}")
        self.connection = connection
        self.number = number
        self.problem = problem


class LabelCountMismatch(RepositoryError):
    """The repository's label pages did not add up to their ``totalCount``."""

    def __init__(self, full_name: str, fetched: int, total_count: int) -> None:
        super().__init__(full_name, f"fetched {fetched} labels, totalCount {total_count}")
        self.fetched = fetched
        self.total_count = total_count


# --------------------------------------------------------------------------------------------
# Item
# --------------------------------------------------------------------------------------------
class ItemError(SourceError):
    """One item failed; reported as a failed outcome, never raised past the hydrator."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(f"#{number}: {message}")
        self.number = number


class ItemRequestFailed(ItemError):
    """The item's request still failed when sent alone."""

    def __init__(self, number: int, cause: SourceError) -> None:
        super().__init__(number, f"request failed: {cause}")
        self.cause = cause


class AliasError(ItemError):
    """GitHub reported an error other than ``NOT_FOUND`` under the item's alias."""

    def __init__(self, number: int, error_type: str | None, message: str) -> None:
        super().__init__(number, f"GraphQL error {error_type or 'without type'}: {message}")
        self.error_type = error_type


class ResponseShapeError(ItemError):
    """The item's response did not match the typed models (a null, a type, a new enum value)."""


class CompareRefused(ItemError):
    """The compare endpoint answered 404 or 422 for the PR's base and head."""

    def __init__(self, number: int, status: int) -> None:
        super().__init__(number, f"compare answered {status}")
        self.status = status


class InvariantError(ItemError):
    """A count or identity check on the item failed."""


class ConnectionCountMismatch(InvariantError):
    """The fetched entries of a connection differ from its ``totalCount``."""

    def __init__(self, number: int, connection: str, fetched: int, total_count: int) -> None:
        super().__init__(number, f"{connection}: fetched {fetched}, totalCount {total_count}")
        self.connection = connection
        self.fetched = fetched
        self.total_count = total_count


class TotalCountChanged(InvariantError):
    """An overflow page reported a different ``totalCount`` than the first page."""

    def __init__(self, number: int, connection: str, first: int, later: int) -> None:
        super().__init__(number, f"{connection}: totalCount {first} then {later}")
        self.connection = connection
        self.first = first
        self.later = later


class ItemChangedDuringRead(InvariantError):
    """``headRefOid`` or ``updatedAt`` changed between the alias response and an overflow page."""

    def __init__(self, number: int, field: str, first: str, later: str) -> None:
        super().__init__(number, f"{field} changed from {first} to {later} between pages")
        self.field = field
        self.first = first
        self.later = later


class PagingStalled(InvariantError):
    """A page reported a next page without progress (no new cursor, or no entries); following it
    could never end."""

    def __init__(self, number: int, connection: str, cursor: str | None, entries: int) -> None:
        super().__init__(
            number,
            f"{connection}: next page without progress ({entries} entries, cursor {cursor!r})",
        )
        self.connection = connection
        self.cursor = cursor
        self.entries = entries


class DuplicateEntry(InvariantError):
    """A connection listed an entry twice by the key the database stores it under."""

    def __init__(self, number: int, connection: str, key: object) -> None:
        super().__init__(number, f"{connection}: {key!r} listed twice")
        self.connection = connection
        self.key = key


class MergeStateMismatch(InvariantError):
    """``state`` and ``mergedAt`` disagree: a merged pull request has a merge time, others none."""

    def __init__(self, number: int, state: str, merged: bool) -> None:
        super().__init__(number, f"state {state} with mergedAt {'set' if merged else 'null'}")
        self.state = state
        self.merged = merged


class NullEntry(InvariantError):
    """A connection, or an entry in one, was null; never counted as zero."""

    def __init__(self, number: int, connection: str) -> None:
        super().__init__(number, f"{connection}: null connection or entry")
        self.connection = connection


class CompareCountMismatch(InvariantError):
    """The compare endpoint's commit counts disagree with each other or with GraphQL."""

    def __init__(self, number: int, what: str, expected: int, actual: int) -> None:
        super().__init__(number, f"compare {what}: expected {expected}, got {actual}")
        self.what = what
        self.expected = expected
        self.actual = actual


class IdentityMismatch(InvariantError):
    """The response describes a different item than the one requested."""

    def __init__(self, number: int, field: str, expected: object, actual: object) -> None:
        super().__init__(number, f"{field}: requested {expected}, received {actual}")
        self.field = field
        self.expected = expected
        self.actual = actual


class ActorWithoutId(InvariantError):
    """A non-null actor has no ``databaseId`` (``EnterpriseUserAccount``)."""

    def __init__(self, number: int, role: str, typename: str, login: str) -> None:
        super().__init__(number, f"{role} {login} is a {typename}, which has no databaseId")
        self.role = role
        self.typename = typename
        self.login = login
