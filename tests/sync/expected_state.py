"""The database a sync must leave, computed from the scenario model and from what earlier runs
stored, never from the writer's code (redesign plan Step 3b-2a-2c).

``History`` is the model's own history: after each run it records what the run stored, from the
model as the run read it (no test changes the model during a run) and from what earlier runs
stored. Each run, by plan 5.2 and 5.3:

- plans an in-coverage item that is not stored, whose ``updatedAt`` or state differs from the
  stored one, that is unsettled, that carries a label its repository no longer has, or whose index
  links differ from its stored links (step 3); every other stored item takes ``isDraft`` and
  ``authorAssociation`` from the index in place;
- writes the planned items one batch at a time in queue order, open items first, then by
  ``updatedAt`` descending (steps 3 and 6). Within a batch an actor sighting inserts an account
  not yet stored and clears ``not_found_at``; a commit-linked account neither stored, looked up
  earlier in the run, nor sighted in the batch is looked up. A null actor keeps the reference D28
  keeps;
- refreshes the accounts last refreshed more than 24 hours ago and without ``not_found_at``, and
  those with ``not_found_at`` that a hydrated commit links (gap 90); only lookups change a stored
  login, type, name, or profile;
- deletes the stored items its index no longer lists in coverage (step 7).

Rows hold natural keys where the database holds row ids: a repository, pull request, or account
by its ``github_id``, a run by its position among the test's runs. The stamps the sync takes from
``Date`` headers are compared only as set or not, and the run counters not at all.
"""

from __future__ import annotations

import copy
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from github_activity_db.source import PULL_REQUEST_HYDRATION_VERSION
from tests.source.scenario import PullRequest, Repository, Scenario

Node = dict[str, Any]
Row = dict[str, Any]
Planned = tuple[Repository, PullRequest]

UNSETTLED = timedelta(minutes=10)
REFRESH_AFTER = timedelta(hours=24)
BATCH_SIZE = 25
INDEX_CLOSING_LINKS = 10
REVIEWER_ACCOUNTS = ("User", "Bot", "Mannequin")

# The stamps a run takes from Date headers or its own clock: compared as set or not.
STAMPS = frozenset(
    {
        ("repositories", "resolved_at"),
        ("github_users", "refreshed_at"),
        ("github_users", "not_found_at"),
        ("account_profiles", "refreshed_at"),
        ("pull_requests", "hydrated_at"),
        ("repo_sync_state", "last_index_started_at"),
        ("repo_sync_state", "last_success_at"),
        ("sync_runs", "started_at"),
        ("sync_runs", "deadline_at"),
        ("sync_runs", "finished_at"),
        ("sync_failures", "first_failed_at"),
        ("sync_failures", "last_failed_at"),
    }
)
RUN_COUNTERS = frozenset(
    ("sync_runs", column)
    for column in (
        "graphql_points",
        "rest_calls",
        "items_planned",
        "items_hydrated",
        "items_deleted",
        "failures",
    )
)
# Each table's natural key, which pairs expected and stored rows in a difference; a table not
# listed pairs whole rows.
KEYS: dict[str, tuple[str, ...]] = {
    "repositories": ("github_id",),
    "repo_labels": ("repository_id", "node_id"),
    "github_users": ("node_id",),
    "account_profiles": ("account_id",),
    "pull_requests": ("github_id",),
    "pr_labels": ("pr_id", "name"),
    "pr_reviews": ("github_id",),
    "pr_comments": ("github_id",),
    "pr_commits": ("pr_id", "sha"),
    "pr_files": ("pr_id", "ordinal"),
    "pr_closing_issues": ("pr_id", "issue_repo_github_id", "issue_number"),
    "pr_review_requests": ("pr_id", "ordinal"),
    "pr_assignees": ("pr_id", "account_id"),
    "automation_accounts": ("github_id",),
    "automation_emails": ("email",),
    "sync_runs": ("id",),
    "repo_sync_state": ("repository_id",),
    "sync_pending": ("repository_id", "item_kind", "number"),
    "sync_failures": ("repository_id", "item_kind", "number"),
}
PULL_REQUEST_TABLES = (
    "pull_requests",
    "pr_labels",
    "pr_reviews",
    "pr_comments",
    "pr_commits",
    "pr_files",
    "pr_closing_issues",
    "pr_review_requests",
    "pr_assignees",
)

# Fields stored under their snake-case names (plan 5.1: "named in snake case").
REPOSITORY_FIELDS = (
    "isPrivate",
    "isArchived",
    "isSecurityPolicyEnabled",
    "visibility",
    "description",
    "homepageUrl",
    "stargazerCount",
    "forkCount",
    "diskUsage",
    "forkingAllowed",
    "allowUpdateBranch",
    "webCommitSignoffRequired",
    "hasWikiEnabled",
    "hasDiscussionsEnabled",
    "hasProjectsEnabled",
    "hasVulnerabilityAlertsEnabled",
    "usesCustomOpenGraphImage",
    "mergeCommitAllowed",
    "squashMergeAllowed",
    "rebaseMergeAllowed",
    "deleteBranchOnMerge",
    "mergeCommitTitle",
    "mergeCommitMessage",
    "squashMergeCommitTitle",
    "squashMergeCommitMessage",
)
REPOSITORY_TIMES = ("createdAt", "updatedAt", "pushedAt", "archivedAt")
PULL_REQUEST_FIELDS = (
    "number",
    "state",
    "title",
    "body",
    "includesCreatedEdit",
    "maintainerCanModify",
    "baseRefOid",
    "headRefName",
    "headRefOid",
    "isCrossRepository",
    "reviewDecision",
    "mergeable",
    "mergeStateStatus",
    "canBeRebased",
    "additions",
    "deletions",
    "changedFiles",
    "totalCommentsCount",
)
PULL_REQUEST_TIMES = ("createdAt", "mergedAt", "closedAt", "publishedAt", "lastEditedAt")
TOTALS = {
    "commits_total": "commits",
    "files_total": "files",
    "reviews_total": "reviews",
    "comments_total": "comments",
    "labels_total": "labels",
    "closing_issues_total": "closingIssuesReferences",
    "user_linked_closing_issues_total": "userLinkedClosingIssues",
    "review_requests_total": "reviewRequests",
    "assignees_total": "assignedActors",
}
REVIEW_FIELDS = ("state", "includesCreatedEdit", "authorAssociation", "authorCanPushToRepository")
REVIEW_TIMES = ("createdAt", "submittedAt", "publishedAt", "updatedAt", "lastEditedAt")
COMMENT_FIELDS = ("createdViaEmail", "includesCreatedEdit", "authorAssociation")
COMMENT_TIMES = ("createdAt", "publishedAt", "updatedAt", "lastEditedAt")
# D31: the profile fields a lookup reads, by type; every other profile column is null.
PROFILE_FIELDS = {
    "User": ("company", "location", "websiteUrl"),
    "Organization": (
        "description",
        "email",
        "location",
        "websiteUrl",
        "twitterUsername",
        "isVerified",
        "archivedAt",
        "hasSponsorsListing",
        "pinnedItemsRemaining",
    ),
}
PROFILE_COLUMNS = (
    "company",
    "location",
    "website_url",
    "description",
    "email",
    "twitter_username",
    "is_verified",
    "archived_at",
    "has_sponsors_listing",
    "pinned_items_remaining",
)


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def ts(value: str | None) -> str | None:
    """A GitHub time as UtcDateTime stores it."""
    if value is None:
        return None
    moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def moment(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def nested(node: Node | None, *path: str) -> Any:
    """``node[a][b]...``, or None where a step is null."""
    value: Any = node
    for key in path:
        if value is None:
            return None
        value = value.get(key)
    return value


def account_id(actor: Node | None) -> int | None:
    """The account a reference names, by its ``github_id``: the driver makes no
    ``EnterpriseUserAccount``, the one type without one."""
    return None if actor is None else int(actor["databaseId"])


@dataclass
class StoredAccount:
    """A ``github_users`` row as the model's history says the database holds it."""

    node_id: str
    github_id: int
    login: str
    type: str
    refreshed: datetime
    not_found: bool = False
    # What the latest lookup that returned the account read; None until its first (5.2).
    looked_up: Node | None = None


@dataclass
class StoredPull:
    repository: int
    node: Node  # the pull request as its latest hydration read it
    hydrated: datetime
    rows: dict[str, list[Row]]


@dataclass
class History:
    floor: datetime
    accounts: dict[str, StoredAccount] = field(default_factory=dict)
    pulls: dict[int, StoredPull] = field(default_factory=dict)
    runs: list[int] = field(default_factory=list)
    index_totals: dict[int, int] = field(default_factory=dict)
    # What the runs exercised, so a test can show its seeds reach the rules it checks.
    events: Counter[str] = field(default_factory=Counter)
    # Accounts a sighting of the current run found again after a NOT_FOUND lookup.
    seen_again: set[str] = field(default_factory=set)

    def record_run(
        self, scenario: Scenario, tracked: Sequence[Repository], run_id: int, now: datetime
    ) -> None:
        """What the run ``run_id``, at ``now``, stored over the model as it is."""
        self.runs.append(run_id)
        self.seen_again.clear()
        planned: list[Planned] = []
        listed: dict[int, set[int]] = {}
        for repository in tracked:
            labels = {label["name"] for label in repository.data["labels"]["nodes"]}
            entries = [pull for pull in repository.pulls.values() if not pull.gone]
            self.index_totals[repository.data["databaseId"]] = len(entries)
            covered = listed.setdefault(repository.data["databaseId"], set())
            for pull in entries:
                entry = pull.index_entry()
                if not in_coverage(entry, self.floor):
                    continue
                github_id = int(entry["fullDatabaseId"])
                covered.add(github_id)
                stored = self.pulls.get(github_id)
                reasons = planned_because(entry, stored, labels)
                self.events.update(reasons)
                if reasons:
                    planned.append((repository, pull))
                elif stored is not None:
                    self.update_in_place(stored, entry)

        linked: set[str] = set()
        looked_up: dict[str, Node | None] = {}
        for batch in batches(planned):
            nodes = [copy.deepcopy(pull.node) for _, pull in batch]
            for node in nodes:
                for actor in sightings(node):
                    self.sight(actor, now)
            for node in nodes:
                for user in commit_links(node):
                    linked.add(user["id"])
                    if user["id"] not in self.accounts and user["id"] not in looked_up:
                        looked_up[user["id"]] = self.look_up_link(scenario, user["id"], now)
            for (repository, _), node in zip(batch, nodes, strict=True):
                self.write(repository, node, now)

        self.refresh(scenario, linked, now)
        for github_id, stored in list(self.pulls.items()):
            if github_id not in listed[stored.repository]:
                del self.pulls[github_id]
                self.events["pull request deleted"] += 1

    def update_in_place(self, stored: StoredPull, entry: Node) -> None:
        (row,) = stored.rows["pull_requests"]
        update = {"is_draft": entry["isDraft"], "author_association": entry["authorAssociation"]}
        if any(row[column] != value for column, value in update.items()):
            self.events["updated in place"] += 1
        row.update(update)

    def sight(self, actor: Node, now: datetime) -> None:
        """An actor sighting inserts an account not yet stored, from the sighting, and clears
        ``not_found_at``; it leaves login and type to lookups (5.2)."""
        account = self.accounts.get(actor["id"])
        if account is None:
            self.accounts[actor["id"]] = StoredAccount(
                actor["id"], actor["databaseId"], actor["login"], actor["__typename"], now
            )
        elif account.not_found:
            account.not_found = False
            self.seen_again.add(account.node_id)
            self.events["seen again by a sighting"] += 1

    def look_up_link(self, scenario: Scenario, node_id: str, now: datetime) -> Node | None:
        """A batch's lookup of a commit-linked account not yet stored: found, it is inserted
        from the lookup; NOT_FOUND, it stays unstored and its commits link no account (5.2)."""
        data = copy.deepcopy(scenario.accounts[node_id])
        if data is None:
            self.events["commit link not found"] += 1
            return None
        self.events["inserted by a commit lookup"] += 1
        self.accounts[node_id] = StoredAccount(
            node_id, data["databaseId"], data["login"], data["__typename"], now, looked_up=data
        )
        return data

    def write(self, repository: Repository, node: Node, now: datetime) -> None:
        github_id = int(node["fullDatabaseId"])
        previous = self.pulls.get(github_id)
        stored = {account.github_id for account in self.accounts.values()}
        rows = pull_rows(repository.data, node, previous.rows if previous else {}, stored)
        self.events["reference kept"] += kept_references(rows)
        self.pulls[github_id] = StoredPull(repository.data["databaseId"], node, now, rows)

    def refresh(self, scenario: Scenario, linked: set[str], now: datetime) -> None:
        """The refresh after the last hydration write (5.2): it reads the accounts as the run's
        writes left them, so a sighting in this run that cleared ``not_found_at`` makes the
        account a candidate again."""
        for account in self.accounts.values():
            stale = not account.not_found and now - account.refreshed > REFRESH_AFTER
            gap_90 = account.not_found and account.node_id in linked
            if not (stale or gap_90):
                continue
            self.events["gap 90 lookup" if gap_90 else "refresh"] += 1
            if stale and account.node_id in self.seen_again:
                self.events["refreshed after a sighting found it again"] += 1
            data = copy.deepcopy(scenario.accounts[account.node_id])
            if data is None:
                if not account.not_found:
                    self.events["not found by the refresh"] += 1
                account.not_found = True
                continue
            if data["login"] != account.login:
                self.events["login changed by a lookup"] += 1
            if account.not_found:
                self.events["seen again by a lookup"] += 1
            account.login, account.type = data["login"], data["__typename"]
            account.looked_up, account.refreshed, account.not_found = data, now, False

    def tables(
        self, tracked: Sequence[tuple[Repository, str]], automation: Mapping[str, Any]
    ) -> dict[str, list[Row]]:
        """Every row the database must hold now, by table."""
        run = len(self.runs)
        tables: dict[str, list[Row]] = {table: [] for table in KEYS}
        for repository, group in tracked:
            data = repository.data
            tables["repositories"].append(repository_row(data, group))
            tables["repo_labels"].extend(
                {
                    "repository_id": data["databaseId"],
                    "node_id": label["id"],
                    "name": label["name"],
                    "color": label["color"],
                    "description": label["description"],
                    "is_default": label["isDefault"],
                    "created_at": ts(label["createdAt"]),
                    "updated_at": ts(label["updatedAt"]),
                }
                for label in data["labels"]["nodes"]
            )
            tables["repo_sync_state"].append(
                {
                    "repository_id": data["databaseId"],
                    "last_index_started_at": True,
                    "index_pr_total": self.index_totals[data["databaseId"]],
                    "index_issue_total": None,
                    "last_success_at": True,
                    "last_run_id": run,
                    "last_outcome": "complete",
                    "halt_reason": None,
                }
            )
        for account in self.accounts.values():
            tables["github_users"].append(account_row(account))
            profile = profile_row(account)
            if profile is not None:
                tables["account_profiles"].append(profile)
        for pull in self.pulls.values():
            for table, rows in pull.rows.items():
                tables[table].extend(rows)
        tables["sync_runs"] = [run_row(ordinal) for ordinal in range(1, run + 1)]
        tables["automation_accounts"] = [
            {**entry, "run_id": run} for entry in automation.get("accounts", [])
        ]
        tables["automation_emails"] = [
            {"email": entry["email"].lower(), "reason": entry["reason"], "run_id": run}
            for entry in automation.get("emails", [])
        ]
        return tables


def in_coverage(entry: Node, floor: datetime) -> bool:
    """Plan section 3."""
    state = entry["state"]
    if state == "OPEN":
        return True
    closed = entry["mergedAt"] if state == "MERGED" else entry["closedAt"]
    return closed is not None and moment(closed) >= floor


def planned_because(entry: Node, stored: StoredPull | None, labels: set[str]) -> list[str]:
    """Why plan 5.3 step 3 hydrates the item the index lists as ``entry``; none when it does
    not. Hydration versions never differ and no item has a failure row in these runs."""
    if stored is None:
        return ["planned: not stored"]
    old = stored.node
    reasons = []
    if entry["updatedAt"] != old["updatedAt"]:
        reasons.append("planned: updatedAt")
    if entry["state"] != old["state"]:
        reasons.append("planned: state")
    if stored.hydrated - moment(old["updatedAt"]) < UNSETTLED:
        reasons.append("planned: unsettled")
    if any(label["name"] not in labels for label in old["labels"]["nodes"]):
        reasons.append("planned: label gone")
    if links_differ(entry["closingIssuesReferences"], old["closingIssuesReferences"]):
        reasons.append("planned: links")
    return reasons


def links_differ(indexed: Node, stored: Node) -> bool:
    """D23: the link total differs, an indexed link is not stored, a stored link is not indexed
    when the index lists all of them, or the index lists a link twice."""

    def key(link: Node) -> tuple[int, int]:
        return (link["number"], link["repository"]["databaseId"])

    listed = [key(link) for link in indexed["nodes"]]
    kept = {key(link) for link in stored["nodes"]}
    total = indexed["totalCount"]
    return (
        total != stored["totalCount"]
        or any(link not in kept for link in listed)
        or (len(listed) == total and any(link not in listed for link in kept))
        or len(set(listed)) < min(total, INDEX_CLOSING_LINKS)
    )


def batches(planned: list[Planned]) -> Iterator[list[Planned]]:
    """Plan 5.3 step 3's queue (open items first, then ``updatedAt`` descending; nothing is
    pending in these runs) cut into batches: the first unbatched item and the further ones of
    its repository, up to 25."""

    def order(item: Planned) -> tuple[bool, float]:
        node = item[1].node
        return (node["state"] != "OPEN", -moment(node["updatedAt"]).timestamp())

    queue = sorted(planned, key=order)
    keys = [order(item) for item in queue]
    # Equal keys in two repositories would leave the batch order to the engine's tie-break.
    assert len(set(keys)) == len(keys), "the driver made two changes in one second"
    while queue:
        repository = queue[0][0]
        batch = [item for item in queue if item[0] is repository][:BATCH_SIZE]
        yield batch
        queue = [item for item in queue if all(item is not taken for taken in batch)]


def sightings(node: Node) -> Iterator[Node]:
    """The actor sightings of a hydrated pull request (plan 5.2): author, merger, editors,
    review and comment authors, requested accounts, and assignees."""
    for actor in (node["author"], node["mergedBy"], node["editor"]):
        if actor is not None:
            yield actor
    for connection in ("reviews", "comments"):
        for child in node[connection]["nodes"]:
            for actor in (child["author"], child["editor"]):
                if actor is not None:
                    yield actor
    for request in node["reviewRequests"]["nodes"]:
        reviewer = request["requestedReviewer"]
        if reviewer is not None and reviewer["__typename"] in REVIEWER_ACCOUNTS:
            yield reviewer
    yield from node["assignedActors"]["nodes"]


def commit_links(node: Node) -> Iterator[Node]:
    for commit in node["commits"]["nodes"]:
        user = commit["commit"]["author"]["user"]
        if user is not None:
            yield user


def pull_rows(
    repository: Node, node: Node, previous: Mapping[str, list[Row]], stored: set[int]
) -> dict[str, list[Row]]:
    """The rows a hydration of ``node`` writes (5.3 step 6). ``previous`` holds the rows the
    item's last write left, whose references D28 keeps when GitHub now returns no actor;
    ``stored`` holds the ``github_id`` of every stored account, which a commit link resolves to
    (5.2)."""
    pr = int(node["fullDatabaseId"])
    (before,) = previous.get("pull_requests") or [{}]

    def kept(table: str, key: str, column: str) -> dict[Any, Any]:
        return {row[key]: row[column] for row in previous.get(table, [])}

    row: Row = {snake(name): node[name] for name in PULL_REQUEST_FIELDS}
    row.update({snake(name): ts(node[name]) for name in PULL_REQUEST_TIMES})
    row.update({column: node[connection]["totalCount"] for column, connection in TOTALS.items()})
    author, merger = node["author"], node["mergedBy"]
    row.update(
        repository_id=repository["databaseId"],
        github_id=pr,
        node_id=node["id"],
        is_draft=node["isDraft"],
        author_association=node["authorAssociation"],
        author_id=account_id(author) if author else before.get("author_id"),
        author_reported=author is not None,
        editor_id=account_id(node["editor"]),
        merged_by_id=account_id(merger) if merger else before.get("merged_by_id"),
        merged_by_reported=merger is not None,
        base_ref=node["baseRefName"],
        head_repo_github_id=nested(node, "headRepository", "databaseId"),
        head_repo_full_name=nested(node, "headRepository", "nameWithOwner"),
        head_repo_owner_node_id=nested(node, "headRepositoryOwner", "id"),
        head_repo_owner_login=nested(node, "headRepositoryOwner", "login"),
        merge_commit_oid=nested(node, "mergeCommit", "oid"),
        targets_default_branch=node["baseRefName"] == repository["defaultBranchRef"]["name"],
        milestone_number=nested(node, "milestone", "number"),
        milestone_title=nested(node, "milestone", "title"),
        potential_merge_commit_oid=nested(node, "potentialMergeCommit", "oid"),
        stack_node_id=nested(node, "stack", "id"),
        stack_number=nested(node, "stack", "number"),
        stack_size=nested(node, "stack", "size"),
        stack_base_ref_name=nested(node, "stack", "baseRefName"),
        stack_entry_node_id=nested(node, "stackEntry", "id"),
        stack_position=nested(node, "stackEntry", "position"),
        github_updated_at=ts(node["updatedAt"]),
        hydrated_at=True,
        hydration_version=PULL_REQUEST_HYDRATION_VERSION,
    )

    reviewers = kept("pr_reviews", "github_id", "reviewer_id")
    reviews = []
    for review in node["reviews"]["nodes"]:
        github_id = int(review["fullDatabaseId"])
        reviews.append(
            {
                **{snake(name): review[name] for name in REVIEW_FIELDS},
                **{snake(name): ts(review[name]) for name in REVIEW_TIMES},
                "github_id": github_id,
                "node_id": review["id"],
                "pr_id": pr,
                "reviewer_id": account_id(review["author"])
                if review["author"]
                else reviewers.get(github_id),
                "reviewer_reported": review["author"] is not None,
                "editor_id": account_id(review["editor"]),
                "commit_oid": nested(review, "commit", "oid"),
                "inline_comments_total": review["comments"]["totalCount"],
            }
        )

    authors = kept("pr_comments", "github_id", "author_id")
    comments = []
    for comment in node["comments"]["nodes"]:
        github_id = int(comment["fullDatabaseId"])
        comments.append(
            {
                **{snake(name): comment[name] for name in COMMENT_FIELDS},
                **{snake(name): ts(comment[name]) for name in COMMENT_TIMES},
                "github_id": github_id,
                "node_id": comment["id"],
                "pr_id": pr,
                "author_id": account_id(comment["author"])
                if comment["author"]
                else authors.get(github_id),
                "author_reported": comment["author"] is not None,
                "editor_id": account_id(comment["editor"]),
            }
        )

    linked = kept("pr_commits", "sha", "author_id")
    commits = []
    for entry in node["commits"]["nodes"]:
        commit = entry["commit"]
        user = commit["author"]["user"]
        if user is None:
            author_id, linked_github_id = linked.get(commit["oid"]), None
        else:
            linked_github_id = int(user["databaseId"])
            author_id = linked_github_id if linked_github_id in stored else None
        commits.append(
            {
                "pr_id": pr,
                "sha": commit["oid"],
                "author_id": author_id,
                "linked_github_id": linked_github_id,
                "author_name": commit["author"]["name"],
                "author_email": commit["author"]["email"],
                "authored_at": ts(commit["authoredDate"]),
            }
        )

    user_linked = {
        (link["number"], link["repository"]["databaseId"])
        for link in node["userLinkedClosingIssues"]["nodes"]
    }
    requests = []
    for ordinal, request in enumerate(node["reviewRequests"]["nodes"]):
        reviewer = request["requestedReviewer"] or {}
        is_account = reviewer.get("__typename") in REVIEWER_ACCOUNTS
        requests.append(
            {
                "pr_id": pr,
                "ordinal": ordinal,
                "request_github_id": request["databaseId"],
                "request_node_id": request["id"],
                "requested_type": reviewer.get("__typename"),
                "requested_github_id": reviewer.get("databaseId", reviewer.get("fullDatabaseId")),
                "requested_node_id": reviewer.get("id"),
                "account_id": reviewer["databaseId"] if is_account else None,
                "team_slug": None if is_account else reviewer.get("slug"),
                "as_code_owner": request["asCodeOwner"],
            }
        )

    return {
        "pull_requests": [row],
        "pr_labels": [
            {"pr_id": pr, "name": label["name"], "label_node_id": label["id"]}
            for label in node["labels"]["nodes"]
        ],
        "pr_reviews": reviews,
        "pr_comments": comments,
        "pr_commits": commits,
        "pr_files": [
            {
                "pr_id": pr,
                "ordinal": ordinal,
                "path": change["path"],
                "change_type": change["changeType"],
                "additions": change["additions"],
                "deletions": change["deletions"],
            }
            for ordinal, change in enumerate(node["files"]["nodes"])
        ],
        "pr_closing_issues": [
            {
                "pr_id": pr,
                "issue_repo_github_id": link["repository"]["databaseId"],
                "issue_repo_full_name": link["repository"]["nameWithOwner"],
                "issue_number": link["number"],
                "issue_github_id": int(link["fullDatabaseId"]),
                "user_linked": (link["number"], link["repository"]["databaseId"]) in user_linked,
            }
            for link in node["closingIssuesReferences"]["nodes"]
        ],
        "pr_review_requests": requests,
        "pr_assignees": [
            {"pr_id": pr, "account_id": account_id(actor)}
            for actor in node["assignedActors"]["nodes"]
        ],
    }


def kept_references(rows: Mapping[str, list[Row]]) -> int:
    """References D28 kept in these rows: a reference GitHub returned no actor for that still
    names the account stored before."""
    (pull,) = rows["pull_requests"]
    kept = [
        not pull["author_reported"] and pull["author_id"] is not None,
        not pull["merged_by_reported"] and pull["merged_by_id"] is not None,
    ]
    kept += [
        not r["reviewer_reported"] and r["reviewer_id"] is not None for r in rows["pr_reviews"]
    ]
    kept += [not c["author_reported"] and c["author_id"] is not None for c in rows["pr_comments"]]
    kept += [
        c["linked_github_id"] is None and c["author_id"] is not None for c in rows["pr_commits"]
    ]
    return sum(kept)


def repository_row(data: Node, group: str) -> Row:
    row: Row = {snake(name): data[name] for name in REPOSITORY_FIELDS}
    row.update({snake(name): ts(data[name]) for name in REPOSITORY_TIMES})
    row.update(
        github_id=data["databaseId"],
        node_id=data["id"],
        owner=data["owner"]["login"],
        name=data["name"],
        full_name=data["nameWithOwner"],
        repo_group=group,
        default_branch=data["defaultBranchRef"]["name"],
        labels_total=data["labels"]["totalCount"],
        is_tracked=True,
        resolved_at=True,
        primary_language=nested(data, "primaryLanguage", "name"),
        license_spdx_id=nested(data, "licenseInfo", "spdxId"),
        plan_features_codeowners=data["planFeatures"]["codeowners"],
        plan_features_team_review_requests=data["planFeatures"]["teamReviewRequests"],
        plan_features_maximum_manual_review_requests=data["planFeatures"][
            "maximumManualReviewRequests"
        ],
    )
    return row


def account_row(account: StoredAccount) -> Row:
    """Name, times, and the linked ids come only from a lookup (5.2), null until the first."""
    data = account.looked_up or {}
    return {
        "github_id": account.github_id,
        "node_id": account.node_id,
        "login": account.login,
        "type": account.type,
        "name": data.get("name"),
        "created_at": ts(data.get("createdAt")),
        "updated_at": ts(data.get("updatedAt")),
        "linked_user_github_id": nested(data, "user", "databaseId"),
        "claimant_github_id": nested(data, "claimant", "databaseId"),
        "refreshed_at": True,
        "not_found_at": account.not_found,
    }


def profile_row(account: StoredAccount) -> Row | None:
    """D31: a ``User``'s or ``Organization``'s profile as the latest lookup read it."""
    data = account.looked_up
    if data is None or account.type not in PROFILE_FIELDS:
        return None
    row: Row = dict.fromkeys(PROFILE_COLUMNS)
    row.update({snake(name): data[name] for name in PROFILE_FIELDS[account.type]})
    row.update(account_id=account.github_id, archived_at=ts(row["archived_at"]), refreshed_at=True)
    return row


def run_row(ordinal: int) -> Row:
    """A run of these tests: complete, exit 0, without a deadline or deletions to approve."""
    return {
        "id": ordinal,
        "started_at": True,
        "deadline_at": False,
        "finished_at": True,
        "status": "complete",
        "exit_code": 0,
        "trigger": "manual",
        "code_version": "test",
        "config_source": "test",
        "config_sha256": "0" * 64,
        "deletions_approved": None,
    }


def stored_tables(database: Path, runs: Sequence[int]) -> dict[str, list[Row]]:
    """Every table's rows as the database holds them, read the way ``History.tables`` writes
    them: row ids dropped, a foreign key as the natural key of the row it names, stamps as set or
    not, and the run counters left out."""
    connection = sqlite3.connect(database)
    try:
        names = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
            )
        ]
        natural: dict[str, dict[int, Any]] = {
            table: dict(connection.execute(f"SELECT id, github_id FROM {table}"))
            for table in ("repositories", "github_users", "pull_requests")
        }
        natural["sync_runs"] = {run_id: ordinal for ordinal, run_id in enumerate(runs, 1)}
        tables: dict[str, list[Row]] = {}
        for table in names:
            columns = [info[1] for info in connection.execute(f"PRAGMA table_info({table})")]
            references = {
                key[3]: key[2] for key in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            if table == "sync_runs":
                references["id"] = "sync_runs"
            rows = []
            for values in connection.execute(f"SELECT * FROM {table}"):
                row: Row = {}
                for column, value in zip(columns, values, strict=True):
                    if (table, column) in RUN_COUNTERS or (column == "id" and table != "sync_runs"):
                        continue
                    if column in references and value is not None:
                        value = natural.get(references[column], {}).get(
                            value, f"<{references[column]} row {value}>"
                        )
                    row[column] = value is not None if (table, column) in STAMPS else value
                rows.append(row)
            tables[table] = rows
        return tables
    finally:
        connection.close()


ABSENT = "<no such column>"


def differences(expected: Mapping[str, list[Row]], stored: Mapping[str, list[Row]]) -> list[str]:
    """Each table's rows paired by natural key: rows on one side only, and for a pair each
    column whose values differ."""
    lines: list[str] = []
    for table in sorted(set(expected) | set(stored)):
        key = KEYS.get(table)
        want, want_twice = keyed(expected.get(table, []), key)
        have, have_twice = keyed(stored.get(table, []), key)
        lines += [f"{table}: expected key {k} twice" for k in want_twice]
        lines += [f"{table}: stored key {k} twice" for k in have_twice]
        for k in sorted(set(want) | set(have), key=repr):
            if k not in have:
                lines.append(f"{table} {k}: missing; expected {want[k]}")
            elif k not in want:
                lines.append(f"{table} {k}: not expected; stored {have[k]}")
            else:
                for column in sorted(set(want[k]) | set(have[k])):
                    w, h = want[k].get(column, ABSENT), have[k].get(column, ABSENT)
                    if w != h:
                        lines.append(f"{table} {k} {column}: expected {w!r}, stored {h!r}")
    return lines


def keyed(
    rows: list[Row], key: tuple[str, ...] | None
) -> tuple[dict[tuple[Any, ...], Row], list[tuple[Any, ...]]]:
    found: dict[tuple[Any, ...], Row] = {}
    twice = []
    for row in rows:
        normal = {column: int(v) if isinstance(v, bool) else v for column, v in row.items()}
        k = tuple(normal.get(c) for c in key) if key else tuple(sorted(normal.items()))
        if k in found:
            twice.append(k)
        found[k] = normal
    return found, twice
