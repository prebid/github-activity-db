"""Hydration against recorded responses: every scenario and every invariant (plan Step 2).

Recorded units come from public repositories (tests/source/recorder.py names why each was
recorded). A test that needs a case GitHub does not serve on demand modifies a recorded unit in
place and says which one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from github_activity_db.source import (
    Account,
    ClosingIssue,
    Commenter,
    CommitRecord,
    ConnectionTotals,
    Failed,
    FileChange,
    Gone,
    Hydrated,
    LinkedAccount,
    PullRequestOutcome,
    PullRequestRecord,
    Review,
)
from github_activity_db.source.documents import (
    COMMITS,
    PULL_REQUEST_HYDRATION_VERSION,
    overflow_alias,
)
from github_activity_db.source.errors import (
    ActorWithoutId,
    AliasError,
    CompareCountMismatch,
    CompareRefused,
    ConnectionCountMismatch,
    DeadlineReached,
    IdentityMismatch,
    ItemChangedDuringRead,
    ItemRequestFailed,
    NullEntry,
    PagingStalled,
    RepositoryNotFound,
    RepositoryShapeError,
    ResponseShapeError,
    TotalCountChanged,
)
from github_activity_db.source.limits import (
    OVERFLOW_ALIASES_PER_REQUEST,
    PR_BATCH_SIZE,
    SERVER_RETRY_DELAYS,
)
from tests.source.conftest import FakeClock, OpenSource, all_outcomes
from tests.source.recordings import Unit, expected_overflow_stamp
from tests.source.replay import Replay, Seen, synthetic

PREBID_JS = ("prebid", "Prebid.js")
RECORDED_PREBID_JS = [15724, 13481, 12975, 13769, 11155, 8858]


async def hydrate(
    open_source: OpenSource, repository: tuple[str, str], numbers: list[int], **kwargs: Any
) -> list[PullRequestOutcome]:
    async with open_source(**kwargs) as source:
        resolved = await source.resolve(*repository)
        return await all_outcomes(source, resolved, numbers)


async def hydrate_one(open_source, repository: tuple[str, str], number: int) -> PullRequestRecord:
    (outcome,) = await hydrate(open_source, repository, [number])
    assert isinstance(outcome, Hydrated), outcome
    return outcome.record


async def failure(open_source, repository: tuple[str, str], number: int) -> Exception:
    (outcome,) = await hydrate(open_source, repository, [number])
    assert isinstance(outcome, Failed), outcome
    return outcome.error


def batches(replay: Replay) -> list[list[int]]:
    return [seen.numbers for seen in replay.seen if seen.operation == "PullRequestBatch"]


def is_batch(seen: Seen) -> bool:
    return seen.operation == "PullRequestBatch"


def pr_unit(replay: Replay, number: int, repository: tuple[str, str] = PREBID_JS) -> Unit:
    return replay.unit("pull_request", owner=repository[0], name=repository[1], number=number)


def copied(replay: Replay, recorded: int, number: int) -> Unit:
    """Recorded Prebid.js unit ``recorded`` served as pull request ``number``, with its own id."""
    unit = pr_unit(replay, recorded)
    unit.key = {**unit.key, "number": number}
    unit.data["number"] = number
    unit.data["id"] = f"PR_copy_{number}"
    unit.source = f"synthetic: recorded unit #{recorded} served as #{number}"
    return unit


def copies(replay: Replay, count: int) -> list[int]:
    """``count`` pull requests without overflow, copied from recorded #13481."""
    numbers = [900_001 + i for i in range(count)]
    for number in numbers:
        replay.add(copied(replay, 13481, number))
    return numbers


def is_overflow(seen: Seen) -> bool:
    return seen.operation == "Overflow"


def overflow_sizes(replay: Replay) -> list[int]:
    return [
        sum(1 for name in seen.variables if name.startswith("id"))
        for seen in replay.seen
        if is_overflow(seen)
    ]


COMMITS_PAGE_2 = {"id": "PR_kwDOAlf9v85oRaET", "connection": "commits", "after": "MTAw"}
"""Recorded second commits page of #11155 (101 commits)."""


# --------------------------------------------------------------------------------------------
# Recorded scenarios
# --------------------------------------------------------------------------------------------
class TestRecordedScenarios:
    async def test_one_batch_returns_one_outcome_per_number_in_order(
        self, open_source, replay: Replay
    ) -> None:
        numbers = [*RECORDED_PREBID_JS, 999999]
        outcomes = await hydrate(open_source, PREBID_JS, numbers)
        assert [outcome.number for outcome in outcomes] == numbers
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes[:-1])
        assert outcomes[-1] == Gone(999999)
        assert batches(replay) == [numbers]

    async def test_a_bot_author_and_its_commit_link(self, open_source) -> None:
        record = await hydrate_one(open_source, PREBID_JS, 15724)
        # GraphQL: Bot/dependabot as author; dependabot[bot] as the commit's user.
        assert record.author == Account(
            github_id=49699333, node_id="MDM6Qm90NDk2OTkzMzM=", login="dependabot", type="Bot"
        )
        assert record.commit_links() == (
            LinkedAccount(
                github_id=49699333, node_id="MDM6Qm90NDk2OTkzMzM=", login="dependabot[bot]"
            ),
        )
        assert record.author in record.accounts()

    async def test_ids_above_32_bits(self, open_source) -> None:
        record = await hydrate_one(open_source, PREBID_JS, 15724)
        assert record.github_id == 4757532486
        assert record.github_id > 2**31

    async def test_a_duplicate_path_keeps_both_entries(self, open_source) -> None:
        record = await hydrate_one(open_source, PREBID_JS, 13481)
        assert record.changed_files == 2
        assert record.totals.files == len(record.files) == 3
        assert [change.path for change in record.files].count("CLAUDE.md") == 2

    async def test_null_author_and_a_deleted_commenter(self, open_source) -> None:
        record = await hydrate_one(open_source, PREBID_JS, 12975)
        assert record.author is None
        assert record.deleted_author_comment_count == 1
        assert sum(commenter.comment_count for commenter in record.commenters) == 1
        assert record.totals.comments == 2

    async def test_null_merger(self, open_source) -> None:
        record = await hydrate_one(open_source, PREBID_JS, 13769)
        assert record.state == "MERGED"
        assert record.merged_at is not None
        assert record.merged_by is None

    async def test_commit_overflow_through_node_id(self, open_source, replay: Replay) -> None:
        total = pr_unit(replay, 11155).data["commits"]["totalCount"]
        assert 100 < total <= 250
        record = await hydrate_one(open_source, PREBID_JS, 11155)
        assert record.commits_source == "graphql"
        assert record.totals.commits == len(record.commits) == total
        assert [seen.operation for seen in replay.seen].count("Overflow") == 1

    async def test_more_than_250_commits_come_from_compare(
        self, open_source, replay: Replay
    ) -> None:
        total = pr_unit(replay, 8858).data["commits"]["totalCount"]
        assert total > 250
        record = await hydrate_one(open_source, PREBID_JS, 8858)
        assert record.commits_source == "compare"
        assert record.totals.commits == len(record.commits) == total
        assert len({commit.sha for commit in record.commits}) == total
        assert [seen.operation for seen in replay.seen].count("rest") == 3
        assert "Overflow" not in [seen.operation for seen in replay.seen]

    async def test_a_dismissed_verdict(self, open_source) -> None:
        record = await hydrate_one(open_source, ("prebid", "prebid-server-java"), 4637)
        states = [
            (review.reviewer.login if review.reviewer else None, review.state)
            for review in record.reviews
        ]
        assert states == [
            ("Lightwood13", "CHANGES_REQUESTED"),
            ("Lightwood13", "DISMISSED"),
            ("CTMBNara", "APPROVED"),
            ("And1sS", "APPROVED"),
        ]
        assert [review.github_id for review in record.reviews] == sorted(
            review.github_id for review in record.reviews
        )

    @pytest.mark.parametrize(
        ("repository", "number", "connection"),
        [
            (("prebid", "prebid-mobile-ios"), 851, "files"),
            (("prebid", "salesagent"), 1699, "reviews"),
            (("prebid", "prebid-server"), 4343, "comments"),
        ],
    )
    async def test_overflow_on_each_recorded_connection(
        self, open_source, replay: Replay, repository, number, connection
    ) -> None:
        total = pr_unit(replay, number, repository).data[connection]["totalCount"]
        assert total > 100
        record = await hydrate_one(open_source, repository, number)
        assert getattr(record.totals, connection) == total
        fetched = {
            "files": len(record.files),
            "reviews": len(record.reviews),
            "comments": sum(c.comment_count for c in record.commenters)
            + record.deleted_author_comment_count,
        }
        assert fetched[connection] == total

    async def test_record_dates_and_version(self, open_source, replay: Replay) -> None:
        record = await hydrate_one(open_source, PREBID_JS, 13481)
        unit = pr_unit(replay, 13481)
        date = datetime.strptime(unit.headers["date"], "%a, %d %b %Y %H:%M:%S GMT").replace(
            tzinfo=UTC
        )
        assert record.hydrated_at == date
        assert record.github_updated_at == datetime.fromisoformat(unit.data["updatedAt"])
        assert record.hydration_version == PULL_REQUEST_HYDRATION_VERSION
        assert record.created_at.tzinfo is UTC


def recorded_pull_requests() -> list[Unit]:
    return [
        unit
        for unit in Replay.from_fixtures().units.values()
        if unit.kind == "pull_request" and unit.data is not None
    ]


def wire_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def wire_account(actor: dict[str, Any] | None) -> Account | None:
    if actor is None:
        return None
    return Account(
        github_id=actor["databaseId"],
        node_id=actor["id"],
        login=actor["login"],
        type=actor["__typename"],
    )


@pytest.mark.parametrize(
    "unit",
    recorded_pull_requests(),
    ids=lambda unit: f"{unit.key['name']}#{unit.key['number']}",
)
async def test_every_field_maps_from_its_wire_name(open_source, unit: Unit) -> None:
    """Each record field against the recorded JSON it comes from, read by its wire name."""
    data = unit.data
    record = await hydrate_one(open_source, (unit.key["owner"], unit.key["name"]), data["number"])
    assert (
        record.repository,
        record.github_id,
        record.node_id,
        record.number,
        record.state,
        record.is_draft,
        record.title,
        record.body,
        record.author,
        record.author_association,
        record.created_at,
        record.merged_at,
        record.merged_by,
        record.base_ref,
        record.is_cross_repository,
        record.additions,
        record.deletions,
        record.changed_files,
        record.github_updated_at,
    ) == (
        f"{unit.key['owner']}/{unit.key['name']}",
        int(data["fullDatabaseId"]),
        data["id"],
        data["number"],
        data["state"],
        data["isDraft"],
        data["title"],
        data["body"],
        wire_account(data["author"]),
        data["authorAssociation"],
        wire_time(data["createdAt"]),
        wire_time(data["mergedAt"]),
        wire_account(data["mergedBy"]),
        data["baseRefName"],
        data["isCrossRepository"],
        data["additions"],
        data["deletions"],
        data["changedFiles"],
        wire_time(data["updatedAt"]),
    )
    assert record.totals == ConnectionTotals(
        labels=data["labels"]["totalCount"],
        closing_issues=data["closingIssuesReferences"]["totalCount"],
        reviews=data["reviews"]["totalCount"],
        comments=data["comments"]["totalCount"],
        commits=data["commits"]["totalCount"],
        files=data["files"]["totalCount"],
    )
    # The first page of each connection leads the record's entries, in order.
    labels = [node["name"] for node in data["labels"]["nodes"]]
    assert list(record.labels[: len(labels)]) == labels
    closing = [
        ClosingIssue(
            repo_github_id=node["repository"]["databaseId"],
            repo_full_name=node["repository"]["nameWithOwner"],
            number=node["number"],
        )
        for node in data["closingIssuesReferences"]["nodes"]
    ]
    assert list(record.closing_issues[: len(closing)]) == closing
    reviews = [
        Review(
            github_id=int(node["fullDatabaseId"]),
            reviewer=wire_account(node["author"]),
            state=node["state"],
        )
        for node in data["reviews"]["nodes"]
    ]
    assert list(record.reviews[: len(reviews)]) == reviews
    files = [
        FileChange(
            path=node["path"],
            change_type=node["changeType"],
            additions=node["additions"],
            deletions=node["deletions"],
        )
        for node in data["files"]["nodes"]
    ]
    assert list(record.files[: len(files)]) == files
    if record.commits_source == "graphql":
        commits = [
            CommitRecord(
                sha=node["commit"]["oid"],
                author=wire_link(node["commit"]["author"]),
                author_name=(node["commit"]["author"] or {}).get("name"),
                author_email=(node["commit"]["author"] or {}).get("email"),
                authored_at=datetime.fromisoformat(node["commit"]["authoredDate"]),
            )
            for node in data["commits"]["nodes"]
        ]
        assert list(record.commits[: len(commits)]) == commits


def wire_link(git_actor: dict[str, Any] | None) -> LinkedAccount | None:
    user = (git_actor or {}).get("user")
    if user is None:
        return None
    return LinkedAccount(github_id=user["databaseId"], node_id=user["id"], login=user["login"])


# --------------------------------------------------------------------------------------------
# Synthetic overflow for labels and closing issues (no tracked PR has more than 100)
# --------------------------------------------------------------------------------------------
def overflow_unit(item: Unit, connection: str, after: str, nodes: list[Any], total: int) -> Unit:
    return Unit(
        kind="overflow",
        key={"id": item.data["id"], "connection": connection, "after": after},
        document_sha256=expected_overflow_stamp(connection),
        recorded_at=item.recorded_at,
        headers=item.headers,
        data={
            "headRefOid": item.data["headRefOid"],
            "updatedAt": item.data["updatedAt"],
            connection: {
                "totalCount": total,
                "pageInfo": {"hasNextPage": False, "endCursor": "end"},
                "nodes": nodes,
            },
        },
        source=f"synthetic: a second {connection} page for recorded unit #{item.key['number']}",
    )


@pytest.mark.parametrize(
    ("connection", "node", "read"),
    [
        ("labels", {"name": "extra"}, lambda record: record.labels),
        (
            "closingIssuesReferences",
            {
                "number": 1,
                "repository": {"databaseId": 39321023, "nameWithOwner": "prebid/Prebid.js"},
            },
            lambda record: record.closing_issues,
        ),
    ],
)
async def test_overflow_on_labels_and_closing_issues(
    open_source, replay: Replay, connection: str, node: dict, read
) -> None:
    unit = pr_unit(replay, 12975)  # one label and one closing issue, recorded
    page = unit.data[connection]
    page["totalCount"] += 1
    page["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
    replay.add(unit)
    replay.add(overflow_unit(unit, connection, "page-2", [node], page["totalCount"]))
    record = await hydrate_one(open_source, PREBID_JS, 12975)
    assert len(read(record)) == page["totalCount"] == 2


# --------------------------------------------------------------------------------------------
# Invariants: each failure is an outcome after one solo retry, never an exception
# --------------------------------------------------------------------------------------------
class TestInvariants:
    async def test_a_connection_count_mismatch(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["files"]["totalCount"] = 4
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, ConnectionCountMismatch)
        assert (error.connection, error.fetched, error.total_count) == ("files", 3, 4)

    @pytest.mark.parametrize(
        "connection",
        ["labels", "closingIssuesReferences", "reviews", "comments", "commits", "files"],
    )
    async def test_every_connection_is_count_checked(
        self, open_source, replay: Replay, connection: str
    ) -> None:
        unit = pr_unit(replay, 12975)
        unit.data[connection]["totalCount"] += 1
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 12975)
        assert isinstance(error, ConnectionCountMismatch)
        assert error.connection == connection

    @pytest.mark.parametrize("where", ["connection", "nodes", "entry"])
    async def test_a_null_is_never_counted_as_zero(
        self, open_source, replay: Replay, where: str
    ) -> None:
        unit = pr_unit(replay, 13481)
        if where == "connection":
            unit.data["files"] = None
        elif where == "nodes":
            unit.data["files"]["nodes"] = None
        else:
            unit.data["files"]["nodes"][0] = None
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, NullEntry)
        assert error.connection == "files"

    async def test_a_null_entry_on_an_overflow_page(self, open_source, replay: Replay) -> None:
        unit = replay.unit("overflow", id="PR_kwDOAlf9v85oRaET", connection="commits", after="MTAw")
        unit.data["commits"]["nodes"][0] = None
        replay.add(unit)
        assert isinstance(await failure(open_source, PREBID_JS, 11155), NullEntry)

    @pytest.mark.parametrize(
        ("field", "value"), [("headRefOid", "0" * 40), ("updatedAt", "2030-01-01T00:00:00Z")]
    )
    async def test_a_change_between_pages(
        self, open_source, replay: Replay, field: str, value: str
    ) -> None:
        unit = replay.unit("overflow", id="PR_kwDOAlf9v85oRaET", connection="commits", after="MTAw")
        unit.data[field] = value
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, ItemChangedDuringRead)
        assert error.field == field

    async def test_a_total_that_changes_between_pages(self, open_source, replay: Replay) -> None:
        unit = replay.unit("overflow", id="PR_kwDOAlf9v85oRaET", connection="commits", after="MTAw")
        unit.data["commits"]["totalCount"] = 102
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, TotalCountChanged)
        assert (error.first, error.later) == (101, 102)

    async def test_an_enterprise_actor_has_no_id(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["author"] = {"__typename": "EnterpriseUserAccount", "login": "someone"}
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, ActorWithoutId)
        assert error.role == "author"

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("reviews", "nodes", 0, "state"), "PENDING"),
            (("isDraft",), "no"),
            (("fullDatabaseId",), "1_2"),
            (("fullDatabaseId",), True),
            (("createdAt",), 1_700_000_000),
            (("author", "__typename"), "Ghost"),
        ],
        ids=[
            "pending-review",
            "string-bool",
            "bigint-with-underscore",
            "bigint-as-bool",
            "numeric-date",
            "unknown-actor",
        ],
    )
    async def test_a_value_the_models_reject(
        self, open_source, replay: Replay, path, value
    ) -> None:
        unit = pr_unit(replay, 13481)
        target = unit.data
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        replay.add(unit)
        assert isinstance(await failure(open_source, PREBID_JS, 13481), ResponseShapeError)

    async def test_the_returned_number_must_match(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["number"] = 13482
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, IdentityMismatch)
        assert error.field == "number"

    async def test_the_repository_must_match(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.extra["repository_database_id"] = 1
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, IdentityMismatch)
        assert error.field == "repository"

    async def test_an_alias_error_other_than_not_found(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.data = None
        unit.errors = [{"type": "FORBIDDEN", "path": [], "message": "Resource not accessible"}]
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, AliasError)
        assert error.error_type == "FORBIDDEN"

    async def test_an_error_on_a_continuation_page(self, open_source, replay: Replay) -> None:
        unit = replay.unit("overflow", id="PR_kwDOAlf9v85oRaET", connection="commits", after="MTAw")
        unit.errors = [{"type": "SERVICE_UNAVAILABLE", "path": ["commits"], "message": "partial"}]
        replay.add(unit)
        assert isinstance(await failure(open_source, PREBID_JS, 11155), AliasError)

    async def test_partial_data_under_an_alias(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.errors = [{"type": "SERVICE_UNAVAILABLE", "path": ["files"], "message": "partial"}]
        replay.add(unit)
        assert isinstance(await failure(open_source, PREBID_JS, 13481), AliasError)

    async def test_a_null_continuation_without_errors(self, open_source, replay: Replay) -> None:
        unit = replay.unit("overflow", **COMMITS_PAGE_2)
        unit.data = None
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, AliasError)
        assert error.error_type is None

    async def test_a_next_page_without_a_cursor_on_the_first_page(
        self, open_source, replay: Replay
    ) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["files"]["pageInfo"] = {"hasNextPage": True, "endCursor": None}
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, PagingStalled)
        assert (error.connection, error.cursor) == ("files", None)

    @pytest.mark.parametrize("cursor", ["MTAw", None], ids=["repeated", "missing"])
    async def test_a_continuation_without_a_new_cursor(
        self, open_source, replay: Replay, cursor: str | None
    ) -> None:
        # The batch and its solo retry each read a stalled page. A hydrator that followed the
        # cursor would read the recorded page on its third request and end, not loop.
        def stall(body: dict[str, Any]) -> None:
            page = body["data"][overflow_alias(0, COMMITS)]["commits"]
            page["pageInfo"] = {"hasNextPage": True, "endCursor": cursor}

        replay.modify(stall, when=is_overflow, times=2)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, PagingStalled)
        assert (error.connection, error.cursor) == ("commits", cursor)

    async def test_more_entries_than_the_total_stop_paging(
        self, open_source, replay: Replay
    ) -> None:
        # One entry over the total and a further page: the hydrator stops instead of reading
        # a page the replay does not have.
        def overfull(body: dict[str, Any]) -> None:
            page = body["data"][overflow_alias(0, COMMITS)]["commits"]
            page["nodes"].append(page["nodes"][0])
            page["pageInfo"] = {"hasNextPage": True, "endCursor": "beyond"}

        replay.modify(overfull, when=is_overflow, times=None)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, ConnectionCountMismatch)
        assert (error.connection, error.fetched, error.total_count) == ("commits", 102, 101)

    async def test_commenters_are_counted_by_id_not_login(
        self, open_source, replay: Replay
    ) -> None:
        unit = pr_unit(replay, 12975)
        author = unit.data["comments"]["nodes"][0]["author"]
        renamed = {**author, "login": "dgirardi-renamed"}
        unit.data["comments"]["nodes"] = [{"author": author}, {"author": None}, {"author": renamed}]
        unit.data["comments"]["totalCount"] = 3
        replay.add(unit)
        record = await hydrate_one(open_source, PREBID_JS, 12975)
        latest = wire_account(renamed)
        assert latest is not None
        # One account, two comments; the latest sighting supplies the login.
        assert record.commenters == (Commenter(account=latest, comment_count=2),)
        assert record.deleted_author_comment_count == 1


class TestCompare:
    def compare_page(self, replay: Replay, page: int) -> Unit:
        unit = next(
            u for u in replay.units.values() if u.kind == "compare" and u.key["page"] == page
        )
        return replay.unit("compare", **unit.key)

    async def test_total_commits_must_match_graphql(self, open_source, replay: Replay) -> None:
        unit = self.compare_page(replay, 1)
        unit.data["total_commits"] = 253
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 8858)
        assert isinstance(error, CompareCountMismatch)
        assert error.what == "total_commits vs commits.totalCount"

    async def test_total_commits_must_not_change_between_pages(
        self, open_source, replay: Replay
    ) -> None:
        unit = self.compare_page(replay, 2)
        unit.data["total_commits"] = 255
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 8858)
        assert isinstance(error, CompareCountMismatch)
        assert error.what == "total_commits between pages"

    async def test_fetched_commits_must_match(self, open_source, replay: Replay) -> None:
        unit = self.compare_page(replay, 3)
        unit.data["commits"] = unit.data["commits"][:-1]
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 8858)
        assert isinstance(error, CompareCountMismatch)
        assert error.what == "fetched commits"

    @pytest.mark.parametrize("status", [404, 422])
    async def test_a_refused_compare(self, open_source, replay: Replay, status: int) -> None:
        replay.fault(
            lambda request: httpx.Response(status, json={"message": "refused"}),
            when=lambda s: s.operation == "rest",
            times=None,
        )
        error = await failure(open_source, PREBID_JS, 8858)
        assert isinstance(error, CompareRefused)
        assert error.status == status

    async def test_an_empty_linked_account_is_none(self, open_source, replay: Replay) -> None:
        # GitHub's REST description types a commit's author as an account, {}, or null.
        unit = self.compare_page(replay, 1)
        commit = next(entry for entry in unit.data["commits"] if entry["author"])
        commit["author"] = {}
        replay.add(unit)
        record = await hydrate_one(open_source, PREBID_JS, 8858)
        (converted,) = [entry for entry in record.commits if entry.sha == commit["sha"]]
        assert converted.author is None
        assert converted.author_name == commit["commit"]["author"]["name"]

    async def test_a_compare_commit_without_a_git_author(self, open_source, replay: Replay) -> None:
        unit = self.compare_page(replay, 2)
        unit.data["commits"][0]["commit"]["author"] = None
        replay.add(unit)
        assert isinstance(await failure(open_source, PREBID_JS, 8858), ResponseShapeError)


# --------------------------------------------------------------------------------------------
# Splitting and retrying
# --------------------------------------------------------------------------------------------
class TestSplitAndRetry:
    @pytest.mark.parametrize(
        "name", ["bad_gateway", "gateway_timeout", "graphql_timeout", "resource_limit"]
    )
    async def test_a_timeout_splits_down_to_single_items(
        self, open_source, replay: Replay, clock: FakeClock, name: str
    ) -> None:
        replay.fault(
            lambda request: synthetic(name),
            when=lambda s: is_batch(s) and len(s.numbers) > 1,
            times=None,
        )
        outcomes = await hydrate(open_source, PREBID_JS, RECORDED_PREBID_JS)
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        # Halves until single items pass: 6 -> 3 (-> 1, 2 -> 1, 1) and 3 (-> 1, 2 -> 1, 1).
        assert [len(batch) for batch in batches(replay)] == [6, 3, 1, 2, 1, 1, 3, 1, 2, 1, 1]
        assert clock.sleeps == []  # a splittable timeout splits at once, without retries

    @staticmethod
    def raising(error: Exception):
        def fail(request: httpx.Request) -> httpx.Response:
            raise error

        return fail

    @pytest.mark.parametrize(
        "respond",
        [
            lambda request: synthetic("server_error"),
            raising(httpx.ConnectError("refused")),
            raising(httpx.ReadTimeout("slow")),
        ],
        ids=["500", "connect", "read-timeout"],
    )
    async def test_a_server_error_retries_then_splits(
        self, open_source, replay: Replay, clock: FakeClock, respond
    ) -> None:
        replay.fault(
            respond,
            when=lambda s: is_batch(s) and len(s.numbers) > 1,
            times=None,
        )
        outcomes = await hydrate(open_source, PREBID_JS, [13481, 15724])
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        assert [len(batch) for batch in batches(replay)] == [2, 2, 2, 2, 1, 1]
        assert clock.sleeps == list(SERVER_RETRY_DELAYS)

    async def test_an_item_that_fails_once_is_retried_alone(
        self, open_source, replay: Replay
    ) -> None:
        def fail_13481(body: dict) -> None:
            body["data"]["repository"]["p1"] = None
            body.setdefault("errors", []).append(
                {"type": "INTERNAL", "path": ["repository", "p1"], "message": "flaky"}
            )

        replay.modify(fail_13481, when=lambda s: is_batch(s) and s.numbers == [15724, 13481])
        outcomes = await hydrate(open_source, PREBID_JS, [15724, 13481])
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        assert batches(replay) == [[15724, 13481], [13481]]

    async def test_an_item_that_fails_twice_is_failed(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["files"]["totalCount"] = 4
        replay.add(unit)
        outcomes = await hydrate(open_source, PREBID_JS, [15724, 13481])
        assert isinstance(outcomes[0], Hydrated)
        assert isinstance(outcomes[1], Failed)
        assert batches(replay) == [[15724, 13481], [13481]]

    async def test_a_single_item_whose_request_keeps_failing(
        self, open_source, replay: Replay
    ) -> None:
        replay.fault(lambda request: synthetic("bad_gateway"), when=is_batch, times=None)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, ItemRequestFailed)

    async def test_a_failing_continuation_fails_its_item(self, open_source, replay: Replay) -> None:
        replay.fault(
            lambda request: synthetic("server_error"),
            when=lambda s: s.operation == "Overflow",
            times=None,
        )
        outcomes = await hydrate(open_source, PREBID_JS, [11155, 13481])
        assert isinstance(outcomes[0], Failed)
        assert isinstance(outcomes[0].error, ItemRequestFailed)
        assert isinstance(outcomes[1], Hydrated)

    async def test_a_failing_continuation_request_is_split(
        self, open_source, replay: Replay
    ) -> None:
        unit = pr_unit(replay, 12975)
        unit.data["labels"]["totalCount"] += 1
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        replay.add(
            overflow_unit(
                unit, "labels", "page-2", [{"name": "x"}], unit.data["labels"]["totalCount"]
            )
        )
        replay.fault(
            lambda request: synthetic("bad_gateway"),
            when=lambda s: s.operation == "Overflow" and "id1" in s.variables,
            times=None,
        )
        outcomes = await hydrate(open_source, PREBID_JS, [11155, 12975])
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        assert overflow_sizes(replay) == [2, 1, 1]
        # Split continuations complete both items; neither is retried alone.
        assert batches(replay) == [[11155, 12975]]

    async def test_continuations_from_several_items_share_a_request(
        self, open_source, replay: Replay
    ) -> None:
        outcomes = await hydrate(open_source, PREBID_JS, [11155])
        assert isinstance(outcomes[0], Hydrated)
        # Two items with pending pages share one Overflow request.
        replay.seen.clear()
        unit = pr_unit(replay, 12975)
        unit.data["labels"]["totalCount"] += 1
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        replay.add(
            overflow_unit(
                unit, "labels", "page-2", [{"name": "x"}], unit.data["labels"]["totalCount"]
            )
        )
        outcomes = await hydrate(open_source, PREBID_JS, [11155, 12975])
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        overflow = [seen for seen in replay.seen if seen.operation == "Overflow"]
        assert len(overflow) == 1
        assert sorted(name for name in overflow[0].variables if name.startswith("id")) == [
            "id0",
            "id1",
        ]

    @pytest.mark.parametrize(
        ("errors", "raised"),
        [
            (
                [{"type": "NOT_FOUND", "path": ["repository"], "message": "gone"}],
                RepositoryNotFound,
            ),
            (
                [{"type": "FORBIDDEN", "path": ["repository"], "message": "no"}],
                RepositoryShapeError,
            ),
            ([], RepositoryShapeError),
        ],
        ids=["not-found", "forbidden", "no-error"],
    )
    async def test_a_null_repository_fails_the_repository(
        self, open_source, replay: Replay, errors: list, raised: type
    ) -> None:
        def no_repository(body: dict) -> None:
            body["data"]["repository"] = None
            body["errors"] = errors

        replay.modify(no_repository, when=is_batch)
        with pytest.raises(raised):
            await hydrate(open_source, PREBID_JS, [13481])

    async def test_the_deadline_stops_hydration(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic(
                "rate_limited",
                headers={"x-ratelimit-reset": str(int(clock.now().timestamp()) + 3600)},
            ),
            when=is_batch,
        )
        with pytest.raises(DeadlineReached):
            await hydrate(
                open_source, PREBID_JS, [13481], deadline=clock.now() + timedelta(minutes=10)
            )


# --------------------------------------------------------------------------------------------
# Batching and delivery
# --------------------------------------------------------------------------------------------
class TestBatches:
    async def test_numbers_are_sent_in_batches(self, open_source, replay: Replay) -> None:
        numbers = copies(replay, PR_BATCH_SIZE + 1)
        outcomes = await hydrate(open_source, PREBID_JS, numbers)
        assert [outcome.number for outcome in outcomes] == numbers
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        assert batches(replay) == [numbers[:PR_BATCH_SIZE], numbers[PR_BATCH_SIZE:]]

    async def test_a_batch_is_delivered_before_the_next_is_sent(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        numbers = copies(replay, PR_BATCH_SIZE + 1)
        replay.fault(
            lambda request: synthetic(
                "rate_limited",
                headers={"x-ratelimit-reset": str(int(clock.now().timestamp()) + 3600)},
            ),
            when=lambda s: is_batch(s) and s.numbers == numbers[PR_BATCH_SIZE:],
        )
        delivered: list[list[int]] = []
        async with open_source(deadline=clock.now() + timedelta(minutes=10)) as source:
            repository = await source.resolve(*PREBID_JS)
            with pytest.raises(DeadlineReached):
                async for batch in source.hydrate_pull_requests(repository, numbers):
                    delivered.append([outcome.number for outcome in batch])
        assert delivered == [numbers[:PR_BATCH_SIZE]]

    async def test_each_number_is_hydrated_once(self, open_source, replay: Replay) -> None:
        numbers = copies(replay, PR_BATCH_SIZE)
        given = [*numbers, numbers[0], 13481, 13481]
        outcomes = await hydrate(open_source, PREBID_JS, given)
        assert [outcome.number for outcome in outcomes] == [*numbers, 13481]
        assert batches(replay) == [numbers, [13481]]

    async def test_continuations_are_grouped_per_request(self, open_source, replay: Replay) -> None:
        # Two continuations per item; one more than a request holds.
        items = OVERFLOW_ALIASES_PER_REQUEST // 2 + 1
        second_pages = {
            "labels": {"name": "extra"},
            "closingIssuesReferences": {
                "number": 1,
                "repository": {"databaseId": 39321023, "nameWithOwner": "prebid/Prebid.js"},
            },
        }
        numbers = [900_001 + i for i in range(items)]
        for number in numbers:
            unit = copied(replay, 12975, number)  # one label and one closing issue, recorded
            for connection, node in second_pages.items():
                page = unit.data[connection]
                page["totalCount"] += 1
                page["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
                replay.add(overflow_unit(unit, connection, "page-2", [node], page["totalCount"]))
            replay.add(unit)
        outcomes = await hydrate(open_source, PREBID_JS, numbers)
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        assert overflow_sizes(replay) == [
            OVERFLOW_ALIASES_PER_REQUEST,
            2 * items - OVERFLOW_ALIASES_PER_REQUEST,
        ]
