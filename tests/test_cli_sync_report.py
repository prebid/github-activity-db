"""The report ``ghactivity sync`` prints (redesign plan 5.3 step 8), rendered from real engine
reports over the scenario model: step 1's updates, each planned hydration with its reasons, each
in-place update and delete candidate, untracked and pruned repositories, failures, and GitHub's
text printed as written."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from github_activity_db.cli.sync import print_report
from github_activity_db.sync.engine import SyncReport
from github_activity_db.sync.writer import Pruned, SecondNodeId
from tests.source.scenario import Scenario
from tests.sync.engine_support import World, project


@pytest.fixture
def world(scenario: Scenario, migrated_database: Path) -> Iterator[World]:
    yield World(scenario, migrated_database)


def rendered(report: SyncReport, **options: Any) -> list[str]:
    """The report's lines as the command prints them (``print_report``), on a console narrower
    than most of them, so a wrapped line would show."""
    buffer = io.StringIO()
    print_report(Console(file=buffer, width=40, color_system=None), report, seconds=0, **options)
    return buffer.getvalue().splitlines()


async def test_a_dry_run_reports_step_1s_updates_and_its_plan(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=3)
    kept = scenario.add_label(repository, "old")
    renamed = scenario.add_label(repository, "rename-me")
    repository.pulls[1].set(isDraft=True)
    await world.sync()
    github_id = repository.data["databaseId"]
    third = repository.pulls[3].node["fullDatabaseId"]
    scenario.rename_repository(repository, "Prebid2.js")
    repository.data["defaultBranchRef"]["name"] = "main"
    labels = repository.data["labels"]
    labels["nodes"] = [label for label in labels["nodes"] if label["id"] != kept["id"]]
    labels["totalCount"] -= 1
    scenario.rename_label(repository, renamed["id"], "renamed")
    # GitHub's text is printed as written: markup, an emoji code, brackets split across two
    # names, and a trailing backslash.
    for name in ("[red]new[/red]", ":tada:", "[a", "b]", "C:\\"):
        scenario.add_label(repository, name)
    repository.data["isArchived"] = True
    world.tracked[0]["group"] = "Server"
    repository.pulls[1].set(isDraft=False)
    repository.pulls[2].add("comments", scenario.node("comments", by=author))
    repository.pulls[3].gone = True

    report = await world.sync(dry_run=True)

    lines = rendered(report)
    assert lines[0] == "Dry run: the run would exit 0"
    assert lines[1] == (
        "prebid/Prebid.js: complete (renamed from prebid/Prebid.js, group Client -> Server, "
        "archived, default branch master -> main, label added :tada:, label added C:\\, label "
        "added [a, label added [red]new[/red], label added b], label removed old, label renamed "
        "rename-me -> renamed, GitHub name prebid/Prebid2.js)"
    )
    assert lines[2] == "  planned 1, in-place updates 1, delete candidates 1"
    assert lines[3:6] == [
        "  hydrate #2: updatedAt differs from github_updated_at",
        "  update #1: is_draft False, author_association CONTRIBUTOR",
        f"  delete #3 (github_id {third})",
    ]
    assert lines[6].startswith("Hydrated 0 items; GraphQL points ")
    assert len(lines) == 7
    assert report.repositories[0].record is not None
    assert report.repositories[0].record.github_id == github_id


async def test_a_dry_run_reports_untracked_pruned_halted_and_pending(world: World) -> None:
    scenario = world.scenario
    repository, author = project(world, pulls=30)
    project(world, name="dropped")
    await world.sync()
    world.untrack("prebid/dropped")
    await world.sync()  # marks it untracked
    for number in range(1, 28):
        repository.pulls[number].gone = True
    repository.pulls[28].add("comments", scenario.node("comments", by=author))
    world.execute(
        "INSERT INTO sync_pending (repository_id, item_kind, number, run_id) "
        "SELECT id, 'pull_request', 28, 2 FROM repositories WHERE name = 'Prebid.js'"
    )

    report = await world.sync(dry_run=True, prune_untracked=True)

    lines = rendered(report)
    assert lines[:3] == [
        "Dry run: the run would exit 2",
        "prebid/Prebid.js: halted",
        "  planned 1, in-place updates 0, delete candidates 27",
    ]
    assert lines[3].startswith("  deletions halted: 27 deletion candidates exceed the delete guard")
    assert lines[4] == "  hydrate #28 (pending): updatedAt differs from github_updated_at"
    assert len([line for line in lines if line.startswith("  delete #")]) == 27
    assert "Would prune: prebid/dropped (1 items)" in lines

    world.track("prebid/dropped")
    again = rendered(await world.sync(dry_run=True))
    assert "prebid/dropped: complete (tracked again)" in again


async def test_a_run_reports_failures_refusals_and_its_error(world: World) -> None:
    scenario = world.scenario
    repository, _ = project(world, pulls=2)
    world.track("prebid/missing")
    world.sync_settings["accepted_failures"] = [
        {"repository": "prebid/Prebid.js", "number": 1, "reason": "known [bold]one[/bold]"},
        {"repository": "prebid/Prebid.js", "number": 2, "reason": "never fails"},
    ]
    scenario.replay.modify(
        lambda body: body["data"]["repository"]["p0"].update(fullDatabaseId="123"),
        when=lambda seen: seen.operation == "PullRequestBatch",
    )

    report = await world.sync(prune_untracked=True)

    lines = rendered(report, break_lock=True)
    assert lines[0] == f"Run {report.run_id}: complete, exit 2"
    assert lines[1] == "--break-lock found no blocking run"
    assert (
        "prebid/missing: no outcome: RepositoryNotFound: prebid/missing: repository not found"
        in lines
    )
    assert (
        "Prune refused: --prune-untracked is refused: configured repositories did not resolve: "
        "prebid/missing"
    ) in lines
    failed = [line for line in lines if line.startswith("Failed: ")]
    assert len(failed) == 1
    assert failed[0].startswith("Failed: prebid/Prebid.js#1: HydratedIdMismatch: #1: the index ")
    assert failed[0].endswith("(accepted: known [bold]one[/bold])")
    assert "Accepted failure did not fail: prebid/Prebid.js#2 (never fails)" in lines
    assert lines[-1].startswith("Hydrated 1 items; GraphQL points ")
    assert repository.pulls[1].number == 1


def test_an_error_line_is_the_reports_own_markup_and_github_text_is_not() -> None:
    report = SyncReport(
        dry_run=False,
        run_id=7,
        status="failed",
        exit_code=1,
        repositories=(),
        untracked=("prebid/[x]:smile:",),
        planned=(),
        in_place_updates=(),
        deletions=(),
        pruned=(Pruned(9, "prebid/gone", 3),),
        prune_refused=None,
        failures=(),
        accepted_not_failed=(),
        refresh_failures=("LookupConflict: node [y] answered for [z]",),
        second_node_ids=(SecondNodeId(5, "U_stored", "U_[reported]"),),
        items_hydrated=0,
        reclaimed=(3,),
        error="UnexpectedResponse: [/broken] GitHub said :thumbsup:",
    )

    assert rendered(report) == [
        "Run 7: failed, exit 1",
        "Reclaimed stale run 3 (marked aborted)",
        "Untracked: prebid/[x]:smile:",
        "Pruned: prebid/gone (3 items)",
        "Refresh failed: LookupConflict: node [y] answered for [z]",
        "Account 5 reported as node U_[reported], stored as U_stored",
        "Hydrated 0 items; GraphQL points 0, REST calls 0, 0 s",
        "Error: UnexpectedResponse: [/broken] GitHub said :thumbsup:",
    ]


def test_each_entry_prints_on_one_line_with_control_characters_escaped() -> None:
    """A newline in an error page, a tab, a carriage return, and a terminal escape sequence are
    shown as escapes: no entry splits, and no forged line appears."""
    report = SyncReport(
        dry_run=False,
        run_id=7,
        status="complete",
        exit_code=2,
        repositories=(),
        untracked=(),
        planned=(),
        in_place_updates=(),
        deletions=(),
        pruned=(),
        prune_refused=None,
        failures=(),
        accepted_not_failed=(),
        second_node_ids=(),
        refresh_failures=("HTTP 502: <html>\nRun 7: complete, exit 0\r\t\x1b[1A\x1b[2K",),
        items_hydrated=0,
    )

    lines = rendered(report)

    assert lines[1] == (
        "Refresh failed: HTTP 502: <html>\\nRun 7: complete, exit 0\\r\\t\\x1b[1A\\x1b[2K"
    )
    assert lines.count("Run 7: complete, exit 2") == 1
    assert "Run 7: complete, exit 0" not in lines
    assert len(lines) == 3


def test_only_the_error_line_is_red() -> None:
    report = SyncReport(
        dry_run=False,
        run_id=7,
        status="failed",
        exit_code=1,
        repositories=(),
        untracked=(),
        planned=(),
        in_place_updates=(),
        deletions=(),
        pruned=(),
        prune_refused=None,
        failures=(),
        accepted_not_failed=(),
        second_node_ids=(),
        refresh_failures=("Error: not the report's own",),
        items_hydrated=0,
        error="UnexpectedResponse: GitHub answered 400",
    )
    buffer = io.StringIO()
    # no_color=False: NO_COLOR in the environment would otherwise turn off the style tested here.
    console = Console(
        file=buffer, width=200, color_system="standard", force_terminal=True, no_color=False
    )

    print_report(console, report, seconds=0)

    lines = buffer.getvalue().splitlines()
    red = "\x1b[31m"
    (styled,) = [line for line in lines if red in line]
    assert styled.startswith(f"{red}Error: UnexpectedResponse: GitHub answered 400\x1b[0m")
    assert any(line.startswith("Refresh failed: Error: ") for line in lines)


async def test_an_unarchived_repository_is_named(world: World) -> None:
    repository, _ = project(world)
    repository.data["isArchived"] = True
    await world.sync()
    repository.data["isArchived"] = False

    lines = rendered(await world.sync(dry_run=True))

    assert lines[1] == "prebid/Prebid.js: complete (no longer archived)"
