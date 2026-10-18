"""Hydration against recorded responses: the recorded scenarios and the invariant checks (plan
Step 2).

Recorded units come from public repositories (tests/source/recorder.py names why each was
recorded). A test that needs a case GitHub does not serve on demand modifies a recorded unit in
place and says which one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from github_activity_db.source import (
    Account,
    Comment,
    CommitRecord,
    ConnectionTotals,
    Failed,
    FileChange,
    Gone,
    HeadRepository,
    HeadRepositoryOwner,
    Hydrated,
    Label,
    LinkedAccount,
    LinkedClosingIssue,
    Milestone,
    PullRequestOutcome,
    PullRequestRecord,
    RequestedTeam,
    Review,
    ReviewRequest,
    Stack,
    StackEntry,
)
from github_activity_db.source.documents import (
    COMMITS,
    PULL_REQUEST_HYDRATION_VERSION,
    overflow_alias,
)
from github_activity_db.source.errors import (
    AliasError,
    CloseTimeMissing,
    CompareCountMismatch,
    CompareRefused,
    ConnectionCountMismatch,
    DeadlineReached,
    DuplicateEntry,
    IdentityMismatch,
    ItemChangedDuringRead,
    ItemRequestFailed,
    MergeStateMismatch,
    NullEntry,
    PagingStalled,
    RepositoryNotFound,
    RepositoryShapeError,
    RequestTooHeavy,
    ResponseShapeError,
    ServerError,
    TotalCountChanged,
    UnattributedErrors,
    UserLinkedIssueNotListed,
)
from github_activity_db.source.hydrate import _uses_compare
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


def parsed_date(unit: Unit) -> datetime:
    return datetime.strptime(unit.headers["date"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)


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
        assert [comment.author is None for comment in record.comments] == [False, True]
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
            "comments": len(record.comments),
        }
        assert fetched[connection] == total

    @pytest.mark.parametrize("number", [11155, 8858], ids=["with-continuation", "with-compare"])
    async def test_hydrated_at_is_the_alias_response_date(
        self, open_source, replay: Replay, number: int
    ) -> None:
        # The alias answered before its continuation (11155) or compare pages (8858). Their
        # recorded Date headers can fall in the same second, so each later response is served
        # an hour after the alias's.
        unit = pr_unit(replay, number)
        kind = "overflow" if number == 11155 else "compare"
        later = [
            u
            for u in replay.units.values()
            if u.kind == kind and (kind == "compare" or u.key["id"] == unit.data["id"])
        ]
        assert later
        alias_date = parsed_date(unit)
        for u in later:
            u.headers["date"] = format_datetime(alias_date + timedelta(hours=1), usegmt=True)
        assert all(parsed_date(u) != alias_date for u in later)
        record = await hydrate_one(open_source, PREBID_JS, number)
        assert record.hydrated_at == alias_date

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
    if actor["__typename"] == "EnterpriseUserAccount":
        return Account(
            github_id=None,
            node_id=actor["id"],
            login=actor["login"],
            type="EnterpriseUserAccount",
            linked_user_github_id=(actor["user"] or {}).get("databaseId"),
        )
    return Account(
        github_id=actor["databaseId"],
        node_id=actor["id"],
        login=actor["login"],
        type=actor["__typename"],
    )


def wire_review(node: dict[str, Any]) -> Review:
    return Review(
        github_id=int(node["fullDatabaseId"]),
        node_id=node["id"],
        reviewer=wire_account(node["author"]),
        state=node["state"],
        created_at=datetime.fromisoformat(node["createdAt"]),
        submitted_at=wire_time(node["submittedAt"]),
        published_at=wire_time(node["publishedAt"]),
        updated_at=datetime.fromisoformat(node["updatedAt"]),
        last_edited_at=wire_time(node["lastEditedAt"]),
        editor=wire_account(node["editor"]),
        includes_created_edit=node["includesCreatedEdit"],
        commit_oid=(node["commit"] or {}).get("oid"),
        author_association=node["authorAssociation"],
        author_can_push_to_repository=node["authorCanPushToRepository"],
        inline_comments_total=node["comments"]["totalCount"],
    )


def wire_comment(node: dict[str, Any]) -> Comment:
    return Comment(
        github_id=int(node["fullDatabaseId"]),
        node_id=node["id"],
        author=wire_account(node["author"]),
        created_at=datetime.fromisoformat(node["createdAt"]),
        published_at=wire_time(node["publishedAt"]),
        updated_at=datetime.fromisoformat(node["updatedAt"]),
        last_edited_at=wire_time(node["lastEditedAt"]),
        editor=wire_account(node["editor"]),
        created_via_email=node["createdViaEmail"],
        includes_created_edit=node["includesCreatedEdit"],
        author_association=node["authorAssociation"],
    )


def wire_request(node: dict[str, Any]) -> ReviewRequest:
    requested = node["requestedReviewer"]
    typename = requested["__typename"] if requested is not None else None
    return ReviewRequest(
        github_id=node["databaseId"],
        node_id=node["id"],
        as_code_owner=node["asCodeOwner"],
        requested_type=typename,
        account=(
            Account(
                github_id=requested["databaseId"],
                node_id=requested["id"],
                login=requested["login"],
                type=typename,
            )
            if typename in ("User", "Bot", "Mannequin")
            else None
        ),
        team=wire_team(requested),
    )


def wire_team(requested: dict[str, Any] | None) -> RequestedTeam | None:
    """A Team by its databaseId; an EnterpriseTeam by its fullDatabaseId, a BigInt string."""
    if requested is None or requested["__typename"] not in ("Team", "EnterpriseTeam"):
        return None
    if requested["__typename"] == "Team":
        github_id = requested["databaseId"]
    else:
        full = requested["fullDatabaseId"]
        github_id = int(full) if full is not None else None
    return RequestedTeam(github_id=github_id, node_id=requested["id"], slug=requested["slug"])


def wire_assignee(node: dict[str, Any]) -> Account:
    return Account(
        github_id=node["databaseId"],
        node_id=node["id"],
        login=node["login"],
        type=node["__typename"],
    )


def wire_ref(node: dict[str, Any] | None, field: str) -> Any:
    return node[field] if node is not None else None


@pytest.mark.parametrize(
    "unit",
    recorded_pull_requests(),
    ids=lambda unit: f"{unit.key['name']}#{unit.key['number']}",
)
async def test_every_field_maps_from_its_wire_name(open_source, unit: Unit) -> None:
    """Each record field against the recorded JSON it comes from, read by its wire name."""
    data = unit.data
    record = await hydrate_one(open_source, (unit.key["owner"], unit.key["name"]), data["number"])
    head_repository = data["headRepository"]
    owner = data["headRepositoryOwner"]
    stack = data["stack"]
    entry = data["stackEntry"]
    milestone = data["milestone"]
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
        record.closed_at,
        record.published_at,
        record.last_edited_at,
        record.editor,
        record.includes_created_edit,
        record.maintainer_can_modify,
        record.merged_by,
        record.base_ref,
        record.base_ref_oid,
        record.head_ref_name,
        record.head_ref_oid,
        record.head_repository,
        record.head_repository_owner,
        record.merge_commit_oid,
        record.is_cross_repository,
        record.milestone,
        record.review_decision,
        record.mergeable,
        record.merge_state_status,
        record.can_be_rebased,
        record.potential_merge_commit_oid,
        record.stack,
        record.stack_entry,
        record.additions,
        record.deletions,
        record.changed_files,
        record.total_comments_count,
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
        wire_time(data["closedAt"]),
        wire_time(data["publishedAt"]),
        wire_time(data["lastEditedAt"]),
        wire_account(data["editor"]),
        data["includesCreatedEdit"],
        data["maintainerCanModify"],
        wire_account(data["mergedBy"]),
        data["baseRefName"],
        data["baseRefOid"],
        data["headRefName"],
        data["headRefOid"],
        (
            HeadRepository(
                github_id=head_repository["databaseId"],
                full_name=head_repository["nameWithOwner"],
            )
            if head_repository is not None
            else None
        ),
        HeadRepositoryOwner(node_id=owner["id"], login=owner["login"]) if owner else None,
        wire_ref(data["mergeCommit"], "oid"),
        data["isCrossRepository"],
        Milestone(number=milestone["number"], title=milestone["title"]) if milestone else None,
        data["reviewDecision"],
        data["mergeable"],
        data["mergeStateStatus"],
        data["canBeRebased"],
        wire_ref(data["potentialMergeCommit"], "oid"),
        (
            Stack(
                node_id=stack["id"],
                number=stack["number"],
                size=stack["size"],
                base_ref_name=stack["baseRefName"],
            )
            if stack is not None
            else None
        ),
        StackEntry(node_id=entry["id"], position=entry["position"]) if entry else None,
        data["additions"],
        data["deletions"],
        data["changedFiles"],
        data["totalCommentsCount"],
        wire_time(data["updatedAt"]),
    )
    assert record.totals == ConnectionTotals(
        labels=data["labels"]["totalCount"],
        closing_issues=data["closingIssuesReferences"]["totalCount"],
        user_linked_closing_issues=data["userLinkedClosingIssues"]["totalCount"],
        reviews=data["reviews"]["totalCount"],
        comments=data["comments"]["totalCount"],
        commits=data["commits"]["totalCount"],
        files=data["files"]["totalCount"],
        review_requests=data["reviewRequests"]["totalCount"],
        assignees=data["assignedActors"]["totalCount"],
    )
    # The first page of each connection leads the record's entries, in order.
    labels = [Label(name=node["name"], node_id=node["id"]) for node in data["labels"]["nodes"]]
    assert list(record.labels[: len(labels)]) == labels
    user_linked = {
        (node["repository"]["databaseId"], node["number"])
        for node in data["userLinkedClosingIssues"]["nodes"]
    }
    closing = [
        LinkedClosingIssue(
            repo_github_id=node["repository"]["databaseId"],
            repo_full_name=node["repository"]["nameWithOwner"],
            number=node["number"],
            github_id=int(node["fullDatabaseId"]),
            user_linked=(node["repository"]["databaseId"], node["number"]) in user_linked,
        )
        for node in data["closingIssuesReferences"]["nodes"]
    ]
    assert list(record.closing_issues[: len(closing)]) == closing
    reviews = [wire_review(node) for node in data["reviews"]["nodes"]]
    assert list(record.reviews[: len(reviews)]) == reviews
    comments = [wire_comment(node) for node in data["comments"]["nodes"]]
    assert list(record.comments[: len(comments)]) == comments
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
    requests = [wire_request(node) for node in data["reviewRequests"]["nodes"]]
    assert list(record.review_requests[: len(requests)]) == requests
    assignees = [wire_assignee(node) for node in data["assignedActors"]["nodes"]]
    assert list(record.assignees[: len(assignees)]) == assignees
    if not (
        data["reviews"]["pageInfo"]["hasNextPage"] or data["comments"]["pageInfo"]["hasNextPage"]
    ):
        sightings = [data["author"], data["mergedBy"], data["editor"]]
        for node in data["reviews"]["nodes"] + data["comments"]["nodes"]:
            sightings += [node["author"], node["editor"]]
        expected = {wire_account(actor) for actor in sightings if actor is not None}
        expected |= {request.account for request in requests if request.account is not None}
        expected |= set(assignees)
        assert set(record.accounts()) == expected
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


def test_the_recordings_hold_a_value_for_each_added_field() -> None:
    """The mapping test above reads a non-null, non-false value of each Step 3b-1 field from
    some recording, so a field mapped to the wrong record field cannot pass on nulls alone."""
    units = [unit.data for unit in recorded_pull_requests()]
    reviews = [node for data in units for node in data["reviews"]["nodes"]]
    comments = [node for data in units for node in data["comments"]["nodes"]]
    filled = {
        "closedAt": any(data["state"] == "CLOSED" and data["closedAt"] for data in units),
        "lastEditedAt": any(data["lastEditedAt"] for data in units),
        "editor": any(data["editor"] for data in units),
        "headRepository null": any(data["headRepository"] is None for data in units),
        "milestone": any(data["milestone"] for data in units),
        "reviewDecision": any(data["reviewDecision"] for data in units),
        "potentialMergeCommit": any(data["potentialMergeCommit"] for data in units),
        "mergeCommit": any(data["mergeCommit"] for data in units),
        "maintainerCanModify": any(data["maintainerCanModify"] for data in units),
        "user-linked": any(data["userLinkedClosingIssues"]["nodes"] for data in units),
        "code-owner request": any(
            node["asCodeOwner"] for data in units for node in data["reviewRequests"]["nodes"]
        ),
        "Bot assignee": any(
            node["__typename"] == "Bot"
            for data in units
            for node in data["assignedActors"]["nodes"]
        ),
        "inline comments": any(node["comments"]["totalCount"] for node in reviews),
        "review editor": any(node["editor"] for node in reviews),
        "review commit": any(node["commit"] for node in reviews),
        "comment editor": any(node["editor"] for node in comments),
        "createdViaEmail": any(node["createdViaEmail"] for node in comments),
    }
    assert [name for name, present in filled.items() if not present] == []


def wire_link(git_actor: dict[str, Any] | None) -> LinkedAccount | None:
    user = (git_actor or {}).get("user")
    if user is None:
        return None
    return LinkedAccount(github_id=user["databaseId"], node_id=user["id"], login=user["login"])


# --------------------------------------------------------------------------------------------
# Synthetic overflow for labels and closing issues (no tracked PR has more than 100)
# --------------------------------------------------------------------------------------------
def overflow_unit(
    item: Unit,
    connection: str,
    after: str,
    nodes: list[Any],
    total: int,
    next_cursor: str | None = None,
) -> Unit:
    """A synthetic continuation page; ``next_cursor`` gives it a further page."""
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
                "pageInfo": {
                    "hasNextPage": next_cursor is not None,
                    "endCursor": next_cursor or after,
                },
                "nodes": nodes,
            },
        },
        source=f"synthetic: {connection} after {after} for recorded unit #{item.key['number']}",
    )


SECOND_PAGES: dict[str, dict[str, Any]] = {
    "labels": {"id": "LA_extra", "name": "extra"},
    "closingIssuesReferences": {
        "fullDatabaseId": "1000001",
        "number": 1,
        "repository": {"databaseId": 39321023, "nameWithOwner": "prebid/Prebid.js"},
    },
}


def with_second_pages(replay: Replay, unit: Unit, connections: tuple[str, ...]) -> Unit:
    """``unit`` (one recorded entry on each connection) with a second page on ``connections``."""
    for connection in connections:
        page = unit.data[connection]
        page["totalCount"] += 1
        page["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(
            overflow_unit(
                unit, connection, "page-2", [SECOND_PAGES[connection]], page["totalCount"]
            )
        )
    replay.add(unit)
    return unit


@pytest.mark.parametrize(
    ("connection", "node", "read"),
    [
        ("labels", SECOND_PAGES["labels"], lambda record: record.labels),
        (
            "closingIssuesReferences",
            SECOND_PAGES["closingIssuesReferences"],
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


async def test_a_connection_with_three_pages(open_source, replay: Replay) -> None:
    unit = pr_unit(replay, 12975)  # one label, recorded
    labels = unit.data["labels"]
    labels["totalCount"] = 3
    labels["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
    replay.add(unit)
    replay.add(
        overflow_unit(
            unit, "labels", "page-2", [{"id": "LA_b", "name": "b"}], 3, next_cursor="page-3"
        )
    )
    replay.add(overflow_unit(unit, "labels", "page-3", [{"id": "LA_c", "name": "c"}], 3))
    record = await hydrate_one(open_source, PREBID_JS, 12975)
    assert [label.name for label in record.labels] == ["bugfix", "b", "c"]
    overflow = [seen for seen in replay.seen if is_overflow(seen)]
    assert [seen.variables["after0"] for seen in overflow] == ["page-2", "page-3"]


# --------------------------------------------------------------------------------------------
# The connections and fields Step 3b-1 adds
# --------------------------------------------------------------------------------------------
REQUESTED_TEAM = {
    "id": "RR_team",
    "databaseId": 77,
    "asCodeOwner": True,
    "requestedReviewer": {
        "__typename": "Team",
        "id": "T_kwDOAAAAAQ",
        "databaseId": 5,
        "slug": "core",
    },
}
EXTRA_ASSIGNEE = {
    "__typename": "Organization",
    "databaseId": 13242760,
    "id": "MDEyOk9yZ2FuaXphdGlvbjEzMjQyNzYw",
    "login": "prebid",
}


def second_page(replay: Replay, unit: Unit, key: str, node: dict[str, Any]) -> None:
    """``key`` of ``unit`` grows by ``node`` on a synthetic second page."""
    page = unit.data[key]
    page["totalCount"] += 1
    page["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
    replay.add(overflow_unit(unit, key, "page-2", [node], page["totalCount"]))


@pytest.mark.parametrize(
    ("key", "node", "read", "expected"),
    [
        (
            "reviewRequests",
            REQUESTED_TEAM,
            lambda record: record.review_requests[-1].team,
            RequestedTeam(github_id=5, node_id="T_kwDOAAAAAQ", slug="core"),
        ),
        (
            "assignedActors",
            EXTRA_ASSIGNEE,
            lambda record: record.assignees[-1],
            Account(
                github_id=13242760,
                node_id="MDEyOk9yZ2FuaXphdGlvbjEzMjQyNzYw",
                login="prebid",
                type="Organization",
            ),
        ),
    ],
    ids=["reviewRequests", "assignedActors"],
)
async def test_overflow_on_review_requests_and_assignees(
    open_source, replay: Replay, key: str, node: dict[str, Any], read, expected: object
) -> None:
    # Synthetic: recorded #15771 (one request, two assignees) with a second page.
    unit = pr_unit(replay, 15771)
    second_page(replay, unit, key, node)
    replay.add(unit)
    record = await hydrate_one(open_source, PREBID_JS, 15771)
    assert read(record) == expected
    assert (
        getattr(record.totals, {"reviewRequests": "review_requests"}.get(key, "assignees"))
        == (unit.data[key]["totalCount"])
    )
    assert [seen.operation for seen in replay.seen].count("Overflow") == 1


async def test_overflow_on_user_linked_closing_issues(open_source, replay: Replay) -> None:
    # Synthetic: recorded #15753 (one link, user-linked) with a second link on both lists.
    unit = pr_unit(replay, 15753)
    repository = {"databaseId": 39321023, "nameWithOwner": "prebid/Prebid.js"}
    second_page(
        replay,
        unit,
        "closingIssuesReferences",
        {"fullDatabaseId": "1000001", "number": 1, "repository": repository},
    )
    second_page(
        replay,
        unit,
        "userLinkedClosingIssues",
        {"number": 1, "repository": {"databaseId": 39321023}},
    )
    replay.add(unit)
    record = await hydrate_one(open_source, PREBID_JS, 15753)
    assert [(link.number, link.user_linked) for link in record.closing_issues] == [
        (15750, True),
        (1, True),
    ]
    assert record.totals.user_linked_closing_issues == 2


async def test_a_link_not_user_linked(open_source, replay: Replay) -> None:
    # Synthetic: recorded #15753 with its user-linked list emptied.
    unit = pr_unit(replay, 15753)
    unit.data["userLinkedClosingIssues"] = {
        "totalCount": 0,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [],
    }
    replay.add(unit)
    record = await hydrate_one(open_source, PREBID_JS, 15753)
    assert [(link.number, link.user_linked) for link in record.closing_issues] == [(15750, False)]


async def test_a_user_linked_issue_absent_from_the_full_list(open_source, replay: Replay) -> None:
    unit = pr_unit(replay, 15753)
    unit.data["userLinkedClosingIssues"]["nodes"][0]["number"] = 99999
    replay.add(unit)
    error = await failure(open_source, PREBID_JS, 15753)
    assert isinstance(error, UserLinkedIssueNotListed)
    assert error.key == (39321023, 99999)


@pytest.mark.parametrize("key", ["userLinkedClosingIssues", "reviewRequests", "assignedActors"])
async def test_a_short_page_on_an_added_connection(open_source, replay: Replay, key: str) -> None:
    # The last page ends with fewer entries than totalCount.
    unit = pr_unit(replay, 15753 if key == "userLinkedClosingIssues" else 15771)
    unit.data[key]["totalCount"] += 1
    replay.add(unit)
    error = await failure(open_source, PREBID_JS, unit.key["number"])
    assert isinstance(error, ConnectionCountMismatch)
    assert error.connection == key


async def test_a_duplicate_assignee(open_source, replay: Replay) -> None:
    unit = pr_unit(replay, 15771)
    nodes = unit.data["assignedActors"]["nodes"]
    nodes[1] = dict(nodes[0])
    replay.add(unit)
    error = await failure(open_source, PREBID_JS, 15771)
    assert isinstance(error, DuplicateEntry)
    assert error.connection == "assignedActors"


async def test_a_duplicate_review_request(open_source, replay: Replay) -> None:
    # A request listed again in place of a missing one would pass the count check.
    unit = pr_unit(replay, 15771)
    page = unit.data["reviewRequests"]
    page["nodes"].append(dict(page["nodes"][0]))
    page["totalCount"] = 2
    replay.add(unit)
    error = await failure(open_source, PREBID_JS, 15771)
    assert isinstance(error, DuplicateEntry)
    assert error.connection == "reviewRequests"


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
async def test_a_closed_or_merged_pull_request_without_a_close_time(
    open_source, replay: Replay, state: str
) -> None:
    # The coverage rule reads closedAt for a closed pull request (plan section 3).
    unit = pr_unit(replay, 15771 if state == "CLOSED" else 13481)
    assert unit.data["state"] == state and unit.data["closedAt"] is not None
    unit.data["closedAt"] = None
    replay.add(unit)
    error = await failure(open_source, PREBID_JS, unit.key["number"])
    assert isinstance(error, CloseTimeMissing)
    assert error.state == state


async def test_an_open_pull_request_needs_no_close_time(open_source, replay: Replay) -> None:
    record = await hydrate_one(open_source, ("prebid", "salesagent"), 2360)
    assert (record.state, record.closed_at) == ("OPEN", None)


@pytest.mark.parametrize(
    ("recorded", "edit"),
    [
        (
            13481,
            lambda data: (
                data.__setitem__("publishedAt", "2025-07-01T15:00:00Z"),
                data["reviews"]["nodes"][0].__setitem__("publishedAt", "2025-07-01T16:00:00Z"),
                data["reviews"]["nodes"][0].__setitem__("updatedAt", "2025-07-01T17:00:00Z"),
            ),
        ),
        (
            12975,
            lambda data: (
                data.__setitem__("publishedAt", "2025-03-01T09:00:00Z"),
                data["reviews"]["nodes"][0].__setitem__("publishedAt", "2025-03-01T09:30:00Z"),
                data["reviews"]["nodes"][0].__setitem__("updatedAt", "2025-03-01T09:45:00Z"),
                data["comments"]["nodes"][0].__setitem__("publishedAt", "2025-03-01T10:00:00Z"),
                data["comments"]["nodes"][0].__setitem__("updatedAt", "2025-03-01T11:00:00Z"),
                data["comments"]["nodes"][0].__setitem__("lastEditedAt", "2025-03-01T12:00:00Z"),
            ),
        ),
    ],
    ids=["pull-request-and-review-times", "comment-times"],
)
async def test_fields_equal_in_every_recording_map_apart(
    open_source, replay: Replay, recorded: int, edit
) -> None:
    """publishedAt equals createdAt in every recorded pull request, review, and comment, so the
    mapping test alone cannot tell them apart; here each time differs from its siblings."""
    unit = pr_unit(replay, recorded)
    edit(unit.data)
    unit.source = f"synthetic: recorded #{recorded} with times that differ from createdAt"
    replay.add(unit)
    for node in (
        unit.data,
        *unit.data["reviews"]["nodes"][:1],
        *unit.data["comments"]["nodes"][:1],
    ):
        times = [node.get(name) for name in ("createdAt", "publishedAt", "updatedAt")]
        assert len(set(times)) == 3, times
    await test_every_field_maps_from_its_wire_name(open_source, unit)


async def test_a_duplicate_user_linked_issue(open_source, replay: Replay) -> None:
    unit = pr_unit(replay, 15753)
    page = unit.data["userLinkedClosingIssues"]
    page["nodes"].append(dict(page["nodes"][0]))
    page["totalCount"] = 2
    replay.add(unit)
    error = await failure(open_source, PREBID_JS, 15753)
    assert isinstance(error, DuplicateEntry)
    assert error.connection == "userLinkedClosingIssues"


def review_requests_of_each_kind(replay: Replay) -> Unit:
    """Recorded #15771's user request, then a team, an enterprise team, and a request GitHub
    returns with no reviewer (synthetic), added to the replay."""
    unit = pr_unit(replay, 15771)
    user_request = unit.data["reviewRequests"]["nodes"][0]
    unit.data["reviewRequests"]["nodes"] = [
        user_request,
        REQUESTED_TEAM,
        {
            **REQUESTED_TEAM,
            "id": "RR_enterprise",
            "requestedReviewer": {
                "__typename": "EnterpriseTeam",
                "id": "ET_kwDOAAAAAQ",
                "fullDatabaseId": "9000000001",
                "slug": "enterprise-reviewers",
            },
        },
        {**REQUESTED_TEAM, "id": "RR_none", "databaseId": None, "requestedReviewer": None},
    ]
    unit.data["reviewRequests"]["totalCount"] = 4
    replay.add(unit)
    return unit


async def test_review_requests_of_each_kind(open_source, replay: Replay) -> None:
    unit = review_requests_of_each_kind(replay)
    await test_every_field_maps_from_its_wire_name(open_source, unit)
    record = await hydrate_one(open_source, PREBID_JS, 15771)
    assert [
        (request.requested_type, request.account is not None, request.team, request.github_id)
        for request in record.review_requests
    ] == [
        ("User", True, None, 3843805356),
        ("Team", False, RequestedTeam(github_id=5, node_id="T_kwDOAAAAAQ", slug="core"), 77),
        (
            "EnterpriseTeam",
            False,
            RequestedTeam(
                github_id=9000000001, node_id="ET_kwDOAAAAAQ", slug="enterprise-reviewers"
            ),
            77,
        ),
        (None, False, None, None),
    ]
    # A requested account is an actor sighting; a team or an enterprise team is not an account.
    assert record.review_requests[0].account in record.accounts()


def enterprise_team_request(unit: Unit) -> dict[str, Any]:
    (request,) = [
        node
        for node in unit.data["reviewRequests"]["nodes"]
        if (node["requestedReviewer"] or {}).get("__typename") == "EnterpriseTeam"
    ]
    return request["requestedReviewer"]


async def test_an_enterprise_team_without_a_full_database_id(open_source, replay: Replay) -> None:
    # fullDatabaseId is nullable in GitHub's schema; the team is still told by its node id.
    unit = review_requests_of_each_kind(replay)
    enterprise_team_request(unit)["fullDatabaseId"] = None
    replay.add(unit)
    await test_every_field_maps_from_its_wire_name(open_source, unit)
    record = await hydrate_one(open_source, PREBID_JS, 15771)
    assert record.review_requests[2].team == RequestedTeam(
        github_id=None, node_id="ET_kwDOAAAAAQ", slug="enterprise-reviewers"
    )


async def test_an_enterprise_team_id_that_is_not_a_big_int_fails_the_item(
    open_source, replay: Replay
) -> None:
    # A BigInt arrives as a string of digits; a JSON number is a shape GitHub does not send.
    unit = review_requests_of_each_kind(replay)
    enterprise_team_request(unit)["fullDatabaseId"] = 9000000001
    replay.add(unit)
    error = await failure(open_source, PREBID_JS, 15771)
    assert isinstance(error, ResponseShapeError)
    assert "expected a BigInt, a string of digits: 9000000001" in str(error)


async def test_a_stack_and_its_entry(open_source, replay: Replay) -> None:
    # Synthetic: no tracked pull request is in a stack (section 7, "Merge state and stacks").
    unit = pr_unit(replay, 13481)
    unit.data["stack"] = {"id": "PRS_1", "number": 3, "size": 2, "baseRefName": "master"}
    unit.data["stackEntry"] = {"id": "PRSE_1", "position": 1}
    replay.add(unit)
    record = await hydrate_one(open_source, PREBID_JS, 13481)
    assert record.stack == Stack(node_id="PRS_1", number=3, size=2, base_ref_name="master")
    assert record.stack_entry == StackEntry(node_id="PRSE_1", position=1)


async def test_a_closed_pull_request_and_its_close_time(open_source, replay: Replay) -> None:
    record = await hydrate_one(open_source, PREBID_JS, 15771)
    assert (record.state, record.merged_at) == ("CLOSED", None)
    assert record.closed_at == datetime(2026, 10, 16, 10, 26, 1, tzinfo=UTC)


async def test_an_editor_is_a_sighting(open_source, replay: Replay) -> None:
    record = await hydrate_one(open_source, PREBID_JS, 15771)
    assert record.editor is not None and record.editor.type == "Bot"
    assert record.editor in record.accounts()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("closedAt",), "yesterday"),
        (("maintainerCanModify",), "true"),
        (("totalCommentsCount",), "3"),
        (("mergeable",), 1),
        (("headRepositoryOwner", "login"), 7),
        (("stack",), "PRS_1"),
        (("assignedActors", "nodes", 0, "__typename"), "Team"),
        (("assignedActors", "nodes", 0, "databaseId"), "1683175"),
        (("reviewRequests", "nodes", 0, "requestedReviewer", "__typename"), "Organization"),
        (("reviewRequests", "nodes", 0, "asCodeOwner"), None),
        (("labels", "nodes", 0, "id"), None),
    ],
    ids=[
        "date-string",
        "string-bool",
        "string-count",
        "int-enum",
        "int-login",
        "string-stack",
        "team-assignee",
        "string-assignee-id",
        "organization-reviewer",
        "null-code-owner",
        "null-label-id",
    ],
)
async def test_an_added_field_of_the_wrong_type(
    open_source, replay: Replay, path: tuple[Any, ...], value: Any
) -> None:
    unit = pr_unit(replay, 15771)
    if path[0] == "labels":
        unit = pr_unit(replay, 13481)
    target = unit.data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    replay.add(unit)
    assert isinstance(await failure(open_source, PREBID_JS, unit.key["number"]), ResponseShapeError)


@pytest.mark.parametrize(("total", "compare"), [(250, False), (251, True)])
def test_compare_starts_above_250_commits(total: int, compare: bool) -> None:
    assert _uses_compare(total) is compare


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
        [
            "labels",
            "closingIssuesReferences",
            "userLinkedClosingIssues",
            "reviews",
            "comments",
            "commits",
            "files",
            "reviewRequests",
            "assignedActors",
        ],
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

    @pytest.mark.parametrize(
        ("user", "linked"), [({"databaseId": 81847}, 81847), (None, None)], ids=["user", "no-user"]
    )
    async def test_an_enterprise_actor_is_kept_as_an_account(
        self, open_source, replay: Replay, user: dict[str, int] | None, linked: int | None
    ) -> None:
        # Synthetic: the recorded author of #13481 replaced by an EnterpriseUserAccount, which
        # has no databaseId; 6fedb05 failed the item (gap 85).
        unit = pr_unit(replay, 13481)
        unit.data["author"] = {
            "__typename": "EnterpriseUserAccount",
            "login": "someone_corp",
            "id": "EUA_kgDOAAAAAQ",
            "user": user,
        }
        replay.add(unit)
        record = await hydrate_one(open_source, PREBID_JS, 13481)
        expected = Account(
            github_id=None,
            node_id="EUA_kgDOAAAAAQ",
            login="someone_corp",
            type="EnterpriseUserAccount",
            linked_user_github_id=linked,
        )
        assert record.author == expected
        assert expected in record.accounts()

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("reviews", "nodes", 0, "state"), "PENDING"),
            (("isDraft",), "no"),
            (("fullDatabaseId",), "1_2"),
            (("fullDatabaseId",), True),
            (("createdAt",), 1_700_000_000),
            (("createdAt",), "1700000000"),
            (("createdAt",), "2025-07-01 14:51:49Z"),
            (("author", "__typename"), "Ghost"),
            (("state",), "QUEUED"),
            (("additions",), "141"),
            (("unexpectedField",), 1),
        ],
        ids=[
            "pending-review",
            "string-bool",
            "bigint-with-underscore",
            "bigint-as-bool",
            "numeric-date",
            "digit-string-date",
            "space-separated-date",
            "unknown-actor",
            "unknown-state",
            "string-int",
            "unselected-field",
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

    async def test_a_not_found_below_a_null_alias_is_not_gone(
        self, open_source, replay: Replay
    ) -> None:
        # Gone needs NOT_FOUND on the pull request itself; one deeper down nulled the alias.
        unit = pr_unit(replay, 13481)
        unit.data = None
        unit.errors = [{"type": "NOT_FOUND", "path": ["commits", "nodes", 0], "message": "x"}]
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, AliasError)
        assert error.error_type == "NOT_FOUND"

    @pytest.mark.parametrize(
        "suffix",
        [[None], [{}], [1.5], [0.0], [True], [-1]],
        ids=["null", "object", "float", "integral-float", "bool", "negative"],
    )
    async def test_a_not_found_at_a_malformed_path_is_not_gone(
        self, open_source, replay: Replay, clock: FakeClock, suffix: list[Any]
    ) -> None:
        # ["repository", "p0", null] must not be read as ["repository", "p0"], which means Gone.
        unit = pr_unit(replay, 13481)
        unit.data = None
        unit.errors = [{"type": "NOT_FOUND", "path": suffix, "message": "Missing resource"}]
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, ItemRequestFailed)
        assert isinstance(error.cause, ServerError)
        assert "malformed" in str(error.cause)
        # The transport retried the malformed answer; the item's own request is not repeated.
        assert batches(replay) == [[13481]] * 4

    @pytest.mark.parametrize(
        ("state", "merged_at"),
        [("MERGED", None), ("OPEN", "2025-07-01T14:55:24Z"), ("CLOSED", "2025-07-01T14:55:24Z")],
        ids=["merged-without-a-time", "open-with-a-time", "closed-with-a-time"],
    )
    async def test_state_and_merge_time_must_agree(
        self, open_source, replay: Replay, state: str, merged_at: str | None
    ) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["state"] = state
        unit.data["mergedAt"] = merged_at
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, MergeStateMismatch)
        assert error.state == state

    async def test_a_first_page_with_a_next_page_but_no_entries(
        self, open_source, replay: Replay
    ) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["files"]["nodes"] = []
        unit.data["files"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, PagingStalled)
        assert (error.connection, error.entries) == ("files", 0)

    async def test_a_continuation_with_a_next_page_but_no_entries(
        self, open_source, replay: Replay
    ) -> None:
        # A fresh cursor each time but no entries would be followed forever.
        def empty(body: dict[str, Any]) -> None:
            page = body["data"][overflow_alias(0, COMMITS)]["commits"]
            page["nodes"] = []
            page["pageInfo"] = {"hasNextPage": True, "endCursor": "fresh"}

        replay.modify(empty, when=is_overflow, times=2)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, PagingStalled)
        assert (error.cursor, error.entries) == ("fresh", 0)
        assert "0 entries, cursor 'fresh'" in str(error)

    async def test_an_error_outside_every_alias_fails_the_items(
        self, open_source, replay: Replay
    ) -> None:
        # Nothing ties it to one item, so the request fails: split to single items, each of which
        # fails, gets its solo retry (no transport retry preceded it), and fails again.
        def stray(body: dict[str, Any]) -> None:
            body["errors"] = [{"type": "INTERNAL", "path": ["repository"], "message": "partial"}]

        replay.modify(stray, when=is_batch, times=None)
        outcomes = await hydrate(open_source, PREBID_JS, [13481, 15724])
        for outcome in outcomes:
            assert isinstance(outcome, Failed)
            assert isinstance(outcome.error, ItemRequestFailed)
            assert isinstance(outcome.error.cause, UnattributedErrors)
        assert batches(replay) == [[13481, 15724], [13481], [15724], [13481], [15724]]

    async def test_a_one_off_error_outside_a_lone_continuation_is_retried(
        self, open_source, replay: Replay
    ) -> None:
        # 11155's continuation is a one-alias request; the solo retry reads it again.
        def stray(body: dict[str, Any]) -> None:
            body["errors"] = [{"type": "INTERNAL", "path": ["elsewhere"], "message": "partial"}]

        replay.modify(stray, when=is_overflow)
        outcomes = await hydrate(open_source, PREBID_JS, [11155])
        assert isinstance(outcomes[0], Hydrated)
        assert batches(replay) == [[11155], [11155]]

    async def test_an_error_outside_every_continuation_fails_the_item(
        self, open_source, replay: Replay
    ) -> None:
        def stray(body: dict[str, Any]) -> None:
            body["errors"] = [{"type": "INTERNAL", "path": ["elsewhere"], "message": "partial"}]

        replay.modify(stray, when=is_overflow, times=None)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, ItemRequestFailed)
        assert isinstance(error.cause, UnattributedErrors)

    async def test_a_not_found_beside_another_error_is_not_gone(
        self, open_source, replay: Replay
    ) -> None:
        # Gone needs every error under the alias to be NOT_FOUND on the pull request itself.
        unit = pr_unit(replay, 13481)
        unit.data = None
        unit.errors = [
            {"type": "NOT_FOUND", "path": [], "message": "gone"},
            {"type": "FORBIDDEN", "path": ["author"], "message": "no"},
        ]
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, AliasError)

    async def test_a_null_alias_without_errors(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.data = None
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, AliasError)
        assert error.error_type is None

    async def test_a_continuation_without_nodes(self, open_source, replay: Replay) -> None:
        unit = replay.unit("overflow", **COMMITS_PAGE_2)
        unit.data["commits"]["nodes"] = None
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 11155)
        assert isinstance(error, NullEntry)
        assert error.connection == "commits"

    async def test_a_continuation_the_models_reject(self, open_source, replay: Replay) -> None:
        unit = replay.unit("overflow", **COMMITS_PAGE_2)
        unit.data["headRefOid"] = None
        replay.add(unit)
        assert isinstance(await failure(open_source, PREBID_JS, 11155), ResponseShapeError)

    async def test_a_later_continuation_does_not_clear_an_earlier_failure(
        self, open_source, replay: Replay
    ) -> None:
        # Both continuations of one item share a request; the first fails once, the second
        # succeeds. The item must fail in its batch and pass on its solo retry.
        with_second_pages(replay, pr_unit(replay, 12975), ("labels", "closingIssuesReferences"))

        def moved(body: dict[str, Any]) -> None:
            body["data"]["o0_labels"]["headRefOid"] = "0" * 40

        replay.modify(moved, when=is_overflow)
        outcomes = await hydrate(open_source, PREBID_JS, [12975])
        assert isinstance(outcomes[0], Hydrated)
        assert batches(replay) == [[12975], [12975]]

    async def test_a_failed_item_reads_no_compare(self, open_source, replay: Replay) -> None:
        unit = with_second_pages(replay, pr_unit(replay, 8858), ("labels",))
        page = replay.unit("overflow", id=unit.data["id"], connection="labels", after="page-2")
        page.data["headRefOid"] = "0" * 40
        replay.add(page)
        error = await failure(open_source, PREBID_JS, 8858)
        assert isinstance(error, ItemChangedDuringRead)
        assert "rest" not in [seen.operation for seen in replay.seen]

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

    async def test_each_comment_keeps_its_id_and_author(self, open_source, replay: Replay) -> None:
        # One account under two logins (a rename between comments) stays two sightings of one id.
        unit = pr_unit(replay, 12975)
        recorded = unit.data["comments"]["nodes"][0]
        author = recorded["author"]
        renamed = {**author, "login": "dgirardi-renamed"}
        unit.data["comments"]["nodes"] = [
            {**recorded, "id": "IC_11", "fullDatabaseId": "11", "author": author},
            {**recorded, "id": "IC_12", "fullDatabaseId": "12", "author": None},
            {**recorded, "id": "IC_13", "fullDatabaseId": "13", "author": renamed},
        ]
        unit.data["comments"]["totalCount"] = 3
        replay.add(unit)
        record = await hydrate_one(open_source, PREBID_JS, 12975)
        assert [(comment.github_id, comment.author) for comment in record.comments] == [
            (11, wire_account(author)),
            (12, None),
            (13, wire_account(renamed)),
        ]

    async def test_a_closing_link_renamed_between_pages_is_still_one_link(
        self, open_source, replay: Replay
    ) -> None:
        # The database keys a link by repository id and number; a rename changes only the name.
        unit = pr_unit(replay, 12975)
        page = unit.data["closingIssuesReferences"]
        link = page["nodes"][0]
        renamed = {**link, "repository": {**link["repository"], "nameWithOwner": "prebid/renamed"}}
        page["nodes"].append(renamed)
        page["totalCount"] += 1
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 12975)
        assert isinstance(error, DuplicateEntry)
        assert error.key == (link["repository"]["databaseId"], link["number"])

    @pytest.mark.parametrize(
        "connection", ["labels", "closingIssuesReferences", "reviews", "comments", "commits"]
    )
    async def test_an_entry_listed_twice_fails_the_item(
        self, open_source, replay: Replay, connection: str
    ) -> None:
        # The count still matches: only the key check sees the duplicate.
        unit = pr_unit(replay, 12975)
        page = unit.data[connection]
        page["nodes"].append(dict(page["nodes"][0]))
        page["totalCount"] += 1
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 12975)
        assert isinstance(error, DuplicateEntry)
        assert error.connection == connection


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

    async def test_a_failing_compare_request_fails_the_item(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(
            lambda request: synthetic("server_error"),
            when=lambda s: s.operation == "rest",
            times=None,
        )
        error = await failure(open_source, PREBID_JS, 8858)
        assert isinstance(error, ItemRequestFailed)
        # One compare request and its retries; the item's own request is not retried again.
        assert [seen.operation for seen in replay.seen].count("rest") == 4
        assert clock.sleeps == list(SERVER_RETRY_DELAYS)

    async def test_a_compare_commit_listed_twice_fails_the_item(
        self, open_source, replay: Replay
    ) -> None:
        first = self.compare_page(replay, 1).data["commits"][0]
        unit = self.compare_page(replay, 3)
        unit.data["commits"][-1] = first
        replay.add(unit)
        error = await failure(open_source, PREBID_JS, 8858)
        assert isinstance(error, DuplicateEntry)
        assert (error.connection, error.key) == ("commits", first["sha"])

    async def test_a_compare_page_the_models_reject(self, open_source, replay: Replay) -> None:
        unit = self.compare_page(replay, 1)
        unit.data["total_commits"] = "254"
        replay.add(unit)
        assert isinstance(await failure(open_source, PREBID_JS, 8858), ResponseShapeError)

    async def test_compare_commits_map_from_their_wire_names(
        self, open_source, replay: Replay
    ) -> None:
        pages = sorted(
            (unit for unit in replay.units.values() if unit.kind == "compare"),
            key=lambda unit: unit.key["page"],
        )
        expected = [
            CommitRecord(
                sha=commit["sha"],
                author=LinkedAccount(
                    github_id=commit["author"]["id"],
                    node_id=commit["author"]["node_id"],
                    login=commit["author"]["login"],
                )
                if commit["author"]
                else None,
                author_name=commit["commit"]["author"]["name"],
                author_email=commit["commit"]["author"]["email"],
                authored_at=datetime.fromisoformat(commit["commit"]["author"]["date"]),
            )
            for page in pages
            for commit in page.data["commits"]
        ]
        record = await hydrate_one(open_source, PREBID_JS, 8858)
        assert list(record.commits) == expected
        assert any(commit.author is None for commit in expected)
        assert any(commit.author is not None for commit in expected)

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

    async def test_an_item_gone_on_its_solo_retry_is_gone(
        self, open_source, replay: Replay
    ) -> None:
        def fail_13481(body: dict[str, Any]) -> None:
            body["data"]["repository"]["p1"] = None
            body.setdefault("errors", []).append(
                {"type": "INTERNAL", "path": ["repository", "p1"], "message": "flaky"}
            )

        def gone(body: dict[str, Any]) -> None:
            body["data"]["repository"]["p0"] = None
            body.setdefault("errors", []).append(
                {"type": "NOT_FOUND", "path": ["repository", "p0"], "message": "deleted"}
            )

        replay.modify(fail_13481, when=lambda s: is_batch(s) and s.numbers == [15724, 13481])
        replay.modify(gone, when=lambda s: is_batch(s) and s.numbers == [13481])
        outcomes = await hydrate(open_source, PREBID_JS, [15724, 13481])
        assert isinstance(outcomes[0], Hydrated)
        assert outcomes[1] == Gone(13481)

    async def test_an_item_that_fails_twice_is_failed(self, open_source, replay: Replay) -> None:
        unit = pr_unit(replay, 13481)
        unit.data["files"]["totalCount"] = 4
        replay.add(unit)
        outcomes = await hydrate(open_source, PREBID_JS, [15724, 13481])
        assert isinstance(outcomes[0], Hydrated)
        assert isinstance(outcomes[1], Failed)
        assert batches(replay) == [[15724, 13481], [13481]]

    async def test_a_single_item_whose_request_keeps_failing(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        # Alone, a timeout is retried like a server error; the failed request is not repeated
        # by a solo retry.
        replay.fault(lambda request: synthetic("bad_gateway"), when=is_batch, times=None)
        error = await failure(open_source, PREBID_JS, 13481)
        assert isinstance(error, ItemRequestFailed)
        assert isinstance(error.cause, RequestTooHeavy)
        assert batches(replay) == [[13481]] * 4
        assert clock.sleeps == list(SERVER_RETRY_DELAYS)

    async def test_a_single_item_that_recovers_from_a_timeout(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        replay.fault(lambda request: synthetic("bad_gateway"), when=is_batch, times=2)
        outcomes = await hydrate(open_source, PREBID_JS, [13481])
        assert isinstance(outcomes[0], Hydrated)
        assert clock.sleeps == list(SERVER_RETRY_DELAYS[:2])

    async def test_a_resource_limit_with_partial_data_splits(
        self, open_source, replay: Replay, clock: FakeClock
    ) -> None:
        # GitHub documents partial results "along with an error indicating that resource limits
        # were exceeded"; the message and path are constructed.
        def exceeded(body: dict[str, Any]) -> None:
            body["errors"] = [
                {"message": "Resource limits for this query exceeded.", "path": ["repository"]}
            ]

        replay.modify(exceeded, when=lambda s: is_batch(s) and len(s.numbers) > 1, times=None)
        outcomes = await hydrate(open_source, PREBID_JS, [13481, 15724, 12975])
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        assert [len(batch) for batch in batches(replay)] == [3, 1, 2, 1, 1]
        assert clock.sleeps == []

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
        # The continuation was alone and retried by the transport; no solo retry repeats it.
        assert [seen.operation for seen in replay.seen].count("Overflow") == 4
        assert batches(replay) == [[11155, 13481]]

    async def test_a_failing_continuation_request_is_split(
        self, open_source, replay: Replay
    ) -> None:
        unit = pr_unit(replay, 12975)
        unit.data["labels"]["totalCount"] += 1
        unit.data["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "page-2"}
        replay.add(unit)
        replay.add(
            overflow_unit(
                unit,
                "labels",
                "page-2",
                [{"id": "LA_x", "name": "x"}],
                unit.data["labels"]["totalCount"],
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
                unit,
                "labels",
                "page-2",
                [{"id": "LA_x", "name": "x"}],
                unit.data["labels"]["totalCount"],
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
            (
                [{"type": "NOT_FOUND", "path": ["repository", "p0"], "message": "gone"}],
                RepositoryShapeError,
            ),
            ([], RepositoryShapeError),
        ],
        ids=["not-found", "forbidden", "not-found-below", "no-error"],
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
        numbers = [900_001 + i for i in range(items)]
        for number in numbers:
            with_second_pages(replay, copied(replay, 12975, number), tuple(SECOND_PAGES))
        outcomes = await hydrate(open_source, PREBID_JS, numbers)
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes)
        assert overflow_sizes(replay) == [
            OVERFLOW_ALIASES_PER_REQUEST,
            2 * items - OVERFLOW_ALIASES_PER_REQUEST,
        ]

    async def test_a_failed_item_gets_no_more_continuations(
        self, open_source, replay: Replay
    ) -> None:
        # The last item's labels page is in the first request and fails; its closing-issue page
        # would be the only alias of a second request.
        items = OVERFLOW_ALIASES_PER_REQUEST // 2 + 1
        numbers = [900_001 + i for i in range(items)]
        for number in numbers:
            with_second_pages(replay, copied(replay, 12975, number), tuple(SECOND_PAGES))
        page = replay.unit(
            "overflow", id=f"PR_copy_{numbers[-1]}", connection="labels", after="page-2"
        )
        page.data["headRefOid"] = "0" * 40
        replay.add(page)
        outcomes = await hydrate(open_source, PREBID_JS, numbers)
        assert isinstance(outcomes[-1], Failed)
        assert all(isinstance(outcome, Hydrated) for outcome in outcomes[:-1])
        # The first round, then the solo retry's two continuations.
        assert overflow_sizes(replay) == [OVERFLOW_ALIASES_PER_REQUEST, 2]
