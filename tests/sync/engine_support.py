"""A migrated database and a scenario model, synced by the engine (plan Step 3b-2a-2b-2)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from github_activity_db.sync.engine import SyncOptions, SyncReport, sync
from github_activity_db.tracking import TrackedRepository, TrackingConfig, TrackingConfigFile
from tests.source.scenario import Repository, Scenario
from tests.sync.records import fetch

FLOOR = datetime(2023, 1, 1, tzinfo=UTC)
# Past the 10-minute unsettled window, so a run after it plans only what changed.
SETTLED = 11 * 60


class World:
    """The scenario and the database; ``config`` lists the repositories a test tracks."""

    def __init__(self, scenario: Scenario, database: Path) -> None:
        self.scenario = scenario
        self.database = database
        self.url = f"sqlite+aiosqlite:///{database}"
        self.tracked: list[dict[str, Any]] = []
        self.sync_settings: dict[str, Any] = {}
        self.automation: dict[str, Any] = {}
        self.floor = FLOOR
        # Options a parametrized case gives both a dry run and the run.
        self.options: dict[str, Any] = {}

    def track(self, repository: Repository | str, group: str = "Client") -> TrackedRepository:
        """Add a ``tracking.toml`` entry for the repository, or for an ``owner/name``."""
        full_name = repository if isinstance(repository, str) else repository.full_name
        owner, name = full_name.split("/")
        self.tracked.append({"owner": owner, "name": name, "group": group})
        return TrackedRepository(owner=owner, name=name, group=group)

    def untrack(self, full_name: str) -> None:
        """Drop the repository's ``tracking.toml`` entry."""
        self.tracked = [
            entry for entry in self.tracked if f"{entry['owner']}/{entry['name']}" != full_name
        ]

    def config(self) -> TrackingConfigFile:
        data: dict[str, Any] = {
            "coverage": {"floor": self.floor},
            "repositories": self.tracked,
            "sync": self.sync_settings,
        }
        if self.automation:
            data["automation"] = self.automation
        return TrackingConfigFile(TrackingConfig.model_validate(data), "test", "0" * 64)

    async def sync(self, *, settle: bool = True, **options: Any) -> SyncReport:
        """Run the engine; with ``settle``, first move the clock past the unsettled window, so
        the run hydrates every change it reads settled and the next run plans only new ones."""
        if settle:
            self.scenario.clock.advance(SETTLED)
        return await sync(
            SyncOptions(
                database_url=self.url,
                config=self.config(),
                trigger="manual",
                code_version="test",
                **options,
            ),
            open_source=self.scenario.source,
            clock=self.scenario.clock,
        )

    def rows(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        return fetch(self.database, sql, *params)

    def execute(self, sql: str, *params: Any) -> None:
        """A change a test makes to the stored rows between runs, committed."""
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(sql, params)
        finally:
            connection.close()

    def value(self, sql: str, *params: Any) -> Any:
        (row,) = fetch(self.database, sql, *params)
        (value,) = row.values()
        return value

    def run(self, run_id: int | None) -> dict[str, Any]:
        (row,) = self.rows("SELECT * FROM sync_runs WHERE id = ?", run_id)
        return row

    def state(self, repository: Repository) -> dict[str, Any] | None:
        """The repository's ``repo_sync_state`` row, None when it has none."""
        rows = self.rows(
            "SELECT s.* FROM repo_sync_state s JOIN repositories r ON r.id = s.repository_id "
            "WHERE r.github_id = ?",
            repository.data["databaseId"],
        )
        return rows[0] if rows else None

    def numbers(self, repository: Repository) -> list[int]:
        """The stored pull-request numbers of the repository."""
        return [
            row["number"]
            for row in self.rows(
                "SELECT p.number FROM pull_requests p "
                "JOIN repositories r ON r.id = p.repository_id "
                "WHERE r.github_id = ? ORDER BY p.number",
                repository.data["databaseId"],
            )
        ]

    def pending(self, repository: Repository) -> list[int]:
        return [
            row["number"]
            for row in self.rows(
                "SELECT s.number FROM sync_pending s JOIN repositories r ON r.id = s.repository_id "
                "WHERE r.github_id = ? ORDER BY s.number",
                repository.data["databaseId"],
            )
        ]

    def failures(self) -> list[tuple[str, int, str]]:
        """``sync_failures`` rows: repository, number, and the error's class."""
        return [
            (row["full_name"], row["number"], row["last_error"].split(":")[0])
            for row in self.rows(
                "SELECT r.full_name, f.number, f.last_error FROM sync_failures f "
                "JOIN repositories r ON r.id = f.repository_id ORDER BY r.full_name, f.number"
            )
        ]


def outcomes(world: World, run_id: int | None) -> dict[str, str]:
    """The ``repo_sync_state`` rows the run wrote: each repository's ``last_outcome``."""
    return {
        row["full_name"]: row["last_outcome"]
        for row in world.rows(
            "SELECT r.full_name, s.last_outcome FROM repo_sync_state s "
            "JOIN repositories r ON r.id = s.repository_id WHERE s.last_run_id = ? "
            "ORDER BY r.full_name",
            run_id,
        )
    }


def ended(world: World, report: SyncReport) -> tuple[str, int | None, dict[str, str]]:
    """How the run's row ended and what it wrote to ``repo_sync_state``; the report agrees."""
    run = world.run(report.run_id)
    assert (report.status, report.exit_code) == (run["status"], run["exit_code"])
    return run["status"], run["exit_code"], outcomes(world, report.run_id)


def planned(report: SyncReport) -> list[int]:
    return [item.number for item in report.planned]


def project(world: World, pulls: int = 1, name: str = "Prebid.js") -> tuple[Repository, str]:
    """A tracked repository with ``pulls`` open pull requests by one author."""
    scenario = world.scenario
    author = scenario.add_account()
    repository = scenario.add_repository("prebid", name)
    for _ in range(pulls):
        scenario.open_pull_request(repository, author)
    world.track(repository)
    return repository, author
