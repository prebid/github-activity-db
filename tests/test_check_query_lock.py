"""The append-only check on tests/query_versions.json and tests/schema_versions.json."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_query_lock.py"
GIT = shutil.which("git")
BASH = shutil.which("bash")
LOCK = "tests/query_versions.json"
SCHEMA_LOCK = "tests/schema_versions.json"


def git(repo: Path, *args: str) -> None:
    assert GIT is not None
    subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "start")
    return tmp_path


def write_lock(repo: Path, entries: dict[str, str]) -> None:
    path = repo / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


def commit(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "lock")


def check(repo: Path, base: str = "main") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_new_entry_passes(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "a" * 64, "pull_request:2": "b" * 64})
    result = check(repo)
    assert result.returncode == 0, result.stderr


def test_a_changed_entry_fails(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = check(repo)
    assert result.returncode == 1
    assert "pull_request:1 changed" in result.stderr


def test_a_removed_entry_fails(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64, "pull_request:2": "b" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:2": "b" * 64})
    result = check(repo)
    assert result.returncode == 1
    assert "pull_request:1 was removed" in result.stderr


def test_a_lock_absent_at_the_base_passes(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    result = check(repo)
    assert result.returncode == 0
    assert "does not exist at main" in result.stdout


def test_an_unknown_base_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    result = check(repo, base="origin/no-such-branch")
    assert result.returncode == 2


def test_a_deleted_lock_removes_every_entry(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).unlink()
    result = check(repo)
    assert result.returncode == 1
    assert "pull_request:1 was removed" in result.stderr


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"pull_request:1": 1}'])
def test_a_malformed_lock_is_an_error_not_a_pass(repo: Path, text: str) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).write_text(text)
    result = check(repo)
    assert result.returncode == 2, result.stderr


def test_an_absolute_lock_path_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main", "--lock", str(repo / LOCK)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "is not one of tests/query_versions.json" in result.stderr


def test_the_checker_runs_isolated_from_site_packages(repo: Path) -> None:
    # CI runs the base copy with -I -S, outside the pull request's environment.
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "--base", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "pull_request:1 changed" in result.stderr


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe not utf-8", b"[" * 100_000 + b"]" * 100_000],
    ids=["not-utf-8", "nested-too-deeply"],
)
def test_an_unreadable_lock_is_an_error_not_a_pass(repo: Path, content: bytes) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).write_bytes(content)
    result = check(repo)
    assert result.returncode == 2, result.stderr


def test_an_integer_past_pythons_digit_limit_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    (repo / LOCK).write_text('{"pull_request:1": ' + "1" * 5000 + "}")
    result = check(repo)
    assert result.returncode == 2, result.stderr


def test_the_verdict_does_not_depend_on_the_working_directory(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    write_lock(repo, {"pull_request:1": "c" * 64})
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main"],
        cwd=repo / "tests",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pull_request:1 changed" in result.stderr


def test_a_malformed_lock_at_the_base_is_an_error_not_a_pass(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / LOCK).write_text("{not json")
    commit(repo)
    write_lock(repo, {"pull_request:1": "a" * 64})
    result = check(repo)
    assert result.returncode == 2
    assert "at main is not JSON" in result.stderr


def test_a_lock_path_outside_the_repository_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main", "--lock", "../query_versions.json"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "is not one of tests/query_versions.json" in result.stderr


def test_without_git_the_check_is_an_error_not_a_pass(repo: Path) -> None:
    write_lock(repo, {"pull_request:1": "a" * 64})
    commit(repo)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={"PATH": str(repo / "no-such-bin")},
        timeout=60,
    )
    assert result.returncode == 2
    assert "git is not on PATH" in result.stderr


def write_schema_lock(repo: Path, entries: dict[str, Any]) -> None:
    path = repo / SCHEMA_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


V1: dict[str, Any] = {"1": [["pull_requests", "number"], ["v_pr_reviewers", "final_verdict"]]}
V2: dict[str, Any] = {
    **V1,
    "2": {
        "columns": [["pull_requests", "number"], ["v_pr_reviews", "state"]],
        "views": {"v_pr_reviews": "a" * 64, "v_github_users": "b" * 64},
    },
}


# Two entries with views, so a view can be edited in one that is not the newest at the base.
V3: dict[str, Any] = {
    **V2,
    "3": {
        "columns": V2["2"]["columns"],
        "views": {"v_pr_reviews": "c" * 64, "v_github_users": "b" * 64},
    },
}


def with_views(views: dict[str, str]) -> dict[str, Any]:
    """V2 with version 2's views replaced."""
    return {**V2, "2": {**V2["2"], "views": views}}


class TestSchemaLock:
    """tests/schema_versions.json: entries, their pairs, and their view digests are kept; only
    the newest entry may gain pairs or views."""

    def test_a_pair_added_to_the_newest_entry_passes(self, repo: Path) -> None:
        write_schema_lock(repo, V1)
        commit(repo)
        write_schema_lock(repo, {"1": [*V1["1"], ["pull_requests", "title"]]})
        result = check(repo)
        assert result.returncode == 0, result.stderr
        assert "tests/schema_versions.json: 1 entries at main kept; 1 entries now" in result.stdout

    def test_an_added_version_passes(self, repo: Path) -> None:
        write_schema_lock(repo, V1)
        commit(repo)
        write_schema_lock(repo, {**V1, "2": [["pull_requests", "number"]]})
        result = check(repo)
        assert result.returncode == 0, result.stderr
        assert "tests/schema_versions.json: 1 entries at main kept; 2 entries now" in result.stdout

    def test_a_pair_added_to_a_superseded_entry_fails(self, repo: Path) -> None:
        """Version 1 describes databases that never had a column version 2 added."""
        write_schema_lock(repo, V1)
        commit(repo)
        write_schema_lock(
            repo,
            {"1": [*V1["1"], ["pull_requests", "title"]], "2": [["pull_requests", "number"]]},
        )
        result = check(repo)
        assert result.returncode == 1
        assert (
            "schema version 1 gained pull_requests.title; only the newest version, 2, gains "
            "columns" in result.stderr
        )

    def test_a_pair_removed_from_a_base_entry_fails(self, repo: Path) -> None:
        write_schema_lock(repo, V1)
        commit(repo)
        write_schema_lock(repo, {"1": [["pull_requests", "number"]]})
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 1 lost v_pr_reviewers.final_verdict" in result.stderr

    def test_a_renamed_pair_fails(self, repo: Path) -> None:
        write_schema_lock(repo, V1)
        commit(repo)
        write_schema_lock(repo, {"1": [["pull_requests", "pr_number"], V1["1"][1]]})
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 1 lost pull_requests.number" in result.stderr

    def test_a_removed_entry_fails(self, repo: Path) -> None:
        write_schema_lock(repo, {**V1, "2": V1["1"]})
        commit(repo)
        write_schema_lock(repo, {"2": V1["1"]})
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 1 was removed" in result.stderr

    def test_a_deleted_lock_removes_every_entry(self, repo: Path) -> None:
        write_schema_lock(repo, V1)
        commit(repo)
        (repo / SCHEMA_LOCK).unlink()
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 1 was removed" in result.stderr

    def test_a_pair_removed_from_an_object_entry_fails(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        write_schema_lock(repo, {**V2, "2": {**V2["2"], "columns": [["pull_requests", "number"]]}})
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 2 lost v_pr_reviews.state" in result.stderr

    def test_a_pair_added_to_the_newest_object_entry_passes(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        columns = [*V2["2"]["columns"], ["pull_requests", "title"]]
        write_schema_lock(repo, {**V2, "2": {**V2["2"], "columns": columns}})
        result = check(repo)
        assert result.returncode == 0, result.stderr

    def test_a_pair_added_to_a_superseded_object_entry_fails(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        columns = [*V2["2"]["columns"], ["pull_requests", "title"]]
        write_schema_lock(repo, {**V2, "2": {**V2["2"], "columns": columns}, "3": V2["2"]})
        result = check(repo)
        assert result.returncode == 1
        assert (
            "schema version 2 gained pull_requests.title; only the newest version, 3, gains "
            "columns" in result.stderr
        )

    @pytest.mark.parametrize(
        ("current", "message"),
        [
            pytest.param(
                {**with_views({**V2["2"]["views"], "v_pr_reviews": "c" * 64}), "3": V2["2"]},
                "schema version 2 changed view v_pr_reviews",
                id="changed digest",
            ),
            pytest.param(
                {**with_views({"v_pr_reviews": "a" * 64}), "3": V2["2"]},
                "schema version 2 lost view v_github_users",
                id="removed view",
            ),
            pytest.param(
                {**V2, "1": [["pull_requests", "number"]], "3": V2["2"]},
                "schema version 1 lost v_pr_reviewers.final_verdict",
                id="removed pair",
            ),
        ],
    )
    def test_an_edit_to_a_superseded_entry_fails(
        self, repo: Path, current: dict[str, Any], message: str
    ) -> None:
        """An entry stays fixed once a newer one is added."""
        write_schema_lock(repo, V2)
        commit(repo)
        write_schema_lock(repo, current)
        result = check(repo)
        assert result.returncode == 1
        assert message in result.stderr

    @pytest.mark.parametrize(
        ("views", "message"),
        [
            pytest.param(
                {**V2["2"]["views"], "v_pr_reviews": "d" * 64},
                "schema version 2 changed view v_pr_reviews",
                id="changed digest",
            ),
            pytest.param(
                {"v_pr_reviews": "a" * 64},
                "schema version 2 lost view v_github_users",
                id="removed view",
            ),
        ],
    )
    def test_a_view_edit_below_the_newest_entry_at_the_base_fails(
        self, repo: Path, views: dict[str, str], message: str
    ) -> None:
        """Views are kept in every base entry, not only in the base's newest one."""
        write_schema_lock(repo, V3)
        commit(repo)
        write_schema_lock(repo, {**V3, "2": {**V3["2"], "views": views}})
        result = check(repo)
        assert result.returncode == 1
        assert message in result.stderr

    def test_a_version_has_at_most_nine_digits(self, repo: Path) -> None:
        write_schema_lock(repo, V1)
        commit(repo)
        write_schema_lock(repo, {**V1, "999999999": []})
        result = check(repo)
        assert result.returncode == 0, result.stderr
        write_schema_lock(repo, {**V1, "1000000000": []})
        result = check(repo)
        assert result.returncode == 2
        assert (
            "schema version '1000000000' is not a positive integer of at most nine digits "
            "without leading zeros" in result.stderr
        )

    def test_a_view_added_to_the_newest_entry_passes(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        write_schema_lock(repo, with_views({**V2["2"]["views"], "v_new": "c" * 64}))
        result = check(repo)
        assert result.returncode == 0, result.stderr

    def test_a_changed_view_digest_fails(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        write_schema_lock(repo, with_views({**V2["2"]["views"], "v_pr_reviews": "c" * 64}))
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 2 changed view v_pr_reviews" in result.stderr

    def test_a_removed_view_fails(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        write_schema_lock(repo, with_views({"v_pr_reviews": "a" * 64}))
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 2 lost view v_github_users" in result.stderr

    def test_an_entry_rewritten_as_a_list_loses_its_views(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        write_schema_lock(repo, {**V2, "2": V2["2"]["columns"]})
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 2 lost view v_github_users" in result.stderr
        assert "schema version 2 lost view v_pr_reviews" in result.stderr

    def test_a_view_added_to_a_superseded_entry_fails(self, repo: Path) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        superseded = {"columns": V1["1"], "views": {"v_late": "d" * 64}}
        write_schema_lock(repo, {**V2, "1": superseded, "3": V2["2"]})
        result = check(repo)
        assert result.returncode == 1
        assert "schema version 1 gained view v_late; only the newest version, 3" in result.stderr

    @pytest.mark.parametrize(
        "entry",
        [
            {"columns": [], "views": {"v": "not-a-digest"}},
            {"columns": [], "views": {"v": "A" * 64}},
            {"columns": [], "views": []},
            {"columns": [], "views": {}, "extra": 1},
            {"views": {}},
        ],
        ids=["short digest", "upper-case digest", "views a list", "extra key", "no columns"],
    )
    def test_a_malformed_view_entry_is_an_error_not_a_pass(
        self, repo: Path, entry: dict[str, Any]
    ) -> None:
        write_schema_lock(repo, V2)
        commit(repo)
        write_schema_lock(repo, {**V2, "2": entry})
        result = check(repo)
        assert result.returncode == 2, result.stderr

    @pytest.mark.parametrize(
        "text",
        [
            '{"1": "pull_requests.number"}',
            '{"1": [["pull_requests"]]}',
            '{"1": [[1, 2]]}',
            "[]",
            '{"1": [], "v2": []}',
            '{"1": [], "02": []}',
            '{"1": [], "1000000000": []}',
        ],
        ids=[
            "string entry",
            "short pair",
            "non-string pair",
            "not an object",
            "version not a number",
            "version with a leading zero",
            "version of ten digits",
        ],
    )
    def test_a_malformed_lock_is_an_error_not_a_pass(self, repo: Path, text: str) -> None:
        write_schema_lock(repo, V1)
        commit(repo)
        (repo / SCHEMA_LOCK).write_text(text)
        result = check(repo)
        assert result.returncode == 2, result.stderr

    @pytest.mark.parametrize(
        ("lock", "text"),
        [
            pytest.param(
                SCHEMA_LOCK,
                '{"1": [["pull_requests", "number"], ["v_pr_reviewers", "final_verdict"]], '
                '"2": {"columns": [], "views": {"v": "' + "c" * 64 + '"}}, '
                '"2": {"columns": [["pull_requests", "number"]], "views": {"v_pr_reviews": "'
                + "a" * 64
                + '", "v_github_users": "'
                + "b" * 64
                + '"}}}',
                id="schema version twice",
            ),
            pytest.param(
                SCHEMA_LOCK,
                '{"1": [["pull_requests", "number"], ["v_pr_reviewers", "final_verdict"]], '
                '"2": {"columns": [], "views": {"v_pr_reviews": "'
                + "c" * 64
                + '", "v_pr_reviews": "'
                + "a" * 64
                + '", "v_github_users": "'
                + "b" * 64
                + '"}}}',
                id="view twice",
            ),
            pytest.param(
                LOCK,
                '{"pull_request:1": "' + "c" * 64 + '", "pull_request:1": "' + "a" * 64 + '"}',
                id="query version twice",
            ),
        ],
    )
    def test_a_key_listed_twice_is_an_error_not_a_pass(
        self, repo: Path, lock: str, text: str
    ) -> None:
        """json.loads keeps the last of two equal keys, which would hide a changed first one."""
        write_lock(repo, {"pull_request:1": "a" * 64})
        write_schema_lock(repo, V2)
        commit(repo)
        (repo / lock).write_text(text)
        result = check(repo)
        assert result.returncode == 2, result.stderr
        assert "listed twice" in result.stderr
        assert "is not JSON" not in result.stderr

    def test_both_locks_are_checked_by_default(self, repo: Path) -> None:
        write_lock(repo, {"pull_request:1": "a" * 64})
        write_schema_lock(repo, V1)
        commit(repo)
        write_lock(repo, {"pull_request:1": "c" * 64})
        write_schema_lock(repo, {"1": []})
        result = check(repo)
        assert result.returncode == 1
        assert "pull_request:1 changed" in result.stderr
        assert "schema version 1 lost pull_requests.number" in result.stderr

    def test_lock_selects_one(self, repo: Path) -> None:
        write_lock(repo, {"pull_request:1": "a" * 64})
        write_schema_lock(repo, V1)
        commit(repo)
        write_lock(repo, {"pull_request:1": "c" * 64})
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", "main", "--lock", SCHEMA_LOCK],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "query_versions" not in result.stdout + result.stderr

    def test_the_repository_locks_are_read_by_their_rules(self, repo: Path) -> None:
        """The real lock files parse under the rule the checker gives each, in a scratch
        repository whose base commit holds them."""
        root = SCRIPT.parent.parent
        for lock in (LOCK, SCHEMA_LOCK):
            (repo / lock).parent.mkdir(parents=True, exist_ok=True)
            (repo / lock).write_text((root / lock).read_text())
        commit(repo)
        result = check(repo)
        assert result.returncode == 0, result.stderr
        for lock in (LOCK, SCHEMA_LOCK):
            entries = len(json.loads((root / lock).read_text()))
            assert f"{lock}: {entries} entries at main kept; {entries} entries now" in result.stdout


# --------------------------------------------------------------------------------------------
# The CI step itself, run against shallow merge checkouts like the one actions/checkout makes
# --------------------------------------------------------------------------------------------
WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "check.yml"
STEP = "Hydration and schema version lock entries are kept"


def lock_step_script() -> str:
    """The step's ``run:`` script from check.yml, with this interpreter for the project's."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {STEP}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    script = "\n".join(body)
    # The checker runs isolated from the pull request's environment.
    invocation = 'uv run --no-sync python -I -S "$checker" --base "$base"'
    assert script.count(invocation) == 1, script
    assert script.count("uv run") == 1, script
    return script.replace("uv run --no-sync python", sys.executable)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    return bare


def work_clone(tmp_path: Path, origin: Path, name: str) -> Path:
    work = tmp_path / name
    git(tmp_path, "clone", "-q", f"file://{origin}", str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "test")
    return work


def push_main(work: Path, *, checker: bool, entries: dict[str, str] | None) -> None:
    if checker:
        (work / "scripts").mkdir(exist_ok=True)
        (work / "scripts" / "check_query_lock.py").write_text(SCRIPT.read_text())
    if entries is not None:
        write_lock(work, entries)
    (work / "README").write_text("base\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main")


def run_step_on_merge(
    tmp_path: Path, origin: Path, work: Path, change
) -> subprocess.CompletedProcess[str]:
    """Commit ``change`` on a PR branch, push its merge into main as a ref, and run the step
    in a depth-1 checkout of that merge, as a pull_request run does."""
    git(work, "checkout", "-q", "-b", "pr")
    change(work)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "pr")
    git(work, "checkout", "-q", "main")
    git(work, "merge", "-q", "--no-ff", "pr", "-m", "merge")
    git(work, "push", "-q", "origin", "HEAD:refs/pull/1/merge")
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "-q", str(checkout))
    git(checkout, "remote", "add", "origin", f"file://{origin}")
    git(checkout, "fetch", "-q", "--depth=1", "origin", "refs/pull/1/merge")
    git(checkout, "checkout", "-q", "FETCH_HEAD")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    return run_step(checkout, runner_temp)


def run_step(checkout: Path, runner_temp: Path) -> subprocess.CompletedProcess[str]:
    """The step under ``bash -e``, as GitHub Actions runs a ``run:`` script."""
    assert BASH is not None
    return subprocess.run(
        [BASH, "-e", "-c", lock_step_script()],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
        timeout=60,
    )


def edit_entry(work: Path) -> None:
    write_lock(work, {"pull_request:1": "c" * 64})


def edit_entry_and_relax_checker(work: Path) -> None:
    edit_entry(work)
    checker = work / "scripts" / "check_query_lock.py"
    checker.write_text("raise SystemExit(0)\n")


def test_the_step_fails_an_edited_entry(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})
    result = run_step_on_merge(tmp_path, origin, work, edit_entry)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pull_request:1 changed" in result.stderr


def test_the_step_runs_the_base_checker_not_the_pull_requests(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})
    result = run_step_on_merge(tmp_path, origin, work, edit_entry_and_relax_checker)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "runner-temp/check_query_lock.py" in result.stdout


def test_the_step_passes_a_new_entry(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})

    def add_entry(target: Path) -> None:
        write_lock(target, {"pull_request:1": "a" * 64, "pull_request:2": "b" * 64})

    result = run_step_on_merge(tmp_path, origin, work, add_entry)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_step_uses_the_pull_requests_checker_where_the_base_has_none(
    tmp_path: Path, origin: Path
) -> None:
    # The first pull request with the lock: the base has neither the checker nor the lock.
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=False, entries=None)

    def add_lock(target: Path) -> None:
        (target / "scripts").mkdir(exist_ok=True)
        (target / "scripts" / "check_query_lock.py").write_text(SCRIPT.read_text())
        write_lock(target, {"pull_request:1": "a" * 64})

    result = run_step_on_merge(tmp_path, origin, work, add_lock)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "running scripts/check_query_lock.py" in result.stdout
    assert "does not exist at" in result.stdout


def test_the_step_fails_a_pair_removed_from_a_schema_entry(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    write_schema_lock(work, V1)
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})

    def remove_pair(target: Path) -> None:
        write_schema_lock(target, {"1": V1["1"][:1]})

    result = run_step_on_merge(tmp_path, origin, work, remove_pair)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "schema version 1 lost v_pr_reviewers.final_verdict" in result.stderr


def test_the_step_fails_without_a_merge_commit(tmp_path: Path, origin: Path) -> None:
    work = work_clone(tmp_path, origin, "work")
    push_main(work, checker=True, entries={"pull_request:1": "a" * 64})
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", "--depth=1", f"file://{origin}", str(checkout))
    result = run_step(checkout, tmp_path)
    assert result.returncode != 0
