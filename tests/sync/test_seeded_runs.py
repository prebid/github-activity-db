"""Seeded runs over random GitHub changes (redesign plan Step 3b-2a-2c).

Each fixed seed builds a small world in the scenario model, then drives several runs, changing
GitHub between them through the model's own methods. After each run the database equals the state
``History`` computes from the model and from what earlier runs stored (``expected_state.py``),
compared table by table on every column except row ids, the stamps the sync takes from ``Date``
headers (compared as set or not), and the run counters.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.source.scenario import PullRequest, Repository, Scenario, ScenarioError, iso
from tests.sync.engine_support import World
from tests.sync.expected_state import History, differences, stored_tables

RUNS = 5
# What every seed must reach: a pull request's author D28 kept, the 24-hour refresh finding an
# account gone, and a state change.
CORE = frozenset({"kept: author", "refresh", "not found by the refresh", "planned: state"})
# Seeds chosen from 1-120, every one of which left the expected state after each run, for the
# rarer paths each reaches (``History.events``, counted only when they happen); together they
# reach every path it counts, each kind of reference D28 keeps included, except an unsettled
# item, which ``World.sync`` settles on purpose.
REACHES = {
    9: CORE
    | {
        "login changed by a lookup",
        "kept: reviewer",
        "kept: commenter",
        "inserted by a commit lookup",
        "planned: links",
    },
    35: CORE
    | {
        "login changed by a lookup",
        "updated in place",
        "planned: links",
        "inserted by a commit lookup",
        "gap 90 lookup",
        "seen again by a lookup",
    },
    56: CORE
    | {
        "kept: merger",
        "kept: reviewer",
        "kept: commit link",
        "gap 90 lookup",
        "inserted by a commit lookup",
        "updated in place",
        "planned: links",
    },
    74: CORE
    | {
        "login changed by a lookup",
        "kept: merger",
        "commit link not found",
        "gap 90 lookup",
        "seen again by a sighting",
        "refreshed after a sighting found it again",
    },
    93: CORE
    | {
        "login changed by a lookup",
        "updated in place",
        "planned: links",
        "planned: label gone",
        "inserted by a commit lookup",
        "pull request deleted",
        "seen again by a sighting",
        "refreshed after a sighting found it again",
    },
}
SEEDS = tuple(REACHES)
REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
ASSOCIATIONS = ("MEMBER", "CONTRIBUTOR", "COLLABORATOR", "NONE")
MAX_LINKS = 3
MAX_DELETED = 3
# How often each change is drawn: mostly content, and account changes often enough to reach
# D28's kept references and the refresh.
CHANGES = {
    "open": 2,
    "comment": 4,
    "review": 4,
    "commit": 4,
    "push files": 1,
    "label": 2,
    "unlabel": 1,
    "request review": 3,
    "withdraw request": 1,
    "assign": 3,
    "unassign": 1,
    "merge": 2,
    "close": 1,
    "reopen": 1,
    "draft": 2,
    "ready": 2,
    "association": 2,
    "edit": 1,
    "link": 2,
    "unlink": 1,
    "add label": 1,
    "rename label": 2,
    "delete account": 2,
    "restore account": 2,
    "rename account": 2,
    "gone": 1,
}


@pytest.fixture
def world(scenario: Scenario, migrated_database: Path) -> World:
    return World(scenario, migrated_database)


class Driver:
    """Random GitHub changes through the scenario model's own methods. A change the model, or
    GitHub, refuses raises ``ScenarioError`` and is skipped. Every change takes its own second,
    so no two ``updatedAt`` values are equal and the queue order (5.3 step 3) has no ties."""

    def __init__(self, world: World, history: History, rng: random.Random) -> None:
        self.world = world
        self.scenario = world.scenario
        self.history = history
        self.rng = rng
        self.repositories: list[Repository] = []
        self.groups: dict[str, str] = {}
        self.users: list[str] = []
        self.deleted: list[str] = []
        self.serial = itertools.count(1)

    def build(self) -> None:
        """Two repositories of four pull requests and two labels; three users and a bot who act,
        a bot only commits name (stored through a lookup, 5.2), a user deleted before anything
        names it but a commit (gap 90), and an organization only ever assigned (D14)."""
        scenario = self.scenario
        self.users = [scenario.add_account("User") for _ in range(3)]
        self.bot = scenario.add_account("Bot", login="helper")
        self.bot_id = scenario.account(self.bot)["databaseId"]
        self.committers = [scenario.add_account("Bot", login="builder"), scenario.add_account()]
        self.organization = scenario.add_account("Organization", login="an-org")
        scenario.delete_account(self.committers[1])
        self.deleted.append(self.committers[1])
        for name, group in (("alpha", "Client"), ("beta", "Server")):
            repository = scenario.add_repository("prebid", name)
            self.world.track(repository, group)
            self.repositories.append(repository)
            self.groups[repository.full_name] = group
            for _ in range(2):
                scenario.add_label(repository, self.name("label"))
            for _ in range(4):
                self.tick()
                scenario.open_pull_request(repository, self.rng.choice(self.people()))

    def tracked(self) -> list[tuple[Repository, str]]:
        return [(repository, self.groups[repository.full_name]) for repository in self.repositories]

    def configure(self, run: int) -> None:
        """The ``[automation]`` lists, their reasons new each run, so each run's copy shows."""
        self.world.automation = {
            "accounts": [
                {"github_id": self.bot_id, "login": "helper", "reason": f"bot, run {run}"}
            ],
            "emails": [{"email": "CI@Example.com", "reason": f"ci, run {run}"}],
        }

    def tick(self) -> None:
        self.scenario.clock.advance(1)

    def change(self) -> str:
        changes = self.changes()
        (name,) = self.rng.choices(list(changes), weights=[CHANGES[name] for name in changes])
        self.tick()
        try:
            changes[name]()
        except ScenarioError as refused:
            return f"{name} (refused: {refused})"
        return name

    # -- what to change -------------------------------------------------------------------
    def people(self) -> list[str]:
        """Accounts that author, review, comment, commit, and are requested."""
        return [*self.users, self.bot]

    def anyone(self) -> str | None:
        """An account to act, or none GitHub still names (D28)."""
        return self.rng.choice([*self.people(), None])

    def pull(self, *states: str, draft: bool | None = None) -> PullRequest:
        """A pull request GitHub still lists, in one of ``states`` when given."""
        pulls = [
            pull
            for repository in self.repositories
            for pull in repository.pulls.values()
            if not pull.gone
            and (not states or pull.node["state"] in states)
            and (draft is None or pull.node["isDraft"] == draft)
        ]
        if not pulls:
            raise ScenarioError(f"no pull request is {' or '.join(states) or 'left'}")
        return self.rng.choice(pulls)

    def repository_of(self, pull: PullRequest) -> Repository:
        return next(r for r in self.repositories if any(p is pull for p in r.pulls.values()))

    def pick(self, choices: list[str], what: str) -> str:
        if not choices:
            raise ScenarioError(f"no {what}")
        return self.rng.choice(choices)

    def name(self, kind: str) -> str:
        return f"{kind}-{next(self.serial)}"

    def changes(self) -> dict[str, Callable[[], object]]:
        scenario = self.scenario
        return {
            "open": lambda: scenario.open_pull_request(
                self.rng.choice(self.repositories), self.rng.choice(self.people())
            ),
            "comment": lambda: self.pull().add(
                "comments", scenario.node("comments", by=self.anyone())
            ),
            "review": lambda: self.pull().add(
                "reviews",
                scenario.node("reviews", by=self.anyone(), state=self.rng.choice(REVIEW_STATES)),
            ),
            # A commit can link a deleted account too (gap 90).
            "commit": lambda: self.pull().add(
                "commits",
                scenario.node(
                    "commits", by=self.rng.choice([*self.people(), *self.committers, None])
                ),
            ),
            "push files": lambda: self.pull().add("files", scenario.node("files")),
            "label": self.label,
            "unlabel": self.unlabel,
            "request review": self.request_review,
            "withdraw request": self.withdraw_request,
            "assign": self.assign,
            "unassign": self.unassign,
            "merge": lambda: self.pull("OPEN", draft=False).merge(
                by=self.rng.choice([*self.users, None])
            ),
            "close": lambda: self.pull("OPEN").close(),
            "reopen": lambda: self.pull("CLOSED").reopen(),
            "draft": self.draft,
            # Marking a draft ready does not move updatedAt (plan 5.3 step 3).
            "ready": lambda: self.pull("OPEN", draft=True).set(isDraft=False),
            "association": lambda: self.pull().set(authorAssociation=self.rng.choice(ASSOCIATIONS)),
            "edit": self.edit,
            "link": self.link,
            "unlink": self.unlink,
            "add label": lambda: scenario.add_label(
                self.rng.choice(self.repositories), self.name("label")
            ),
            "rename label": self.rename_label,
            "delete account": self.delete_account,
            "restore account": self.restore_account,
            "rename account": self.rename_account,
            "gone": self.gone,
        }

    def label(self) -> None:
        pull = self.pull()
        carried = {label["id"] for label in pull.node["labels"]["nodes"]}
        labels = self.repository_of(pull).data["labels"]["nodes"]
        label = self.pick([label["id"] for label in labels if label["id"] not in carried], "label")
        pull.add(
            "labels", next({"id": x["id"], "name": x["name"]} for x in labels if x["id"] == label)
        )

    def unlabel(self) -> None:
        pull = self.pull()
        label = self.pick([label["id"] for label in pull.node["labels"]["nodes"]], "label")
        pull.remove("labels", lambda node: node["id"] == label)

    def request_review(self) -> None:
        pull = self.pull()
        requested = {
            node["requestedReviewer"]["id"]
            for node in pull.node["reviewRequests"]["nodes"]
            if node["requestedReviewer"] is not None
        }
        reviewer = self.pick([a for a in self.people() if a not in requested], "reviewer")
        pull.add("reviewRequests", self.scenario.node("reviewRequests", by=reviewer))

    def withdraw_request(self) -> None:
        pull = self.pull()
        request = self.pick([n["id"] for n in pull.node["reviewRequests"]["nodes"]], "request")
        pull.remove("reviewRequests", lambda node: node["id"] == request)

    def assign(self) -> None:
        pull = self.pull()
        assigned = {node["id"] for node in pull.node["assignedActors"]["nodes"]}
        candidates = [a for a in [*self.people(), self.organization] if a not in assigned]
        assignee = self.pick(candidates, "assignee")
        pull.add("assignedActors", self.scenario.node("assignedActors", by=assignee))

    def unassign(self) -> None:
        pull = self.pull()
        assignee = self.pick([n["id"] for n in pull.node["assignedActors"]["nodes"]], "assignee")
        pull.remove("assignedActors", lambda node: node["id"] == assignee)

    def draft(self) -> None:
        """Converting to a draft moves ``updatedAt`` (docs/testing.md: ``set`` then ``touch``)."""
        pull = self.pull("OPEN", draft=False)
        pull.set(isDraft=True)
        pull.touch()

    def edit(self) -> None:
        pull = self.pull()
        editor = self.scenario.actor(self.rng.choice(self.users))
        now = iso(self.scenario.clock.now())
        pull.set(body=self.name("body"), lastEditedAt=now, editor=editor, includesCreatedEdit=True)
        pull.touch()

    def link(self) -> None:
        pull = self.pull()
        repository = self.rng.choice(self.repositories)
        number = self.rng.randint(1, 30)
        links = pull.node["closingIssuesReferences"]
        if links["totalCount"] >= MAX_LINKS:
            raise ScenarioError(f"#{pull.number} links {MAX_LINKS} issues")
        target = (number, repository.data["databaseId"])
        if any((n["number"], n["repository"]["databaseId"]) == target for n in links["nodes"]):
            raise ScenarioError(f"#{pull.number} links {repository.full_name}#{number}")
        pull.link(number, repository, user_linked=self.rng.random() < 0.5)

    def unlink(self) -> None:
        pull = self.pull()
        links = pull.node["closingIssuesReferences"]["nodes"]
        if not links:
            raise ScenarioError(f"#{pull.number} links no issue")
        link = self.rng.choice(links)
        repository = next(
            r for r in self.repositories if r.data["databaseId"] == link["repository"]["databaseId"]
        )
        pull.unlink(link["number"], repository)

    def rename_label(self) -> None:
        repository = self.rng.choice(self.repositories)
        labels = [label["id"] for label in repository.data["labels"]["nodes"]]
        self.scenario.rename_label(repository, self.pick(labels, "label"), self.name("label"))

    def delete_account(self) -> None:
        if len(self.deleted) >= MAX_DELETED:
            raise ScenarioError(f"{MAX_DELETED} accounts are deleted")
        everyone = [*self.people(), *self.committers, self.organization]
        alive = [a for a in everyone if a not in self.deleted]
        account = self.pick(alive, "account")
        self.scenario.delete_account(account)
        self.deleted.append(account)

    def restore_account(self) -> None:
        account = self.pick(self.deleted, "deleted account")
        self.scenario.restore_account(account)
        self.deleted.remove(account)

    def rename_account(self) -> None:
        """A new login, and a new location where the profile has one. Only a stored account is
        renamed: one renamed before it is stored could be sighted under two logins in one run,
        and which inserts it would depend on the write order."""
        stored = [a for a in self.history.accounts if a not in self.deleted]
        data = self.scenario.account(self.pick(stored, "stored account"))
        data["login"] = self.name("login")
        data["updatedAt"] = iso(self.scenario.clock.now())
        if "location" in data:
            data["location"] = self.name("place")

    def gone(self) -> None:
        self.pull().gone = True


@pytest.mark.parametrize("seed", SEEDS)
async def test_each_run_leaves_the_state_the_model_and_its_history_give(
    world: World, seed: int
) -> None:
    rng = random.Random(seed)  # noqa: S311 - a repeatable test driver, not a secret
    history = History(world.floor)
    driver = Driver(world, history, rng)
    driver.build()
    clock = world.scenario.clock
    log = [f"seed {seed}"]
    for run in range(1, RUNS + 1):
        if run > 1:
            # Sometimes past the 24-hour refresh of every account stored so far.
            gap = rng.choice([rng.randint(60, 6 * 3600), rng.randint(20 * 3600, 30 * 3600)])
            clock.advance(gap)
            made = [driver.change() for _ in range(rng.randint(6, 14))]
            log.append(f"before run {run}, {gap} s later: {', '.join(made)}")
        driver.configure(run)

        report = await world.sync()

        # The expected state reads the model after the run as the run read it: nothing changed
        # it, and the clock did not move during the run.
        assert clock.sleeps == []
        assert (report.status, report.exit_code, report.error) == ("complete", 0, None)
        assert (report.failures, report.refresh_failures) == ((), ())
        assert report.run_id is not None
        history.record_run(world.scenario, driver.repositories, report.run_id, clock.now())
        expected = history.tables(driver.tracked(), world.automation)
        found = differences(expected, stored_tables(world.database, history.runs))
        assert found == [], "\n".join([*log, f"after run {run}:", *found])

    # The seed still drives the runs through the rules it was chosen for.
    reached = {event for event, count in history.events.items() if count > 0}
    assert REACHES[seed] <= reached, "\n".join([*log, str(dict(history.events))])
