# Redesign Plan

This is the plan of record for rebuilding the GitHub Activity DB as the data layer for the Prebid
activity dashboard. It records the decisions, the design, the execution steps, and the evidence each
design choice rests on. Update the step status table as work lands; do not rewrite history in it.

Pre-Step-1 facts were measured against `main` at `43fcf06`, Step 1 facts on the PR #5 branch (merged
as `8c5b16b`), and live GitHub on 2026-10-16. On 2026-10-16 the whole plan was audited against live
GitHub, GitHub's documentation, the installed libraries, and `main`, and corrected. On 2026-10-17
Step 3 was planned against `main` at `c1f474b`, and the library behaviours it relies on were probed
with the installed versions (section 7). Counts that drift (PR and issue totals) are dated;
regenerate them with `ghactivity verify` once Step 3 lands.

## Contents

1. [Scope](#1-scope)
2. [Decisions](#2-decisions)
3. [Coverage rule](#3-coverage-rule)
4. [Gap register](#4-gap-register)
5. [Design](#5-design)
6. [Execution steps](#6-execution-steps)
7. [Evidence](#7-evidence)
8. [Open items](#8-open-items)
9. [Later work](#9-later-work)

## Step status

| Step | Title | Status |
|------|-------|--------|
| 1 | Guardrails | done (PR #5) |
| 2 | GitHub source layer | done (PR #7) |
| 3 | New schema, PR sync engine, cutover (pull requests 3a and 3b) | not started |
| 4 | Issues and closing-issue links | not started |
| 5 | Full bootstrap and verification | not started |
| 6 | Scheduled sync and publication | not started |
| 7 | Documentation rewrite | not started |

## 1. Scope

In scope: the database, the sync that fills it, its verification, and its scheduled publication.

Out of scope until this plan completes: the dashboard application (`prebid/repo-activity-dashboard`)
and replacing its current data layer, a query API, AI enrichment, company affiliation, CI check
status, releases, and repositories not in the tracked list.

## 2. Decisions

| ID | Decision |
|----|----------|
| D1 | Scope is the database only (section 1). |
| D2 | Coverage rule B (section 3). |
| D3 | Pull requests closed without merge are not stored. A tracked PR that closes without merge is deleted; a reopened PR is stored again on the next run. |
| D4 | Requested reviewers are not stored. A reviewer is a user who submitted at least one non-pending review (APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED). The PR author and automation accounts are excluded from reviewer metrics. |
| D5 | Conversation comments (the PR `comments` connection; inline review comments already belong to reviews) are stored one row per comment with its author (D24). Comment text is not stored. A user who submitted a review on a PR is a reviewer there, and their conversation comments count toward that review activity: `v_pr_reviewers` carries their comment count, and `v_pr_commenters` lists only commenters who submitted no review on that PR. |
| D6 | No review or comment timestamps. Stored dates: PR created and merged; issue created and closed; commit authored (the git author date, set by the committer's client). Review order comes from the review id. |
| D7 | No timeline history. The database holds the current state of each item. |
| D8 | Labels are stored as currently applied, with no history. Labels are meaningful while a PR is open. |
| D9 | The database file is published as a GitHub release asset. Data from the private tracked repository (`prebid-agent-skills`) is included. The release is in the public repository `prebid/github-activity-db`, so every asset is world-readable, private-repository rows included; that is accepted (the data is not proprietary, and the private repositories are expected to become public). |
| D10 | The scheduled sync authenticates as a GitHub App installed on the `prebid` organization. |
| D11 | The scheduled sync runs every 6 hours. |
| D12 | `classify_tags` and `ai_summary` are removed. AI enrichment returns later as a separate table with provenance. |
| D13 | No repositories are added. CI check status and releases are out of scope. |
| D14 | Assignees and requested teams are not stored. This is a scope choice: assignment events can land after `updatedAt` (2 of 268 observed, each within a minute), a lag the unsettled rule in 5.3 would absorb. |
| D15 | Commit co-authors are not stored. Requesting them inside the commit list raises GraphQL cost from 2 to 27 points for the same 25 PRs. |
| D16 | The request-pacing stack (`github/pacing/*`) is retired at cutover. The new sync makes about 400 index calls per run (about 800 with the online verify) plus hydration, and reads its budget from each response. |
| D17 | `user_tags` and `pr_user_tags` stay (empty), re-pointed at the new `pull_requests` table. The `search` and `tags` CLI stubs are removed. Until a tag producer exists, a close, reopen, or rebuild removes a PR's tags; a producer must first meet the identity rule in section 9. |
| D18 | The database is rebuilt from GitHub, not migrated row by row. The current file holds nothing that cannot be re-fetched (0 user tags, 0 sync failures, 0 AI values). |
| D19 | PR and issue bodies are stored. |
| D20 | SQLite stays in rollback-journal mode while `github_activity.db` is tracked in git. WAL mode persists in a file once set, so `scripts/check_db_journal.py` checks the tracked file's header as a pre-commit hook and a test. The connection listener does not set the journal mode: that statement needs a lock, so every connection would fail with "database is locked" while another process has a WAL database open. In WAL mode committed transactions can sit in a git-ignored `*-wal` file until a checkpoint, and SQLite documents separating a database from its journal as a corruption path. WAL is reconsidered in Step 6, after the database leaves git. |
| D21 | `tracking.toml` ships inside the package (`src/github_activity_db/tracking.toml`) and is read through `importlib.resources`, so an installed wheel and a checkout read the same list. `TRACKING_CONFIG_PATH` names a different file. |
| D22 | The CI test gate allows no skipped tests (`scripts/check_junit.py --max-skipped 0`). A test that must skip in CI needs the allowance raised in the workflow, in the same change. pytest reports `xfail` as skipped, so the same applies to an `xfail`. |
| D23 | Closing-issue links are read by the index as well as by hydration, and the stored links are compared with the index on every run, so a link added after merge, which does not change the PR's `updatedAt`, is captured (5.3 step 3). |
| D24 | Conversation comments are stored one row per comment, its `github_id` and author and never its text, so a comment by an account the database never stored is a row with a null author, never dropped (D28). |
| D25 | A run, scheduled or manual, publishes when its starting database is valid (the downloaded snapshot passed `verify --db-only`, or `bootstrap` started from an empty database), sync exited 0 or 2, and verify exited 0 (no error, 5.4). The manifest records the sync exit code and verify's counts per class. A final job that runs whatever the others did then marks the workflow failed if sync exited 2, verify failed, or nothing was published, so a failure is visible after the data is saved. |
| D26 | The Step 5 database is not committed; it becomes the first snapshot of release `db` (Step 6b). From Step 3 until 6c removes it, the tracked `github_activity.db` keeps its pre-Step-3 content: the default `DATABASE_URL` moves to a git-ignored file, and `scripts/check_frozen_db.py` fails when the file's SHA-256 differs from the digest it pins, as a pre-commit hook on the file (so CI's `--all-files` run checks it) and as a test. |
| D27 | A stored repository no longer listed in `tracking.toml` is reported on every run (exit 2), and its rows stay until `ghactivity sync --prune-untracked` deletes them; the prune is recorded in `sync_runs`. |
| D28 | The database keeps a person attached to everything it attributed to them. A stored account stays stored after GitHub deletes it, with its last known login and type, and every reference to it stays: when GitHub later returns that actor as null, as a re-hydration after the deletion does, the writer keeps the stored reference of the same object (the PR's author and merger, the issue's author, the review or comment with the same `github_id`, and this PR's commit with the same SHA). `deleted_at` records that a lookup found the account deleted; it changes no reference, the 24-hour refresh skips the account, and a later sighting of the account clears it. An actor the database cannot attribute (GitHub returns null and the object has no stored reference) is represented as deleted, one way everywhere: a null reference, and no `github_users` row for an account never stored. A non-null actor GitHub returns replaces the stored one; a commit link whose account lookup returns null counts as null. Deleted accounts are identified as stored ones by `deleted_at IS NOT NULL`, and unattributed ones by a null actor reference on a PR, issue, review, or comment; on a commit a null link also means an email linked to no account, so commits do not single out deleted accounts. The attribution lives in the stored rows and cascades with them, so anything that re-creates a row from GitHub drops it for accounts deleted before then: a bootstrap (it starts from an empty file), a PR reopened after D3 deleted it, a transferred issue (new `github_id`), and a repository pruned and re-added (D27). |

## 3. Coverage rule

Floor `F = 2023-01-01T00:00:00Z`, set in `tracking.toml` `[coverage]` (added in Step 3). The loader
rejects a floor that is not a TOML offset date-time and converts it to UTC.

- A pull request is in coverage when its state is `OPEN`, or its state is `MERGED` and `mergedAt >= F`.
  (`mergedAt >= createdAt` always holds, so "created or merged on or after F" reduces to this.)
- An issue is in coverage when its state is `OPEN`, or `createdAt >= F`, or `closedAt >= F`.

Every input to the rule is immutable once set except state and `closedAt`, so an item does not enter
coverage because of housekeeping. The alternative rule "updated on or after F" admitted 567 extra PRs
and 22 extra issues whose only post-floor activity was events such as branch deletion (one branch
cleanup on 2023-07-05 touched 150 Prebid.js PRs created before 2023, some from 2016).

Coverage on 2026-10-16: 11,848 pull requests and 4,857 issues across the 18 tracked repositories.

## 4. Gap register

Every gap found in the audit, and where it is resolved.

| # | Gap | Resolution |
|---|-----|------------|
| 1 | No issues table or issue sync | Step 4 |
| 2 | No GitHub user id or account type; bots indistinguishable | Step 3: `github_users` keyed by GitHub id; type and login from GraphQL actor sightings and node lookups (5.2); automation from account type plus the `[automation]` list. `prebidjs-release` (99054218) and `claude` (81847) are type `User`. Logins change: `blueseasx` became `blueseasx-prebid`. |
| 3 | Review order lost; final reviewer state unknown | Step 3: one row per review keyed by review id; `v_pr_reviewers` derives the final verdict |
| 4 | Validation silently drops commits (`author: {}`), reviews (`user: null`), files (`sha: null`) | Steps 2, 3: no silent skips; failures are recorded. GraphQL file objects carry no `sha`; unlinked commit authors keep git name and email; deleted-account comments are stored with a null author unless their author was already stored (D24, D28) |
| 5 | 10,895 PRs with file status `unknown` and zero per-file line counts | Step 5: rebuild from GitHub |
| 6 | Closed-unmerged PRs partially stored | D3 |
| 7 | `reviewers` holds pending review requests | D4 |
| 8 | Commenters not captured | Inline review comments already belong to reviews (168 of 168 sampled). Conversation commenters: D5, D24 |
| 9 | No review timestamps | D6 |
| 10 | Commit lists truncated at 250 | Step 2: compare endpoint, paged, count asserted |
| 11 | Coverage floor undefined; partial history | Section 3; `verify` |
| 12 | AI fields empty and erased by re-sync | D12 |
| 13 | Draft and accuracy fields discarded | Step 3: `is_draft`, `base_ref`, `targets_default_branch`, `author_association`, `is_cross_repository`, automation flags, per-file stats, per-connection totals |
| 14 | Timeline | D7 |
| 15 | Users table | Step 3 |
| 16 | Repository identity by name; tracked list hardcoded | Steps 1, 3: repository GitHub id; config file |
| 17 | PR to closing-issue links | Steps 3, 4; compared on every index (D23) |
| 18 | No scheduled sync | Step 6 |
| 19 | No sync run record | Step 3: `sync_runs`, `repo_sync_state` |
| 20 | API cost (4+ calls per PR before skip checks) | Steps 2, 3: full index plus batched GraphQL |
| 21 | `sync_failures` unique key raises on a second resolve | Step 3: one row per item |
| 22 | One `AsyncSession` shared by concurrent tasks | Step 3: single writer with its own session |
| 23 | Open-PR sweep refetches every open PR each run | Replaced by the full index |
| 24 | Child data stored as JSON blobs | Step 3: normalized tables |
| 25 | Missing indexes | Step 3 |
| 26 | `foreign_keys` off; busy timeout only from the driver default (Python `sqlite3`, 5 s); journal mode not set, and WAL persists in the file once set | Step 1: `foreign_keys=ON` and `busy_timeout=30000` on every connection; a test checks the tracked file's header for rollback mode (D20) |
| 27 | Model and migration drift (`alembic check` fails) | Step 1 |
| 28 | No query layer | Later (section 9) |
| 29 | 14-day freeze after merge | Removed in Step 3 |
| 30 | Documentation inaccurate | Every step; Step 7 |
| 31 | Bulk sync commits nothing until a repository finishes | Step 3: per-item transactions; crash-resume test |
| 32 | No CI; pre-commit hooks not installed; two ruff versions disagree | Step 1 |
| 33 | Existing file misses in-rule PRs and holds stale logins | Step 5 rebuild |
| 34 | GitHub search caps at 1,000 results and omits items | `verify` uses connection totals and set equality, never search |
| 35 | githubkit `graphql.arequest` raises on any GraphQL error, partial results included | Step 2: `GitHub.arequest` (5.7) |
| 36 | `gh release upload --clobber` deletes before uploading | Step 6 |
| 37 | Public-repo schedules disable after 60 days without activity; start-of-hour runs delayed or dropped | Step 6 |
| 38 | The same path can appear twice in one PR's file list | Step 3: file rows keyed by ordinal; `files_total`, not `changed_files`, counts them |
| 39 | Null PR author, reviewer, commenter, issue author, and merger occur | Step 3: nullable references, kept for stored accounts (D28); deleted-account comments stored with a null author (D24) unless their author was already stored (D28) |
| 40 | A review can be submitted seconds after the PR `updatedAt` | Step 3: unsettled re-hydration |
| 41 | Issue transfers between repositories | Step 4: a transferred issue arrives with a new id; deletions recomputed at delete time |
| 42 | `uv sync --frozen` without `--extra dev` uninstalls dev tools | Step 1 |
| 43 | Pre-commit `type: ignore` and `noqa` audits always exit 0 | Step 1 |
| 44 | `get_logger` returns a loguru logger (`{}` formatting), but 51 of the 56 `logger.<level>(...)` calls in `src/` use `%s`-style placeholders, so their arguments are dropped (a failed rate-limit fetch logs a literal `Failed to fetch rate limits: %s`) | Step 3: all 51 are under `github/`, which is removed at cutover (5.7); new code uses `{}` placeholders |
| 45 | `tracking.toml` was read from a path three directories above `config.py`; a non-editable install has no such file and every command reading `Settings.tracked_repos` fails | Step 1 (D21); the built-wheel CI job runs the installed package outside the checkout |
| 46 | Foreign-key enforcement was tested only on engines built directly with `build_engine()`; a `get_engine()` that bypassed it left every application session at `foreign_keys=0` with the suite green | Step 1: tests through `get_session()` |
| 47 | The JUnit gate passed a run in which every test was skipped (pytest exits 0) and read only the first `<testsuite>` | Step 1 (D22) |
| 48 | The suppression caps missed spellings ruff and mypy honor (bare `# noqa`, `# type:ignore[...]`, file-level `# ruff: noqa`, `# mypy: ignore-errors`) and counted directive text inside strings | Step 1: comment-token counter; ruff `PGH004` |
| 49 | An empty `TRACKING_CONFIG_PATH` became the working directory, and a directory or unreadable file raised a raw `OSError` | Step 1: empty means the packaged file; read errors raise `TrackingConfigError` |
| 50 | `actions/checkout` left the job token in `.git/config` while the built-wheel job ran build-backend code from PyPI | Step 1: `persist-credentials: false` on every checkout |
| 51 | `.env.example`, which the setup instructions copy, was deleted in `7899671` | Step 1: restored |
| 52 | The migration test passed while migrating `DATABASE_URL` (by default the tracked database) if `alembic/env.py` ignored its URL override; two `tracking.toml` guards (unknown top-level keys, empty values) had no test | Step 1: decoy URL and head-revision check; guard tests |
| 53 | Suppression counting missed `.pyi` files, `@no_type_check`, and `# isort: skip_file` / `# isort: off`; the Any-alias hook missed `type X = Any` | Step 1 |
| 54 | `check-added-large-files` checked nothing in CI: with `--all-files` no file is newly added | Step 1: `--enforce-all`; `github_activity.db` exempt until Step 6 |
| 55 | Build inputs not pinned: the hatchling build backend and its dependencies, `pre-commit-hooks` by tag, wheel-job dependencies installed without hashes | Step 1: hatchling pinned in `pyproject.toml` and, with its dependencies, hash-pinned in `build-constraints.txt` (a test keeps the two pins equal); `pre-commit-hooks` by SHA; `--require-hashes` installs |
| 56 | The tracked database's journal mode was checked only by pytest: a WAL-mode `github_activity.db` passed every pre-commit hook | Step 1: `scripts/check_db_journal.py` pre-commit hook on the file; the test runs the same script |
| 57 | `uv sync` built the editable project with hatchling's dependencies and `editables` resolved fresh | Step 1: `[tool.uv] build-constraint-dependencies` pins all seven, and `uv lock` copies them into uv.lock's `[manifest]`, which `uv sync --frozen` reads; a test keeps all four pin sites equal and CI runs `uv lock --locked` |
| 58 | The Any-alias check missed `Optional[Any]`, `Union[..., Any]`, and `Annotated[Any, ...]`; `None | Any` and a `typing_extensions` alias of `no_type_check` had no test | Step 1 |
| 59 | The Any-alias check matched spellings, not imported objects: `from typing import Any as Dynamic` then `type Foo = Dynamic` passed, and a module's own `class Any` was flagged | Step 1: names resolved through the module's `typing` / `typing_extensions` imports |
| 60 | The wheel smoke check rejected only the uppercase `TRACKING_CONFIG_PATH`, while `Settings` reads environment variables case-insensitively, so a lowercase override could stand in for a missing packaged file | Step 1: the check rejects a non-empty `tracking_config_path` on the `Settings` instance it reads from |
| 61 | The Any-alias check's import map included imports from function and class bodies, so a function-local `from typing import cast as Dynamic` hid a module-level alias to `Any`, and a function-local `from typing import Any` flagged a module's own `class Any` | Step 1: imports and rebindings are followed in module scope only, in source order, merging branches so a name that may be `typing.Any` counts |
| 62 | The Any-alias check missed string forward references (`Name: TypeAlias = "Any"`, `Optional["Any"]`), aliases in module-level `try`/`with`/loop/`match` blocks and non-`TYPE_CHECKING` `if` blocks, and `t = typing` copies; `x[k] = ...` and `x.attr = ...` counted as rebinding `x` | Step 1: forward references parsed where a type is expected; aliases flagged wherever a type checker reads them in module scope, including an `if TYPE_CHECKING:` body, and not in the runtime-only branch (the `else` of `if TYPE_CHECKING:`); a quoted `"TypeAlias"` annotation is recognized; assignment copies followed; only stored names rebind |
| 63 | The suppression counter missed `from <module> import no_type_check as X` when the module re-exports typing's (mypy follows the re-export) | Step 1: such aliases are collected from any module |
| 64 | Documented nested environment overrides (`SYNC__*`, `PACING__*`, `LOGGING__*`) have no effect: `Settings` sets no `env_nested_delimiter` | The plan audit removed the `SYNC__*` rows from `CLAUDE.md` and the `docs/database.md` example and marked the `PACING__*` table; Step 3 removes `Settings.rate_limit`, `Settings.pacing`, and `Settings.sync` with the pipeline, sets `env_nested_delimiter="__"` on `Settings.model_config`, and tests that `LOGGING__ROTATION` overrides `Settings().logging.rotation` |
| 65 | `docs/development.md` said `build_engine` sets the rollback journal; `docs/roadmap.md` Phases 2-3 contradict this plan; other docs describe removed or nonexistent modules, commands, and counts | The plan audit corrected `docs/development.md` and marked `docs/roadmap.md` as history; Step 7 rewrites the rest |
| 66 | The rollback-journal guard does not see a hot `-journal` beside the tracked file, which a commit would separate from it | Step 3 pins the tracked file's content (D26), so CI rejects any change to it; 6c removes it |
| 67 | The JUnit skip allowance (D22) is a count, so once raised any test may use it | Accepted while the allowance is 0; the first raised allowance names its tests instead |
| 68 | `scripts/count_suppressions.py` does not count ruff's `# ruff: disable[...]` range suppression, which ruff 0.14.11 honors only in preview | Dormant while `preview` is off (`pyproject.toml` sets none). Resolved before Step 3: the counter counts each `# ruff: disable` in any letter case, never `# ruff: enable`, with tests for each honored spelling, a string decoy, and an `enable` decoy (section 7, "Range suppressions") |
| 69 | click exits 2 on every usage error (an unknown option or command, an out-of-range value), the code D25 publishes on, so a mistyped workflow input would publish the unchanged database | Step 3a: a run that never starts exits 1 (5.3 step 8) |
| 70 | Under alembic's SQLite defaults each revision commits on its own and schema changes are not transactional: a revision that fails after dropping a table leaves it dropped, with the previous revision stamped. A statement executed on the migration connection before `context.configure()` makes alembic treat the transaction as external and never commit: `upgrade` exits 0 with no revision stamped | Step 3a: one transaction per upgrade, nothing executed before `context.configure()` (5.1) |
| 71 | A batch alter of a table that a view reads fails ("error in view ...: no such table") | Step 3a: migrations drop and recreate the dependent views (5.1) |
| 72 | Batch recreate drops unnamed CHECK constraints; SQLAlchemy emits no CHECK for `Boolean` or `Enum` on SQLite; `alembic check` compares neither | Step 3a: every CHECK named, through a naming convention; the 5.1 comparison test compares CHECK names |
| 73 | An explicit `DATABASE_URL` naming the tracked file (the `.env` line `docs/development.md` shows) is migrated without complaint: the tag-row guard passes on its 0 tag rows | Step 3a: `alembic/env.py` refuses the repository-root `github_activity.db` (5.1, D26) |
| 74 | Nothing stops `sync` or `verify` from running against an older revision or an empty file (the new default URL creates one on first connect), and the wheel ships no `alembic/` directory (`pyproject.toml` packages only `src/github_activity_db`), so the installed code cannot read the revision chain | Step 3a: a packaged tuple of revision ids, kept equal to the chain by a test; Step 3b: `sync` and online `verify` exit 1 unless the database is at head (5.3, 5.4) |
| 75 | typer prints local variable values in tracebacks by default and `cli/app.py` does not turn it off; every loguru handler `setup_logging` adds shows them too (both stderr handlers set `diagnose=True`, and the optional file handler takes loguru's default, also true). Step 6 runs the CLI in a public repository's Actions logs, which mask only registered secrets | Step 3a: typer's `pretty_exceptions_show_locals` and `diagnose` on all three handlers off (5.7) |
| 76 | `source/` imports httpx directly (`client.py`, `transport.py`), but `pyproject.toml` does not declare it; it is installed only as a githubkit dependency | Its own pull request before Step 3a |
| 77 | In rollback-journal mode a read left half-consumed on another connection blocks a commit until the busy timeout, which then raises "database is locked"; the comment on `SQLITE_BUSY_TIMEOUT_MS` in `db/engine.py` says a lock fails the item. Through the aiosqlite dialect a query fetches every row and closes its cursor inside `execute`, so only a server-side cursor leaves a result half-read; a reader in another process (an open `sqlite3` shell) can still hold the lock | Step 3b: a database error in the writer is fatal (5.3 step 4); no code in `src/` reads through a server-side cursor (`stream`, `stream_scalars`, `stream_results=True`, `yield_per`); the comment is corrected |
| 78 | `source/users.py` checks a looked-up node's id against the requested node id, but not its `databaseId` against the commit link's, which only `sync/` holds | Step 3b: a mismatch fails the items waiting on the link (5.2) |
| 79 | Hydration raises `RepositoryNotFound` or `RepositoryShapeError` when a batch's `repository` is null; 5.3 assigned repository failures only to resolve and index | Step 3b: a row of the 5.3 step 4 table |
| 80 | Gap 44's rule (new code uses `{}` placeholders) has no gate, and ruff cannot be one: PLE1205 flags a correct `logger.warning("brace {}", value)` and passes `logger.warning("percent %s", value)` | Step 3a: an AST check over `src/`, run as a pre-commit hook (so CI's `--all-files` run checks it) and tested with planted calls and a string decoy |

## 5. Design

### 5.1 Schema

Entity tables (`repositories`, `github_users`, `pull_requests`, `pr_reviews`, `pr_comments`, `issues`,
`sync_runs`, `sync_failures`) have their own integer primary key; GitHub identifiers there are unique columns,
never primary keys. Child tables use the composite keys listed. References to `repositories`,
`pull_requests`, and `issues` use `ON DELETE CASCADE`; references to `github_users` use
`ON DELETE SET NULL`.
Application connections enforce foreign keys. Migrations run with `foreign_keys=OFF`, because
alembic's SQLite batch mode drops and recreates tables and an enforced drop cascades into child rows.
The migration engine's connect listener sets it on the DBAPI connection, before any transaction
opens (the pragma has no effect inside one), and raises unless `PRAGMA foreign_keys` then returns 0;
it does both before it turns off the driver's own transaction handling, because putting the
connection into autocommit (`isolation_level = None`) commits a transaction already open on it
(section 7, "Isolation level").
Nothing executes on the SQLAlchemy connection before `context.configure()`: a statement there makes
alembic treat the transaction as external and never commit (gap 70). An upgrade is one transaction:
the listener turns off the driver's own transaction handling, a `begin` listener emits `BEGIN`, and
`context.configure()` gets `transactional_ddl=True`, so a failed revision leaves the file at the
revision the upgrade started from, with no revision of that upgrade applied. `PRAGMA
foreign_key_check` runs inside that transaction after the last revision and raises on any row,
before the commit. A migration that batch-alters a table a view reads drops those views first and
recreates them afterwards from its own frozen SQL; `legacy_alter_table` stays off, so a view left
in place fails the migration (gap 71). Migrations import only alembic and SQLAlchemy, never
application code, and name every index, CHECK constraint, and foreign key they create through
`op.f()`: env.py passes
`Base.metadata` as `target_metadata`, and alembic applies its naming convention to every migration's
operations, old revisions included (on a fresh build, the unnamed foreign keys of `01421d8dfaeb`
and `a2b3c4d5e6f7` take the head convention's names, which is harmless only because Step 3 drops
those tables). The convention does not change after Step 3's migration.
`downgrade()` raises (5.4); offline `--sql` mode is refused, because these checks need a live
connection. `alembic/env.py` refuses to migrate the repository-root
`github_activity.db` while D26 freezes it (gap 73).

`Base.metadata` names indexes, CHECK constraints, and foreign keys by convention, and every CHECK has
a name: batch recreate keeps named CHECKs and drops unnamed ones (gap 72). Migrations pass names
through `op.f()`, which keeps the convention from prefixing a given name twice. Unique constraints
stay out of the convention, which would otherwise report the kept `user_tags`' unnamed `UNIQUE` to
`alembic check`. Columns are NOT NULL unless this section names them nullable, the source record's
value is optional, or the column has no value until a later write: `sync_runs.finished_at`,
`exit_code`, and `halt_reason` (null while the run is `running`), and every `repo_sync_state` column
except `repository_id`, `last_run_id`, and `last_outcome` (`index_issue_total` stays null until
Step 4 indexes issues). The kept tag tables keep their current nullability (`user_tags.description`
and `color`, `pr_user_tags.created_at`).

| Table | Columns | Keys and constraints |
|-------|---------|----------------------|
| `repositories` | `github_id`, `owner`, `name`, `full_name`, `repo_group`, `is_private`, `is_archived`, `default_branch`, `is_tracked` | `github_id` unique; names take GitHub's spelling; `full_name` indexed, not unique (the consumer key (`full_name`, `number`) is unique among `is_tracked` rows); `is_tracked` set by 5.3 step 1 (D27) |
| `github_users` | `github_id`, `node_id`, `login`, `type`, `refreshed_at`, `deleted_at` | `github_id` unique; `node_id` unique; `type` and `login` not null, from GraphQL only (5.2); `type` in (`User`, `Bot`, `Organization`, `Mannequin`); `refreshed_at` set on insert and by every lookup (5.2); `deleted_at` null unless a lookup found the account deleted, and no reference reads it (D28) |
| `pull_requests` | `repository_id`, `number`, `github_id`, `node_id`, `state`, `is_draft`, `title`, `body`, `author_id`, `author_association`, `created_at`, `merged_at`, `merged_by_id`, `base_ref`, `targets_default_branch`, `is_cross_repository`, `additions`, `deletions`, `changed_files`, `commits_total`, `files_total`, `reviews_total`, `comments_total`, `labels_total`, `closing_issues_total`, `github_updated_at`, `hydrated_at`, `hydration_version` | unique (`repository_id`, `number`); `github_id` unique; `state` in (`OPEN`, `MERGED`); `merged_at` set iff `MERGED`; author and merger nullable |
| `pr_labels` | `pr_id`, `name` | PK (`pr_id`, `name`) |
| `pr_reviews` | `github_id`, `pr_id`, `reviewer_id`, `state` | `github_id` unique; reviewer nullable; `state` in (`APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`) |
| `pr_comments` | `github_id`, `pr_id`, `author_id` | `github_id` unique; author nullable; no comment text (D24, D28) |
| `pr_commits` | `pr_id`, `sha`, `author_id`, `author_name`, `author_email`, `authored_at` | PK (`pr_id`, `sha`); author nullable |
| `pr_files` | `pr_id`, `ordinal`, `path`, `change_type`, `additions`, `deletions` | PK (`pr_id`, `ordinal`) |
| `pr_closing_issues` | `pr_id`, `issue_repo_github_id`, `issue_repo_full_name`, `issue_number` | PK (`pr_id`, `issue_repo_github_id`, `issue_number`) |
| `issues` | `repository_id`, `number`, `github_id`, `node_id`, `state`, `state_reason`, `title`, `body`, `author_id`, `created_at`, `closed_at`, `labels_total`, `github_updated_at`, `hydrated_at`, `hydration_version` | unique (`repository_id`, `number`); `github_id` unique; `state` in (`OPEN`, `CLOSED`); author nullable |
| `issue_labels` | `issue_id`, `name` | PK (`issue_id`, `name`) |
| `automation_accounts` | `github_id`, `login`, `reason` | integer primary key; `github_id` unique; replaced from `tracking.toml` at the start of every run |
| `automation_emails` | `email`, `reason` | integer primary key; `email` unique, stored lowercase; replaced the same way |
| `sync_runs` | `started_at`, `deadline_at`, `finished_at`, `status`, `exit_code`, `trigger`, `code_version`, `graphql_points`, `rest_calls`, `items_planned`, `items_hydrated`, `items_deleted`, `failures`, `deletions_approved`, `halt_reason` | `status` in (`running`, `complete`, `time_limited`, `failed`, `aborted`), set as 5.3 step 8 says; `deadline_at` is `started_at` plus `--max-minutes`, null without it; `trigger` is `schedule` or `workflow_dispatch` from `GITHUB_EVENT_NAME`, else `manual`; `code_version` is `GITHUB_SHA`, else the commit of the checkout the package runs from (marked when its tree has changes), else the package version; `items_planned` counts the distinct items planned for hydration (in-place updates excluded), `items_hydrated` the hydrated items written, and `items_deleted` the item rows step 7 and the prune deleted; `failures` counts this run's item failures, accepted ones included; `deletions_approved` is null or JSON naming the repository, the approved N, the candidate count, and whether the deletion ran |
| `repo_sync_state` | `repository_id`, `last_index_started_at`, `index_pr_total`, `index_issue_total`, `last_success_at`, `last_run_id`, `last_outcome`, `halt_reason` | PK `repository_id`; `last_run_id` is the latest run that attempted the repository, or found it untracked, and `last_outcome` its result, in (`complete`, `time_limited`, `failed`, `incomplete_index`, `halted`, `untracked`), both written by step 8, or by the close of a fatal or interrupted run (5.3 step 8); `last_index_started_at` is written only when that index completed and the repository did not fail later in the run, a time-limited repository included (its unreached items are in `sync_pending`), and never by the close of a fatal or interrupted run; `last_success_at` is the run's `finished_at` when `last_outcome` is `complete` and no item failed outside `[sync].accepted_failures`; `halt_reason` is cleared by a run that did not halt |
| `sync_pending` | `repository_id`, `item_kind`, `number`, `run_id` | PK (`repository_id`, `item_kind`, `number`); `item_kind` in (`pull_request`, `issue`); items a time-limited run planned but did not reach; step 8 replaces the rows of each completely indexed repository that did not fail later in the run (a repository gone during hydration keeps its rows, 5.3 step 4) with this run's unreached items, so a run that reached them all leaves none |
| `sync_failures` | `repository_id`, `item_kind`, `number`, `attempts`, `last_error`, `first_failed_at`, `last_failed_at` | unique (`repository_id`, `item_kind`, `number`); `item_kind` in (`pull_request`, `issue`); `attempts` counts the runs in which the item failed; `last_error` is the error's class and message; row deleted on success, or by step 7 when its repository's in-coverage set no longer lists the item |
| `user_tags`, `pr_user_tags` | unchanged except `created_at`, which uses the 5.1 timestamp type with a `datetime.now(UTC)` default | FK to new `pull_requests` |

Column rules:

- `*_total` columns hold the validated `totalCount` of the matching connection; `commits_total`
  equals the compare `total_commits` above 250 commits. `changed_files` is GitHub's figure, which
  counts a path that appears twice in the file list once; it is never compared with file rows.
- `targets_default_branch` is `base_ref = repositories.default_branch` for the repository's current
  default branch. It is written with each PR and recomputed for the repository's PRs when step 1 of
  5.3 sees the default branch change.
- `author_association` is GitHub's current value, which GitHub computes when read; the index keeps
  it current (5.3 step 3).
- `hydration_version` is per kind (PR, issue) and is compared by inequality, so reverting code
  re-hydrates what the reverted code wrote. A change to a kind's query documents (its alias fields
  and overflow continuations) or a migration that adds or redefines a GitHub-derived column of a
  table that may hold rows bumps that kind's version in the same pull request. Step 3's migration
  creates `pull_requests` empty, so the pull-request version stays 1.
- `state_reason` is GitHub's value including `DUPLICATE` (5.8). Descriptive enums (`change_type`,
  `author_association`, `state_reason`) are text without a CHECK, so a new GitHub value is stored,
  not rejected.

Indexes: foreign-key columns of every child table and of `pr_reviews` and `pr_comments`;
`pull_requests (repository_id, state)`,
`(repository_id, merged_at)`, `(author_id)`, `(merged_by_id)`; `issues (repository_id, state)`,
`(author_id)`; `pr_closing_issues (issue_repo_github_id, issue_number)`. A test checks that every
foreign-key column leads an index (a composite primary key counts), except
`pr_user_tags.user_tag_id`, which this section keeps unchanged.

Views (an account is automation when its `type` is `Bot` or its `github_id` is in
`automation_accounts`; a commit is automation when its linked account is, or its `author_email` is in
`automation_emails`):

- `v_github_users`: every `github_users` column except `id` and `refreshed_at` (sync bookkeeping),
  plus the computed `is_automation`.
- `v_pr_reviewers`: one row per (PR, reviewer) for reviews with a non-null reviewer, excluding the PR
  author (`reviewer_id IS NOT author_id`) and automation. `final_verdict` is the state of the
  reviewer's latest `APPROVED`, `CHANGES_REQUESTED`, or `DISMISSED` review by review id, and
  `COMMENTED` when the reviewer has none of those. `DISMISSED` means the reviewer's last verdict was
  withdrawn, so an earlier verdict does not come back. A later `COMMENTED` review does not override a
  verdict. This matches GitHub's `latestOpinionatedReviews`, which omits a reviewer whose latest
  verdict was dismissed. `comment_count` is the reviewer's `pr_comments` rows on the PR (D5).
- `v_pr_commenters`: one row per (PR, comment author) from `pr_comments`, with its `comment_count`,
  for commenters who submitted no review on that PR, excluding the PR author
  (`author_id IS NOT` the PR's `author_id`) and automation (D5).
- `v_commit_authors`: `pr_commits` rows with the PR's repository `full_name` and `number` in place
  of `pr_id` and the linked account's `github_id` in place of `author_id`, plus the automation flag.

Each view lists its columns explicitly and identifies rows only by consumer keys (repository
`full_name`, PR `number` and `github_id`, account `github_id` and `login`, commit `sha`), never by
row ids; `v_github_users` includes `deleted_at` (D28) and neither `id` nor `refreshed_at`. The views compare `lower(author_email)` with
`automation_emails`.

Reviews and comments by accounts the database never stored stay in `pr_reviews` and `pr_comments`
with a null author and are excluded from the views (D24, D28). Each migration that creates or replaces a view embeds a
frozen copy of its SQL as a literal module constant, never a shared mutable constant, so an old
migration keeps producing its own revision's view. Views are not declared in `Base.metadata` (a
metadata table for a view makes `alembic check` report `add_table`). `alembic check` compares
neither views nor CHECK constraints, so a test compares a database migrated to head with the head
models built by `metadata.create_all`, structurally (`PRAGMA table_xinfo`, `index_xinfo`, and
`foreign_key_list`, and CHECK constraints by name and whitespace-normalized text), and each view
with the frozen SQL of the newest migration that creates or replaces it, found by walking the
revision chain. View, constraint, and sync tests run on databases built by `alembic upgrade head`.

Timestamps: all values are UTC. A `TypeDecorator` over SQLAlchemy `DateTime` rejects naive datetimes,
converts aware values with `astimezone(UTC)` before writing (SQLAlchemy's SQLite `DateTime` stores the
wall-clock fields and drops the offset), and returns timezone-aware values on read. Every timestamp
default is Python-side `datetime.now(UTC)`, never `func.now()` or `CURRENT_TIMESTAMP`, whose
19-character text orders differently. Storage keeps SQLAlchemy's fixed-width format so text ordering
equals time ordering.

Consumer interface: the `v_*` views, every column of which is consumer, and the table columns the
data dictionary marks consumer (Step 3). The dictionary has an entry for every table, view, and
column at head, view columns and `alembic_version` included and SQLite's own `sqlite_*` tables
excluded, each marked consumer or internal. Integer
row ids and sync bookkeeping are internal and change on a rebuild; consumers join on `github_id` or
(`repositories.full_name`, `number`). `PRAGMA user_version` holds `schema_version`, set by
migrations (1 from Step 3) and bumped when an interface column is removed, renamed, or changes
meaning; additive changes do not bump it. A lock file maps each `schema_version` to its consumer
(table or view, column) pairs. A test fails when the current version's entry differs from the
consumer pairs the data dictionary marks at head (a listed pair missing at head, or a consumer pair
at head not listed), and on pull requests the CI lock step (Step 2) also fails when a pull request
deletes an entry, or removes a pair from one, that exists at the base commit; an additive change
adds its consumer pairs to the current entry. A change of meaning is left to review. The manifest (5.6) carries the same number.

### 5.2 Identity and automation

- Users are keyed by GitHub `databaseId` (REST `id`). Item ids (PRs, reviews, issues) come from
  `fullDatabaseId`, a BigInt that GraphQL returns as a JSON string; `PullRequest.databaseId` and
  `PullRequestReview.databaseId` are deprecated because they do not support 64-bit ids. Actor ids come
  from `databaseId`, which `User`, `Bot`, `Organization`, and `Mannequin` do not deprecate.
- GraphQL `Actor` types with a `databaseId`: `User`, `Bot`, `Organization`, `Mannequin`. A null actor
  (deleted account) stores a null reference, unless the object already has a stored reference,
  which it keeps (D28). The fifth type, `EnterpriseUserAccount`, has no
  `databaseId`; an item with such an actor is an item failure, never a null reference.
- One account comes back in more than one shape. dependabot (49699333) is `Bot`/`dependabot` as a PR
  author and `dependabot[bot]` as a commit's `GitActor.user`, whose declared type is `User`; REST uses
  `dependabot[bot]`. `copilot-pull-request-reviewer[bot]` and `Copilot` share id 175728472. Identity
  never joins on login.
- Type and login come only from GraphQL: actor sightings (PR author, merger, review author, comment
  author), which carry `__typename`, and `nodes(ids:)` lookups. A commit-linked account (GraphQL
  `GitActor.user` or the REST compare `author`) never sets them. An item whose commit-linked account
  is not yet stored is written only after `nodes(ids:)` (100 ids per request) resolves that account
  in the same run; a failed lookup is an item error, and a null result for an account with no stored
  values means it was deleted before the database knew it, so the commit is stored without a linked
  account (D28). Lookups run after each hydration batch, for the commit-linked accounts that are
  neither stored, nor written or looked up earlier in the run, nor sighted in the same batch; each
  record reaches the writer with the accounts its links need, so its own transaction inserts any
  account row it needs. An item whose lookup failed gets one more lookup holding only its own ids,
  unless the failed request held only those (5.3 step 4). A looked-up account whose `databaseId`
  differs from the commit link's fails the items waiting on it (gap 78). Each run also refreshes
  users last refreshed more than 24 hours ago and not marked
  deleted, so renamed logins converge. A lookup that returns null for a stored account (deleted
  since) keeps its row, its last known values, and every reference to it, and sets `deleted_at`; an
  account seen again, in an actor sighting or a lookup, has `deleted_at` cleared (D28).
- Only lookups change a stored account's login and type. An actor sighting inserts an account not
  yet stored and clears `deleted_at`, but leaves login and type to the refresh, so an account seen
  in two shapes (175728472) does not change login with the order items are written. `refreshed_at`
  is set when the row is inserted and by every lookup. The refresh runs after the last hydration
  write and before step 7, oldest `refreshed_at` first, and stops at the deadline; in it, a
  `databaseId` that differs from the stored `github_id` is a failed refresh. Accounts that no stored
  row references any more stay stored and refreshed: `github_users` holds every account the
  database ever stored.
- Automation: an account is automation when its type is `Bot` or its `github_id` is listed in
  `[automation].accounts`; a commit is automation when its linked account is, or its author email is
  listed in `[automation].emails`. Each run copies both lists into `automation_accounts` and
  `automation_emails`, and the views apply them (5.1), so a config change takes effect on the next run
  without re-hydration, and the published file records the exclusions.

### 5.3 Sync run

A run starts by claiming `sync_runs`. On a dedicated connection it issues `BEGIN IMMEDIATE`
explicitly (SQLAlchemy's default SQLite transaction handling emits no `BEGIN` before a `SELECT`), and
in that transaction refuses to start while another row is `running` with a null `deadline_at` or one
less than 10 minutes in the past, inserts its own row, and replaces `automation_accounts` and
`automation_emails` from `tracking.toml`. `--break-lock` marks a blocking row `aborted` first. In
the same transaction a `running` row whose `deadline_at` is 10 or more minutes past is marked
`aborted`. A refused claim inserts nothing and exits 1, naming the blocking run; `--break-lock` with
no blocking row proceeds and says so; an interrupted run closes its row as `aborted`. With a deferred
`BEGIN`, a claim racing an uncommitted one does not refuse: it reads no `running` row and its insert
fails with "database is locked" (section 7, "Run claim"). Before the claim (for `--dry-run`, before
step 1), the run checks that the database is at the code's migration head and that `user_version`
equals `schema_version`, and exits 1 otherwise (gap 74).

1. Resolve each configured repository, in `tracking.toml` order, with the resolve query (5.8). `followRenames` is on by
   default, so a renamed repository resolves under its configured name. Match the stored row by
   `github_id` and update owner, name, full name, default branch, `is_private`, and `is_archived`;
   when the default branch changed, recompute `targets_default_branch` for its PRs. A repository that
   cannot be resolved, has no default branch, or resolves to a different `github_id` than its stored
   row (the `is_tracked` row whose `full_name` equals the configured name, ignoring case) fails for
   this run, and nothing of it is deleted; when no configured repository resolves, the
   run is fatal (exit 1). A stored repository that a configured entry resolves to gets
   `is_tracked = true` and its `repo_group` from the config. A stored repository is marked
   `is_tracked = false` and reported (D27) only when the run has no `--repo` filter, every configured
   entry resolved to its stored `github_id` or to a new repository, and none resolved to this
   repository's `github_id`. The resolve query also reads the repository's label names; a stored item
   that carries a label the repository no longer has is planned for hydration, because renaming or
   deleting a label changes no item's `updatedAt`.
2. Index, in the same order: enumerate every pull request and issue with
   `pullRequests|issues(first: 100, orderBy: {field: CREATED_AT, direction: ASC})` (5.8). The index
   carries each item's `fullDatabaseId`, state, and dates, and for PRs `isDraft`,
   `authorAssociation`, and the first 10 closing-issue links. The repository's
   `last_index_started_at` is the GitHub `Date` header of the first request of its completed scan
   (the PR connection's when both are indexed) minus 60 s, because the header has one-second
   resolution and is set after the page is read. Creation order is stable, so an update during
   the scan cannot make pagination skip an item; a number listed twice or out of creation order
   fails the repository. The enumerated count must equal the `totalCount`
   returned with the last page. A transfer or deletion during the scan breaks that equality, so on a
   mismatch the connection is rescanned once; a second mismatch leaves the repository with no writes
   or deletions this run. One GitHub request is in flight at a time: GitHub limits GraphQL to 60 s of
   server CPU time per minute and advises against concurrent requests, and parallel scans were never
   measured. Step 5 measures before any wider setting.
3. Plan by `github_id`. Hydrate an in-coverage item when it is not stored, its `updatedAt` differs
   from `github_updated_at`, its index `state` differs from the stored `state`, its
   `hydration_version` differs from the code's for its kind, it is unsettled
   (`hydrated_at - github_updated_at < 10 minutes`), it carries a label its repository no
   longer has (step 1), its link `totalCount` exceeds 10 and either differs from
   `closing_issues_total` or an indexed link is not stored, or the index lists fewer distinct links
   than the smaller of its `totalCount` and 10 (a link listed twice). Update in place without hydration, for
   items not planned for hydration in this run: `is_draft` and `author_association` from the index
   (ready-for-review is not proven to change `updatedAt`, 0 of 151 counterexamples; association
   changes do not change it), and the closing links and `closing_issues_total` when the index lists
   all of them (D23). The writer applies in-place updates before the first hydrated write; a hydrated
   item takes every field from its hydration response, except that a null actor never replaces a
   stored reference (D28). Order the hydration queue: items in
   `sync_pending` first, then open items, then by `updatedAt` descending, so a time-limited run
   refreshes the newest data first and the next run finishes what it left. The queue spans the
   attempted repositories: steps 1-3 run for all of them before any hydration, and each batch takes
   the first unbatched item and further unbatched items of the same repository and kind, in queue
   order, up to that kind's batch size (25 PRs or 100 issues, step 4). The
   in-place updates of every completely indexed repository are applied even when the deadline stops
   a later repository's index. `sync_pending` only orders the queue: a pending item none of the
   conditions above selects is not hydrated. An index entry whose number is stored under a different
   `github_id` is an item failure; the stored row then leaves through step 7. The planner is a pure
   function over snapshots of the stored rows, which `--dry-run` reuses.
4. Hydrate: GraphQL batches of 25 PRs or 100 issues of one repository via aliased
   `pullRequest(number:)` and `issue(number:)` fields (5.8). Read each alias separately. Overflow
   beyond 100 entries on any connection (reviews, comments, commits, files, labels, closing issues)
   pages through `node(id:)`; continuations from several items share one request, and each also reads
   `headRefOid` and `updatedAt`. An item fails when any continuation's `headRefOid` or `updatedAt`
   differs from its alias response's, so a push or a new comment between pages never mixes two
   states. GraphQL stops at 250 commits, so above 250 the full list comes from REST
   `compare/{baseRefOid}...{headRefOid}?per_page=100&page=N`. `hydrated_at` is the GitHub `Date`
   header of the item's alias response, and `github_updated_at` the `updatedAt` in that response,
   never the index value. Failures are handled by class:

   | Class | Detected by | Action |
   |-------|-------------|--------|
   | Rate limited | Any HTTP 429 (GraphQL or REST); HTTP 403 with `retry-after`, `x-ratelimit-remaining: 0`, a secondary-limit message, or `errors[].type == "RATE_LIMITED"`; GraphQL HTTP 200 with `RATE_LIMITED` or an error message naming a secondary rate limit (GitHub documents 200 and 403 for GraphQL, 403 and 429 for REST) | Wait for `retry-after`; else, when `x-ratelimit-remaining` is 0 or the error is `RATE_LIMITED`, until `x-ratelimit-reset` or the last `resetAt`; else 60 s. Each further limit before a success waits at least 60 s doubled per repeat, whatever the headers say (GitHub: "If your request continues to fail due to a secondary rate limit, wait for an exponentially increasing amount of time between retries, and throw an error after a specific number of retries"). Never split; not an attempt. A wait that would end after the `--max-minutes` deadline stops queueing instead |
   | Timeout or resource limit | HTTP 502/504 (GitHub documents both for a request over 10 s); a GraphQL error at any path reporting a timeout or exceeded resource limits (GitHub returns "partial results along with an error indicating that resource limits were exceeded") | Split the batch in half at once; a same-size retry repeats the cost |
   | Server or transport error | other 5xx; a GraphQL server fault at any path; connection error; client timeout; a connection the server broke off or a body it encoded wrongly; a response without data or errors, without a readable `Date`, or with an error path that is not strings and list indices (never trimmed, since an exact path is what makes an item gone) | Retry up to 3 more times (2, 4, 8 s, with jitter), then split |
   | Item gone | alias `null` with `NOT_FOUND` | No write and no failure; the next index removes it |
   | Item error | another alias error; partial alias data; a value the typed models reject; a failed check in step 5; HTTP 404/422 on a compare page | Retry the item once alone, then record it in `sync_failures` and write nothing for it |
   | Repository gone during hydration | a batch's `repository` is null (`RepositoryNotFound`, `RepositoryShapeError`; gap 79) | The repository fails for the rest of the run: its written items stay, step 7 skips it, and its `sync_pending` rows are unchanged |
   | Local database error | any error the database raises while the writer writes (a lock held past the busy timeout, an unexpected constraint failure; gap 77) | Fatal: roll back the item, close the run, exit 1 |
   | Not understood | a GraphQL error without a path, or no data, that matches none of the classes above (a rejected document, an unrecognized limit message); a missing or malformed `rateLimit`; another status (400, a GraphQL 404 or 422) | Fatal: drain the writer, close the run, exit 1 |
   | Unauthorized or forbidden | HTTP 401; a failed credential request (an App token mint answering 4xx without a rate-limit signal); HTTP 403 with no rate-limit signal (a missing permission); an HTTP client failure outside the network (a redirect loop, a proxy or URL problem, a missing auth dependency) | Fatal: drain the writer, close the run, exit 1 |

   Typed response models check types strictly: GitHub's JSON has exact types, and lax coercion
   would read `"no"` as false. `BigInt` ids must be strings of digits and timestamps ISO strings.

   Splitting continues down to single items. A request that cannot split retries like a server
   error (3 more times, 2, 4, 8 s) in the timeout class too. A single item whose own request still
   fails after those retries is recorded as failed without the solo retry, which would repeat that
   request; every other item error gets its solo retry. A resolve or index page that still fails
   fails its repository for this run. A compare page that still fails is an item error. A
   user lookup failure is scoped to what it covers, one node or one request of 100 ids, and is an
   item error for each item waiting on those accounts (5.2); a failed 24-hour
   refresh keeps the stored values, is logged, and is retried next run without changing the exit
   code. Fields that drive logic (`state`, review `state`) are closed enums in the typed models;
   descriptive enums are text (5.1).

   The `--max-minutes` deadline applies to every request. At the deadline the run issues no new
   requests, treats a repository whose index is unfinished as incomplete, records `sync_pending`,
   and runs steps 7 and 8. Without `--max-minutes`, a rate-limit wait longer than a named constant
   fails the run (exit 1).

   Budget, per pool: GraphQL readings come from `rateLimit { cost remaining resetAt }` in every
   response. One token's readings can alternate between two windows, so the guard keeps the lowest
   `remaining` per `resetAt` and waits until every window below the reserve has reset. REST readings
   come from the `x-ratelimit-remaining` and `x-ratelimit-reset` headers of compare responses. Below
   the reserve (a named constant above the largest measured request cost) the guard sleeps until the
   reset, subject to the deadline rule above. `sync_runs.graphql_points` is the sum of
   `rateLimit.cost`; `rest_calls` counts REST requests.
5. Validate before writing: the fetched count equals `totalCount` for reviews (requested without
   `PENDING`, so the counts agree by construction), comments, commits, files, labels, and closing
   issues; every overflow page reports the first page's `totalCount`; a null connection or null node
   is a failure, never a count of zero; the compare `total_commits` equals the PR's commit
   `totalCount`, and the fetched compare commits equal `total_commits`; the returned number equals
   the request (the batch names the resolved repository); `state` is `MERGED` exactly when
   `mergedAt` is set; the entries the database keys (labels, closing issues, reviews, comments,
   commits) are distinct; every page with a next page has entries and a new cursor; and an error
   outside every item fails the request, which then splits, and an item it fails alone gets its
   solo retry, since no retry of the transport preceded it. The hydrated PR's `github_id` equals the
   one the index listed for its number, and a review or comment whose `github_id` is stored under
   another PR fails the item. A PR that closed without merge, or an item that left coverage,
   between index and hydration is skipped without a failure; the next index removes it. `source/`
   returns such a PR as hydrated, so the engine skips it, before any account lookup for it.
6. Write: one writer task with its own session. Each item is one transaction: upsert by `github_id`
   with `INSERT ... ON CONFLICT (github_id) DO UPDATE`, which keeps the row id (never
   `INSERT OR REPLACE`, which deletes the row and cascades); replace the hydrated child rows
   (`pr_labels`, `pr_reviews`, `pr_comments`, `pr_commits`, `pr_files`, `pr_closing_issues`,
   `issue_labels`), where a new review or comment with a null author takes the stored author of the
   row with the same `github_id`, a commit with a null link takes the stored link of this PR's row
   with the same SHA, and a null PR or issue author or merger keeps the stored one (D28); store the
   validated totals; set `hydrated_at`. Fetchers never touch the database.
   A transferred issue arrives with a new `github_id` and number and is inserted in its new
   repository; step 7 removes the old row.
7. Delete: after all writes, for each repository with a complete index that did not fail during
   hydration (step 4), recompute from the database
   the stored items whose `github_id` is absent from that repository's in-coverage set, then delete
   them, together with `sync_failures` rows for items absent from it. Halt deletions for a repository
   when the count exceeds max(25, 2% of its stored PR and issue rows), and record the halt and the
   candidate count in `repo_sync_state.halt_reason`; the `sync_failures` rows of items absent from
   the in-coverage set are removed even then. `ghactivity sync --allow-deletions owner/name=N` (given at
   most once, N at least 1, naming a configured repository)
   deletes all of that repository's candidates when there are at most N, otherwise halts as without
   the flag, and records the approval in `sync_runs.deletions_approved`. `--prune-untracked` deletes
   the repositories that were untracked when the run started and are still untracked after step 1
   (their rows and, by cascade, everything stored under them; `github_users` stay), outside the
   delete guard, and counts their items in `sync_runs.items_deleted` (D27). It is
   refused, after step 1, when any configured entry failed to resolve or `--repo` is given; a refused
   prune deletes nothing and makes the exit 2.
8. Record: update `repo_sync_state` and close the `sync_runs` row. Exit 0 when every repository
   indexed completely and nothing failed; 2 when an item failed in this run (other than one listed
   in `[sync].accepted_failures`), a repository failed, had an incomplete index, or halted, or a
   stored repository is untracked; 1 on a fatal error. An accepted failure is still recorded and
   reported; it names the repository, number, and reason in `tracking.toml`, so a permanent failure
   does not turn every run red. `--max-minutes` stops queueing new hydration, records in
   `sync_pending` the planned items it did not reach (a repository that failed during hydration
   keeps its rows, step 4), drains the writer, still runs step 7 for complete
   repositories and step 8, and sets status `time_limited`; unreached items are not failures. Step 8
   writes `last_run_id` and `last_outcome` for every repository the run attempted. `--dry-run` takes
   no lock, inserts no `sync_runs` row, and writes nothing: it runs steps 1-3 and reports step 1's
   updates, the planned hydrations and in-place updates, and the delete candidates. It exits as the
   run would for every step-8 condition steps 1-3 can show (a failed or incompletely indexed
   repository, an untracked stored repository, a deletion halt, a refused prune), and, like every
   reader in `src/`, uses no server-side cursor, since a half-read query blocks a running sync's
   commit (gap 77).

   `status` records how the run ended and `exit_code` its result. `complete`: it reached step 8
   without reaching the deadline; `time_limited`: it reached the deadline at any point, the refresh
   included; `failed`: exit 1; `aborted`: `--break-lock`, a reclaimed stale row, or an interrupt. A
   repository's `last_outcome` is the first that applies of `untracked`, `failed`,
   `incomplete_index` (a count mismatch after the rescan, or the deadline during its index),
   `halted`, `time_limited` (planned items left unreached), and `complete`; step 8 also writes
   `last_outcome = untracked` and `last_run_id` for every stored repository it finds untracked. The
   close of a fatal or interrupted run also writes `last_run_id` and `last_outcome = failed` for
   every repository it attempted, never `last_index_started_at`, because its committed writes are in
   the file. A deadline before any repository resolves makes the run `time_limited`, not fatal. A
   run that never starts exits 1 before any `sync_runs` row: a usage error (click's own code for it
   is 2, the code D25 publishes on; gap 69), an invalid `tracking.toml`, missing credentials, a
   database not at head, or a refused claim. `--repo` and the `--allow-deletions` repository must
   each name a configured repository, ignoring case; a single-valued option given twice (click keeps
   the last value silently) and `--dry-run` with `--break-lock` are usage errors.

### 5.4 Verification

`ghactivity verify [--repo owner/name]... [--exact] [--max-minutes N] [--report PATH]` (online)
checks the database against a fresh index, as of the sync it follows: for each repository, "that
run" is `repo_sync_state.last_run_id` and the boundary is its `last_index_started_at`. verify uses
the 5.3 step 4 failure classes and retries. It covers the configured repositories (after `--repo`)
and every stored untracked repository, which it reports as not synced without a request. Like
`sync`, it exits 1 before any request unless the database is at the code's migration head and
`user_version` equals `schema_version` (gap 74), and it uses no server-side cursor (gap 77); it
also exits 1 while a sync run holds the lock (5.3). It re-indexes and applies the coverage rule, then
compares by `github_id` the stored set with the result (missing and extra items) and, for items in
both, the stored `github_updated_at`, `state`, `is_draft`, and closing links with the fresh values
(stale items). For a PR whose link `totalCount` exceeds the 10 links the index carries, closing
links compare as in 5.3 step 3: equal totals, and every indexed link stored. Each difference takes
the first class that matches, in this order:

- not synced: every difference in a repository whose `last_outcome` is `failed`,
  `incomplete_index`, or `untracked`, or that has no `last_index_started_at`, reported under the
  repository's name;
- known failure: a `sync_failures` row;
- pending: a `sync_pending` row from that run;
- halted deletion: a stored item outside the fresh in-coverage set in a repository whose
  `last_outcome` is `halted`;
- drift: the fresh index shows a `createdAt` or `updatedAt` at or after the boundary, or, for an
  `is_draft` or closing-link difference with an earlier `updatedAt`, the matching timeline event
  (ready-for-review or convert-to-draft; connected or disconnected) is at or after it, looked up only
  for differing items; an `author_association` difference is drift with its own count;
- removed since sync: a stored item missing from the fresh index whose lookup by number returns
  `NOT_FOUND`, listed by number (the lookup is a hydration batch: `Gone` is removed since sync, a
  hydrated PR an error);
- unchecked: a differing item whose timeline lookup or lookup by number still fails after the
  retries, listed by number;
- error: anything else.

Only an error fails verify. `verify --exact` also fails on not synced, pending, halted deletion,
removed since sync, unchecked, and known failures not listed in `[sync].accepted_failures`; it is
the mode the Step 3-5 acceptance uses, and drift is reported in both modes. A repository that verify cannot
re-index (a fetch that still fails after its retries, a second count mismatch, or a resolve to a
`github_id` other than its stored row's) is reported as
unverified: it fails `--exact` and is reported, without failing, otherwise. A verify that reaches
`--max-minutes` stops with a timeout instead of waiting past it. Exit 0 when no failing class
occurred, 2 when one did, 1 on a fatal error or a timeout. `--report PATH` writes a versioned JSON
report on every exit, 1 included (marked incomplete): the exit code and whether `--exact` was given;
per class, the count of items and their numbers (an item whose differences take two classes counts
in each); per repository, its `last_run_id`, boundary, per-class counts, and per-state counts; and
the unverified repositories with their reasons.

The enumerated count must equal the last page's unfiltered `totalCount` (rescanned once, as in
5.3); per-state counts are
derived from the enumeration and reported, never asserted, because a state change during a scan
moves an item between states. Stored child rows must equal stored totals: `pr_files` rows equal
`files_total`, `pr_commits` rows `commits_total`, `pr_reviews` rows `reviews_total`, `pr_labels` rows
`labels_total`, `pr_closing_issues` rows `closing_issues_total`, `pr_comments` rows
`comments_total`, and
`issue_labels` rows equal `issues.labels_total`.

`ghactivity verify --db-only` (Step 6a, beside the `snapshot` command that writes its inputs; it takes
the snapshot gz, its manifest, and the asset digest as explicit arguments, and a halt exits 1)
checks a downloaded snapshot before anything touches it: the gz SHA-256
equals the manifest's (and the release asset's `digest` when GitHub reports one; a null digest leaves
the manifest as the only check); decompression succeeds; `PRAGMA integrity_check` returns `ok` and
`PRAGMA foreign_key_check` returns no rows; per-repository PR and issue counts equal the manifest's;
and the file's migration revision is the code's head or an ancestor of it. An unknown revision, such
as one written by newer code, halts. Migrations are never downgraded: recovery from a snapshot the
code cannot upgrade is a bootstrap. There is no shrink tolerance: the sync's delete guard bounds
deletions per run.

GitHub search is never an oracle: it returns at most 1,000 results and omits items
(`prebid/prebid-integration-monitor#8` is absent from search but present in the repository).

### 5.5 Configuration

`src/github_activity_db/tracking.toml`, shipped inside the package and read through
`importlib.resources`, parsed with `tomllib` and validated with pydantic at load; unknown keys are
rejected and missing optional values take defaults. `TRACKING_CONFIG_PATH` names a different file.
`[coverage].floor` is required and validated in strict mode, because lax mode accepts the TOML
string `"2023-01-01T00:00:00Z"` and the integer 1672531200 (section 7, "Lax datetimes"). Values are
strictly typed throughout: an account `github_id` is at least 1, and `login`, `email`, and every
`reason` are non-empty; automation account ids and emails are unique, emails compared lowercase;
`unsettled_minutes` is an integer of at least 1, `delete_guard_min_rows` an integer of at least 0,
and `delete_guard_percent` a number above 0 and at most 100; an `accepted_failures` entry has a
`number` of at least 1, is unique by repository and number, must name a configured repository, and
matches by that entry and the number, so a rename does not unhook it; an entry whose item did not
fail is reported.
The file holds domain policy only. Step 1 added `[[repositories]]`; Step 3 adds `[coverage]`,
`[automation]`, and `[sync]`.

```toml
[coverage]
floor = 2023-01-01T00:00:00Z   # required; a TOML offset date-time; converted to UTC

[[repositories]]
owner = "prebid"
name = "Prebid.js"
group = "Client"
# one entry per tracked repository

[automation]
# Accounts of type Bot are flagged automatically; list service accounts of type User here.
accounts = [
  { github_id = 99054218, login = "prebidjs-release", reason = "release automation" },
  { github_id = 81847, login = "claude", reason = "receives commits authored as noreply@anthropic.com" },
]
emails = [
  { email = "noreply@anthropic.com", reason = "Claude commits without a linked account" },
]

[sync]
unsettled_minutes = 10
delete_guard_min_rows = 25
delete_guard_percent = 2.0
# Items whose failure is understood and does not count toward exit 2 (5.3 step 8).
accepted_failures = [
  # { repository = "prebid/Prebid.js", number = 12345, reason = "..." },
]
```

Mechanics measured against GitHub are named constants in `source/`, each citing its evidence row,
not configuration: batch sizes (25 PRs, 100 issues), page size 100, the 250-commit compare threshold,
the retry schedule, the budget reserves, and one request in flight. Workflow values (`--max-minutes`,
snapshot retention) are environment values in `sync.yml` (5.6). Repository groups are listed in
`tracking.toml`.

### 5.6 Scheduled sync and publication

Workflow `.github/workflows/sync.yml`:

- Triggers: `schedule` at `17 */6 * * *` (start of the hour is a documented high-load time) and
  `workflow_dispatch` with inputs `bootstrap`, `allow_deletions` and `prune_untracked` (passed to
  `ghactivity sync`, 5.3 step 7), and `release` (default `db`; the Step 6 acceptance runs against
  `db-test`). A `schedule` run has no inputs, so every step reads `${{ inputs.release || 'db' }}` and
  treats the other inputs as unset.
- `concurrency: { group: db-sync, cancel-in-progress: false }`. A new run replaces a pending one and
  never cancels a running one.
- Top-level `permissions: {}`; each job is granted only what it lists. Three jobs, so the job that
  runs project code and PyPI dependencies holds no write token:
  - `sync` (`permissions: contents: read`; `timeout-minutes: 355`, under GitHub's 6-hour job limit)
    holds the App credentials. Without `bootstrap`, it downloads the latest snapshot and runs
    `verify --db-only`; a missing or failing snapshot halts the run. With `bootstrap`, it downloads
    nothing and starts from an empty file, and the manifest records `bootstrap: true`. It then runs
    `alembic upgrade head`, asserts the revision is head, runs `ghactivity sync --max-minutes 300`
    and `ghactivity verify --max-minutes 40 --report verify.json`, runs `ghactivity snapshot
    --verify-report verify.json`, and hands the snapshot and manifest to `publish` as a workflow
    artifact, with the sync and verify exit codes as job outputs. The job fails when
    `verify --db-only` halts or sync exits 1.
  - `publish` (`needs: sync`; `permissions: contents: write`; no App secret, no project code, no
    PyPI dependencies) runs when D25's condition holds. It accepts only artifact files whose names
    match `^github_activity-[0-9]{8}T[0-9]{6}Z\.(db\.gz|manifest\.json)$` and reads only the
    SHA-256 (64 hex characters) from the manifest. It checks the SHA-256 with coreutils, uploads the
    database and then the manifest (the manifest is the commit point), prunes, and writes the
    heartbeat.
  - `report` (`needs: [sync, publish]`, `if: always()`, `permissions: {}`, no checkout) fails the
    workflow unless both jobs succeeded, publish ran, and both exit codes are 0. A skipped job
    reports success in GitHub Actions, so this job is what makes an unpublished run red.

  Values that `sync` produces (job outputs, artifact names, manifest fields) are untrusted in the
  other jobs: they are used only in `if:` expressions, or through `env:` after validation as an
  integer or a 64-character hex digest, and never interpolated into a `run:` script.
- Authentication (D10): `ghactivity sync` and `ghactivity verify` authenticate in-process with
  githubkit's `AppInstallationAuthStrategy` (app ID, private key, and installation ID from secrets;
  contents, pull requests, issues, and metadata read), which mints a new installation token before
  the 1-hour expiry. A token minted once by `actions/create-github-app-token` would expire during the
  bootstrap or any long run. When App credentials are set, the CLI uses them and ignores
  `GITHUB_TOKEN`. This needs the `githubkit[auth-app]` extra (PyJWT, cryptography) in the locked
  dependencies (Step 6a). The App key is readable by the sync job's dependencies, so the App stays
  read-only and its key rotatable. The workflow's own `GITHUB_TOKEN` (`contents: write`) is used
  only in `publish`.
- Snapshots: `ghactivity snapshot --out DIR [--run-id ID] [--verify-report PATH]` writes the copy
  with `VACUUM INTO`, checks its header for rollback-journal mode (the check
  `scripts/check_db_journal.py` makes), gzips it, and writes the manifest, in the workflow and for
  6b's first snapshot (`run_id` null). Names are `github_activity-<T>.db.gz` and
  `github_activity-<T>.manifest.json`, with `T` as `YYYYMMDDTHHMMSSZ`, because GitHub renames asset
  names that contain special characters. The latest snapshot is the newest `T` whose two assets are
  both in state `uploaded`; `verify --db-only` on that pair decides, and a mismatch halts, with no
  fallback to an older pair. The manifest records the run id, code SHA, migration head,
  `schema_version`, coverage floor, `bootstrap`, per-repository PR and issue counts, per kind the
  number of rows whose `hydration_version` differs from the code's, the sync exit code, verify's
  exit code and counts per class (from `--verify-report`; null without it), and the SHA-256.
- Uploads never use `--clobber`. Before each upload, `publish` deletes a same-name asset in any state
  other than `uploaded`. A same-name `uploaded` asset whose `digest` is `sha256:` followed by the
  expected SHA-256 (or, when `digest` is null, whose downloaded SHA-256 matches) is skipped, so a
  re-run of `publish` completes; any other same-name asset fails the job.
- `VACUUM INTO` writes a consistent copy that can be read from a read-only location. On the SQLite
  measured (section 7), a copy of a WAL database was in rollback-journal mode; SQLite does not
  document that, so `ghactivity snapshot` checks it on every snapshot.
- Retention: keep the newest 28 snapshot pairs (7 days at 4 runs a day); delete assets in any state
  other than `uploaded` (an upload that ends in a 502 can leave one in state `starter`) and any
  database without a manifest older than the newest pair; fail the run when the release holds more
  than 900 assets (GitHub allows 1,000 per release).
- Release `db` is created in Step 6b (tag `db` on the then-current `main`, not marked latest), after
  checking that immutable releases are off for the repository. The workflow halts with a clear error
  when the release is missing or immutable.
- Heartbeat: when the last commit on `main` is older than 7 days, `publish` writes
  `data/manifest.json` through the contents API (`PUT /repos/{owner}/{repo}/contents/{path}`) with
  `GITHUB_TOKEN`, so no git credential is stored on disk. A `GITHUB_TOKEN` commit triggers no
  workflow, so it carries no CI verdict by design; the file is JSON with two-space indentation, no
  trailing whitespace, and one final newline, the form the pre-commit file hooks accept on the next
  pull request.
- `github_activity.db` is removed from git tracking in 6c (Step 6 lists the cleanup).

A `workflow_dispatch` trigger runs only when the workflow file exists on the default branch, so the
workflow merges first with the manual trigger only, is proven by manual runs, and the schedule is
enabled afterwards.

### 5.7 Code layout

`source/` is the only package that imports githubkit; from Step 3, when `github/` is gone, a test
fails when another module does. It scans `src/` outside `source/`, `scripts/`, and `alembic/` for
imports at any scope, `TYPE_CHECKING` blocks included, and for `importlib.import_module` and
`__import__` called with a string literal, and fails when it scans no file; planted files test each
spelling, and a string decoy is not flagged. Tests are exempt, since they build githubkit's errors. A
second test fails when `source/` imports SQLAlchemy or `db/`, so fetchers cannot touch the database.

| Disposition | Modules |
|-------------|---------|
| Keep | `config.py` (without `RateLimitConfig`, `PacingConfig`, `SyncConfig`, and the `rate_limit`, `pacing`, and `sync` settings; `env_nested_delimiter="__"`, gap 64; `github_token` an optional `SecretStr`, which one CLI helper requires for the commands that call GitHub), `tracking.py` and `tracking.toml`, `logging.py` (loguru `diagnose` off, gap 75), `db/engine.py` (without `create_tables` and `drop_tables`; databases are built by `alembic upgrade head`, `tests/db/test_application_engine.py` builds its database that way and keeps both gap 46 checks, and `tests/test_db_engine.py` drops `test_create_tables`), `cli/app.py` (without the `search` and `tags` stubs, D17; tracebacks without local variables, gap 75; a run that never starts exits 1, gap 69), `cli/common.py` (gains `parse_repo_string`; `OutputFormat` goes with the removed subcommands, since no new command takes `--format`), the package and `cli/` `__init__.py` files, `py.typed` |
| New in Step 2 | `source/`: the client factory, failure classification, budget per pool, query documents with their per-kind `hydration_version`, private typed response models, typed invariant errors, resolve, index, hydration with overflow and compare, user lookup, and the typed records that cross into `sync/` (a PR record with its children; per-item outcomes hydrated, gone, or failed) |
| New in Step 3 | `sync/`: plan, writer, engine, verify, run records. The writer's SQL lives in `sync/` (`INSERT ... ON CONFLICT ... DO UPDATE`, with `COALESCE` keeping a stored reference when GitHub returns null, D28), so one module owns each item's transaction. `db/` gains the timestamp type, `SCHEMA_VERSION`, and the packaged tuple of migration revision ids (gap 74). `source/` gains the rate-limit reading for `ghactivity github rate-limit` (Step 3a) and verify's timeline lookup (Step 3b) |
| Replace in Step 3 | `db/models.py`, `db/__init__.py` (its re-exports follow the new models), and the shared test fixtures that import them (`tests/conftest.py`, `tests/factories.py`); `cli/sync.py` becomes one `ghactivity sync [--repo owner/name]... [--max-minutes N] [--dry-run] [--allow-deletions owner/name=N] [--prune-untracked] [--break-lock]` command (the `pr`, `repo`, `all`, and `retry` subcommands go), plus `ghactivity verify [--repo owner/name]... [--exact] [--max-minutes N] [--report PATH]` and, in Step 6a, `--db-only` and `ghactivity snapshot`; `cli/github.py` keeps `rate-limit` over the `source/` factory (GraphQL `rateLimit` and REST `/rate_limit`; `--all` stays and `--verbose` goes), and `test [--repo owner/name] --pr N` resolves one repository (by default the first configured) and hydrates that PR |
| Remove in Step 3 | all of `github/` (`client.py`, `exceptions.py`, `pacing/*`, `rate_limit/*`, `sync/*`), all of `schemas/`, `db/repositories/*`, and their tests and the fixtures only they use |

GraphQL requests go through githubkit's `GitHub.arequest("POST", "/graphql", content=...)`, which
returns a 200 body with `data` and `errors` together. `source/` encodes the body itself, with
`allow_nan=False`, so variables JSON cannot represent raise before any request is sent. `github.graphql.arequest` turns any error into
an exception: `GraphQLFailed`, whose `.response` is the parsed body without the HTTP status or
headers (so the `Date` header 5.3 needs is lost), or `PrimaryRateLimitExceeded` for `RATE_LIMITED`.
Per-alias handling would then run inside exception handlers. githubkit retries only
403/429 rate-limit responses and 5xx; a GraphQL primary limit arrives as HTTP 200 with
`errors[].type == "RATE_LIMITED"`, which `source/` detects itself. githubkit 0.14.2 defaults to
`auto_retry` on, `http_cache=True`, and no timeout, so the factory sets `auto_retry=False`,
`http_cache=False`, and an explicit timeout (for example `httpx.Timeout(30.0, connect=10.0)`, above
GitHub's 10 s request limit). Callers open the client with `async with` and create their fetcher
tasks inside that block: githubkit keeps the open HTTP client in a context variable, so a task
created earlier builds a new client per request.

### 5.8 Query shapes

Resolve (step 1; labels past the first 100 continue with the next document):

```graphql
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id databaseId nameWithOwner name owner { login } isPrivate isArchived
    defaultBranchRef { name }
    labels(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { name } }
  }
  rateLimit { cost remaining resetAt }
}
```

Repository labels after the first page (cost 1):

```graphql
query RepositoryLabels($id: ID!, $after: String!) {
  node(id: $id) {
    ... on Repository { id labels(first: 100, after: $after) { totalCount pageInfo { hasNextPage endCursor } nodes { name } } }
  }
  rateLimit { cost remaining resetAt }
}
```

PR index (cost 1 point per 100 items, measured with and without the closing-link selection):

```graphql
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number fullDatabaseId state isDraft authorAssociation createdAt updatedAt mergedAt
        closingIssuesReferences(first: 10) { totalCount nodes { number repository { databaseId nameWithOwner } } }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
```

Issue index (Step 4), same shape with
`issues(...) { ... nodes { number fullDatabaseId state createdAt updatedAt closedAt } }`.

Pull request fields per alias (25 aliases cost 2 points; cost depends only on the query shape):

```graphql
id number fullDatabaseId state isDraft title body createdAt updatedAt mergedAt
baseRefName baseRefOid headRefOid isCrossRepository authorAssociation
additions deletions changedFiles
author { ...Actor } mergedBy { ...Actor }
labels(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { name } }
closingIssuesReferences(first: 100) { totalCount pageInfo { hasNextPage endCursor }
  nodes { number repository { databaseId nameWithOwner } } }
reviews(first: 100, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) { totalCount
  pageInfo { hasNextPage endCursor } nodes { fullDatabaseId state author { ...Actor } } }
comments(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { fullDatabaseId author { ...Actor } } }
commits(first: 100) { totalCount pageInfo { hasNextPage endCursor }
  nodes { commit { oid authoredDate author { name email user { id databaseId login } } } } }
files(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { path changeType additions deletions } }
```

Issue fields per alias (Step 4; 100 aliases cost 1 point):

```graphql
id number fullDatabaseId state stateReason(enableDuplicate: true) title body createdAt updatedAt closedAt
author { ...Actor }
labels(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { name } }
```

Overflow continuation, one alias per (item, connection), several per request:

```graphql
o0: node(id: $id0) { ... on PullRequest { headRefOid updatedAt
  reviews(first: 100, after: $after0, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) {
    totalCount pageInfo { hasNextPage endCursor } nodes { fullDatabaseId state author { ...Actor } } } } }
```

User lookup (5.2; 100 ids per request):

```graphql
nodes(ids: $ids) { __typename id
  ... on User { databaseId login } ... on Bot { databaseId login }
  ... on Organization { databaseId login } ... on Mannequin { databaseId login } }
```

Rate-limit reading for `ghactivity github rate-limit` (Step 3a; REST `/rate_limit` supplies each
pool's limit and use):

```graphql
query RateLimit { rateLimit { cost remaining resetAt } }
```

Verify's timeline lookup (Step 3b) reads, for each differing PR, its ready-for-review,
convert-to-draft, connected, and disconnected events at or after the repository's boundary. Its
document, formula cost, and recordings are added here when it is written; the recorder adds them
without changing the existing recordings.

`Actor` selects `__typename login` plus `id databaseId` on `User`, `Bot`, `Organization`, and
`Mannequin`; an `EnterpriseUserAccount` actor therefore arrives without an id (5.2). A continuation
includes the `Actor` fragment only when its connection uses it: GitHub rejects a document that
defines a fragment it does not use. Do not nest a
connection inside `commits` (for example `authors(first: N)`); GraphQL charges for every possible
nested node.

## 6. Execution steps

Each step is one pull request from a `feature/<slug>` branch, except Step 3 (pull requests 3a and
3b) and Step 6 (pull requests 6a and 6c, with the 6b manual runs between them). A step starts after
the previous one merges.

### Step 1: Guardrails

Scope:

- `.github/workflows/check.yml` on `pull_request` and pushes to `main`: `uv sync --frozen --extra dev`,
  `pre-commit run --all-files`, pytest with JUnit output checked by `scripts/check_junit.py` (at
  least one test executed, none failed or errored, no skips; D22), and `alembic upgrade head` plus
  `alembic check` against a temporary database. A built-wheel job installs the wheel with the
  locked dependencies and runs it from outside the checkout (D21).
- Pre-commit runs ruff and mypy from `uv.lock` (local hooks via `uv run`), so local and CI use one
  version. `scripts/count_suppressions.py` fails above 5 `type: ignore` or 3 `noqa` comments in
  `src/`, counting the spellings its docstring lists (including `@no_type_check` and the isort
  skip comments); ruff `PGH004` rejects blanket `noqa`. `check-added-large-files` checks every
  file (`github_activity.db` exempt until Step 6).
- Build inputs pinned: GitHub Actions and `pre-commit-hooks` by commit SHA, uv 0.7.10, the
  hatchling build backend; the built-wheel job builds with hash-checked build dependencies
  (`build-constraints.txt`) and installs locked dependencies with hash checking.
- Fix the existing lint and format findings under the locked ruff.
- Declare the `sync_failures` indexes on the model so `alembic check` passes.
- Per-connection `PRAGMA foreign_keys=ON` and `busy_timeout=30000`; the tracked database stays in
  rollback-journal mode, checked on its header by a pre-commit hook and a test (D20).
- `tracking.toml` in the package and its loader; `Settings.tracked_repos` reads it. Step 1 adds only
  `[[repositories]]` (with `group`); `[coverage]`, `[automation]`, and `[sync]` land in the steps
  that read them, so no section exists without a consumer.
- Documentation for the changed commands and files; this plan.

Tests: pragma values and a rejected foreign-key violation, on a directly built engine and through
the application's `get_session()`; `alembic check` on a fresh database; config loading for valid,
invalid, and default cases; the JUnit gate for each condition, including a report written by a
real all-skipped pytest run; the suppression counter for each spelling, string decoys, and each cap
at and over its limit.

Acceptance: CI passes on the exact PR head with a non-empty job list; removing the model index makes
CI fail; removing the pragma listener makes its test fail; routing `get_engine()` around
`build_engine()` makes a test fail; a wheel without `tracking.toml` fails the built-wheel job.

### Step 2: GitHub source layer

Scope: the `source/` package (5.7): the client factory, failure classification and budget guard
(5.3 step 4), query documents (5.8) with per-kind `hydration_version` constants, private typed
response models, typed invariant errors, and the typed records out; resolve with label names; the PR
index; the batch hydrator with overflow on all six connections, the compare fallback, and the user
lookup. A lock file (`tests/query_versions.json`) maps each `<kind>:<version>` to the SHA-256 of its
kind's normalized query documents (the batch request reduced to one alias, and each continuation
request reduced to one alias; comments removed and whitespace runs collapsed to one space). A test
fails when the current documents differ from the current version's entry, and a CI step fails when
a pull request changes or removes an entry that exists at the base commit; it runs the base
commit's copy of the checker, or the pull request's copy where the base has none. Pull requests only: the issue shapes land in Step 4. Not wired into
the CLI; no configuration changes.

Tests run through the `source/` client factory with an injected `httpx.MockTransport`.
`scripts/record_fixtures.py` records responses from public repositories (never
`prebid-agent-skills`): one file per alias, overflow page, and compare page, each stamped with the
normalized query hash and the recording date. A recording keeps the response body and the response
headers `date`, `retry-after`, and `x-ratelimit-*`, never request headers. The mock transport
assembles batch responses from per-alias recordings, so a split or a batch-size change needs no new
recording, and it fails with "re-record" when a request's query hash differs from the stamp.
Synthetic error responses live in `tests/fixtures/source/synthetic/` and name their source:
GitHub's documentation, a live observation, or a construction for a case GitHub does not document;
a test that needs a variant of a recorded item modifies it in the test and says which. Scenarios:
labels past 100 (`golang/go`; no tracked repository has more than 100); overflow on each of the six connections; more than
250 commits (compare); ids above 2^31; a null merger (`Prebid.js#13769`); a null PR author and a null
comment author (`Prebid.js#12975`); a dismissed review (`prebid-server-java#4637`); a duplicate file
path (`Prebid.js#13481`); one bot seen as a PR author and as a commit user (`Prebid.js#15724`); an
`EnterpriseUserAccount` actor; a `NOT_FOUND` alias; a head change, and an `updatedAt` change
without a push, between overflow pages; each failure class in 5.3 step 4 (`RATE_LIMITED` as HTTP
200, a secondary limit as HTTP 200 and as a 403 with `retry-after`, a REST 429, a REST 403 with
`x-ratelimit-remaining: 0` waiting until the reset, a 403 without a rate-limit signal, a 502 then
split, a timeout split, another 5xx, a connection error and a client timeout retried then split, an
unsplittable request retried, a 404 on a compare page, a 401); `rateLimit` readings that alternate
between two windows; each query document's point cost, computed from GitHub's documented formula,
equals its 5.8 figure.

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no
invariant check without a test that targets it; changing a query document without re-recording
fails the suite; the lock check fails on an edited or removed entry (tested against a real git
repository) and runs on the exact PR head; CI passes on the exact PR head.

### Step 3: New schema, PR sync engine, cutover

Two pull requests. 3a lands the schema and removes the old pipeline, which imports the old models
and so cannot outlive them; 3b adds the sync engine and verify on that schema. Between them `main`
has no `sync` command and the tracked database stays frozen (D26).
Prerequisites for 3a: the gap 68 counter follow-up and the httpx declaration (gap 76) have merged.

Every Step 3 test of a GitHub interaction runs through the real `GitHubSource` over
`httpx.MockTransport`. Databases come from one template per test session, built by
`alembic upgrade head` and copied for each test; `metadata.create_all` is used only by the 5.1
schema comparison. Evidence is direct: planted inputs on both sides of every boundary the design
states (the coverage floor, the 10-minute unsettled window, 10 minutes past `deadline_at` for the
lock, the 24-hour refresh, the delete guard's max(25, 2%)), tampered databases, and regression
tests that fail against the parent's code; never mutation testing. Each wait on a thread or
subprocess has an explicit timeout.

#### Step 3a: New schema and cutover

Scope: a migration to the 5.1 schema except `issues` and `issue_labels`, which Step 4 adds. It drops
`pull_requests`, `pr_user_tags`, `repositories`, and `sync_failures`; creates the new tables and
views; keeps `user_tags`; recreates `pr_user_tags` empty with its foreign key to the new
`pull_requests`; renames no parent table; and aborts if either tag table holds rows (D17, D18).
`alembic/env.py` as 5.1 describes it: foreign keys off and checked, one transaction per upgrade,
`foreign_key_check` before the commit, nothing executed before `context.configure()`, dependent
views dropped and recreated around a batch alter, and the tracked file refused. The models with
conventionally named constraints, the timestamp type, the views, `SCHEMA_VERSION` with its lock
file (checked by the CI lock step against the base commit under the 5.1 rule: no base entry
deleted and no pair removed from one, pairs added to the current entry allowed), and
the packaged revision tuple (gap 74). Removal of the old pipeline per 5.7: `github/`,
`schemas/`, `db/repositories/`, the `sync` subcommands, the `search` and `tags` stubs, and every
test and fixture only they use, with `env_nested_delimiter="__"` on `Settings.model_config`
(gap 64). `cli/github.py` over `source/` (5.7, 5.8). Gaps 69 and 75 in the CLI and logging, and
gap 80's placeholder check as a pre-commit hook.
The CLI reports an invalid `tracking.toml` as a CLI error that exits 1; the current `sync all`
command raises it with a traceback. The default `DATABASE_URL` (and the commented default in
`.env.example` and the `.env` example in `docs/development.md`) moves to
`./github_activity.local.db`, `.gitignore`'s exception becomes
`!/github_activity.db` (anchored, so no other file of that name escapes `*.db`), and
`scripts/check_frozen_db.py` pins the tracked file (D26). A data dictionary
(`docs/data-dictionary.md`) covers every table and view at head, each column marked consumer or
internal (5.1), with a test that every table, column, and view in a database migrated to head has an
entry and no entry is orphaned. Documentation the cutover makes false is corrected (`CLAUDE.md`,
`CONTRIBUTING.md`, `docs/development.md`, `docs/testing.md`, and `docs/database.md`, which points
to the dictionary); `docs/architecture.md`, `docs/data-model.md`, `docs/rate-limit-handling.md`,
and `docs/github-api-quirks.md` are marked as describing removed code until Step 7.

Tests: the migration aborting when a tag table holds rows; its success on a seeded database at
`b3c4d5e6f7g8` and on a scratch copy of the tracked file; a planted failure in the second of two
revisions applied by one upgrade leaving the file at the revision the upgrade started from, with
neither applied; the migration connection reporting `foreign_keys=0`, and the upgrade refused with
nothing applied when env.py's connect listener reads anything else (the test plants, through
env.py's own engine with its DBAPI connect function patched, a connection already inside a
transaction with `foreign_keys=ON`; env.py accepts no caller-supplied connection, which would bypass
the listener and the one-transaction setup); a batch alter of `pull_requests` keeping its child
rows, through env.py with a test-only revision that drops and recreates the views; env.py refusing
the tracked file; `downgrade()` raising; `PRAGMA user_version` holding `schema_version`, the
`schema_version` lock failing on a consumer column or view column missing at head and on a consumer
column at head missing from the current entry, and the CI lock
step failing on a pair removed from a base entry (against a real git repository); the revision
tuple equal to the chain; the schema comparison (5.1), CHECK
names included; every foreign-key column leading an index; the timestamp type rejecting a naive
value and converting an aware one to UTC; view rows, on seeded data, for a null-author PR, a null
reviewer, `final_verdict` (a later `DISMISSED`, a later `COMMENTED`, order by review id), and
`v_pr_commenters` excluding reviewers; the dictionary in both directions; only `source/` importing
githubkit, and `source/` importing neither SQLAlchemy nor `db/`; `scripts/check_frozen_db.py` on
the pinned file and on a changed copy; `LOGGING__ROTATION` overriding `Settings().logging.rotation`;
a usage error, an invalid `tracking.toml` (printing no traceback), and a missing token exiting 1
through the installed entry point; an unhandled exception through the installed entry point printing
no local variable values, and every loguru handler configured with `diagnose` off (gap 75);
`github rate-limit` and `github test`; the placeholder check on planted calls and a string decoy;
the default `DATABASE_URL` and `.env.example` naming `./github_activity.local.db`; and the anchored
`.gitignore` exception (`github_activity.local.db` and a `github_activity.db` in a subdirectory
ignored).

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no
invariant check without a test that targets it; the parent's and the head's test counts are
reported with the removed modules listed; `alembic upgrade head` succeeds on a scratch copy of the
tracked file and is refused on the file itself; CI passes on the exact PR head.

#### Step 3b: PR sync engine and verify

Scope: the writer; the planner with the coverage rule, in-place updates, `sync_pending`, deletions,
guards, and their overrides; untracked repositories (D27); accepted failures; the label-set check;
the automation tables; user lookup and refresh; unsettled re-hydration; `hydration_version`
compared by inequality; run records, the run lock, and the head check (gap 74); `ghactivity sync`
and `ghactivity verify` (5.4; `--db-only` is Step 6a). `tracking.toml` gains `[coverage]`,
`[automation]`, and `[sync]` (5.5), and its "Order is the sync order" comment changes to the
resolve and index order (5.3 steps 1-2). Verify's timeline lookup in `source/` with its recordings
(5.8). The REST sampler the acceptance uses, laid out like the recorder: comparison logic in a
module under `tests/source/`, tested offline on recorded or synthetic REST bodies with one planted
mismatch per compared field, REST's `ghost` matching a null or D28-kept reference, and the
per-file sum rule; run from `scripts/`, reaching GitHub through `GitHubSource.transport`; Steps 4
and 5 reuse it. The busy-timeout comment in `db/engine.py`
(gap 77).

Tests through the sync service with a real SQLite file built by `alembic upgrade head` and a fake
GitHub: a scenario model of repositories, PRs, and accounts that renders resolve, index, batch,
overflow, compare, and lookup responses through the Step 2 replay, with `Date` headers from the
test clock. A test parses every recorded pull-request, index, user-lookup, and overflow unit into
the model, re-renders it, and requires exact JSON equality with the recording; one test runs a
first sync of go-gdpr from recordings made for it, without the model. The planner and the writer
also get direct tests. Cases: first sync, update, merge, close then delete, reopen, mass-delete halt and
its override, incomplete-index halt, an unresolvable repository, untracked repository and prune
(including no marking under `--repo`, a re-added repository re-tracked and not pruned, and a refused
prune deleting nothing with exit 2), crash mid-run then resume with committed items intact (once
by cancelling the engine in-process, and once by killing a subprocess, which leaves its row
`running`),
time-limited run and the next run finishing its `sync_pending` items, a pending item whose cause
clears, the deadline reached during the index, the deadline reached before any repository resolves,
dry run writing nothing and exiting as the run would, run lock and `--break-lock` (the lock held by
a separate `sqlite3` connection; a stale `running` row reclaimed as `aborted`; a refused claim
exiting 1 with no row; `--dry-run` taking no lock while a run is `running`), the head check
refusing an older revision and an empty file, `ghactivity sync` exiting 2 through the installed
entry point while a usage error still exits 1, unsettled re-hydration, a renamed repository
followed, queue order (pending, then open, then `updatedAt` descending), an item leaving coverage
between index and hydration skipped without a failure, a planted `pr_user_tags` row surviving
re-hydration and removed when its PR is deleted (D17), the `sync_runs` counters, every item write
on the writer's one connection, version bump re-hydrates all, ready-for-review and
author-association changes applied from the index (and not over a hydrated write), a closing link
added after merge, a PR with more than 10 links, a renamed label, a default-branch change
recomputing `targets_default_branch`, a bot seen first through a commit and a failed user lookup
(its solo lookup succeeding, and failing), an account seen in two shapes keeping its login until a
lookup, identity conflicts (a lookup `databaseId` differing from the commit link's, a review id
stored under another PR, a PR number stored under another `github_id`), a repository gone during
hydration, a database error in the writer exiting 1, a `--dry-run` started during a sync and a
verify started before the sync's claim, each reading beside the sync's item commits without
blocking them (gap 77),
an account deleted after it was stored (`deleted_at` set; its PR author, merger, review, comment,
and commit references kept through a re-hydration that returns it as null) and then seen again, an
account deleted before it was stored (null references everywhere, and a commit whose lookup returns
null stored without a linked account), an object first written after its author's deletion stored
with a null author, a different non-null commit link replacing a stored one, the 24-hour refresh
skipping an account marked deleted,
a closing link listed twice in the index planning the PR for hydration, where the duplicate fails it,
an automation-list change with no hydration changing the views, an accepted failure leaving exit 0,
one case per exit condition in 5.3 step 8, with the status and `last_outcome` each records, a
fatal run included, `sync_failures` rows removed when their item leaves
coverage, also under a deletion halt, `last_index_started_at` left unwritten for a failed,
incompletely indexed, or gone-during-hydration repository and written for a time-limited one, an
in-process interrupt closing the run as `aborted` with `last_outcome = failed` for each attempted
repository and `last_index_started_at` unchanged, view rows after a sync, each verify class, each overlapping pair of
classes taking the first in the 5.4 order, a stale item, verify's repository set, its child-count
checks (each by a planted row deletion), an unverified repository, a verify reaching
`--max-minutes` exiting 1, and its report written on exit 1; config loading for the new sections (defaults, invalid values, unknown keys, a
missing floor, a string floor, an accepted failure naming no configured repository). A few fixed
seeds each drive several runs over random GitHub changes, and after each run the database equals an
expected state computed separately from the scenario model.

Acceptance: `verify --exact` on Prebid.js, salesagent, and prebid-server; a REST sample of PRs that
includes a null author, a null merger, more than 250 commits, overflow, a duplicate path, and a
dismissed review matches field by field, with REST's `ghost` account (id 10137) matching a null
reference or a reference D28 kept from before the deletion,
per-file line counts compared only where REST's per-file sums equal the PR totals, and comment ids
and authors compared as D24 stores them (section 7, "Source vs REST"). The live run starts from a
fresh database at an explicit path, with `GITHUB_TOKEN="$(gh auth token)"` and the token never
printed; reads `ghactivity github rate-limit` before and after, and does not start with fewer than
1,500 GraphQL points remaining; runs a second, incremental sync
before verify; and records the points, REST calls, and time it spent. The sample's cases are chosen
by SQL queries recorded in the pull request; if none of the three repositories holds an in-coverage
dismissed review, prebid-server-java joins the sync and `verify --exact`. A live item failure that
cannot pass is listed in `[sync].accepted_failures` with REST evidence of why, as in Step 5. An
adversarial review of the change, by reviewers who did not write it, finds no invariant check
without a test that targets it; CI passes on the exact PR head.

### Step 4: Issues and closing-issue links

Scope: a migration for the issue tables (the first additive migration); issue index and hydration
(5.8) with `stateReason` including `DUPLICATE`; transfers (a transferred issue arrives with a new
`github_id` and number, step 7 removes the source row, and the old number returns `NOT_FOUND`); a
view joining closing references to stored issues; the data dictionary for issues.

Tests: issue first sync, update, close, and reopen; unsettled re-hydration and version bump for
issues; an issue's stored author kept when GitHub returns it as null (D28); a transfer arriving with a new id; an old number returning `NOT_FOUND`, treated as gone; a
snapshot one migration behind head upgrades and syncs.

Acceptance: `verify --exact` for issues on the same repositories; a REST sample of issues matches
field by field.

### Step 5: Full bootstrap and verification

Scope: sync all tracked repositories into a new database. Projected cost about 1,900 GraphQL points.
The database is not committed (D26); it becomes the first snapshot of release `db` in Step 6b.

Acceptance: `verify --exact` for every repository; zero invariant failures other than accepted
failures, each listed in the PR with REST evidence of why it cannot pass; a 50-item random sample
matches REST; a difference report against the tracked file classifies every differing item (D3
deletion, file-status rebuild, commit list beyond 250, login rename, changed on GitHub after the old
file's sync, in-rule item absent from the old file, schema-only) with per-category counts, zero
unclassified items, and five REST-checked examples per category; the run's time, cost, file size, and
secondary-limit responses at one request in flight are recorded (and at any wider setting proposed).

### Step 6: Scheduled sync and publication

Prerequisite: a GitHub App with read access to Contents, Pull requests, Issues, and Metadata,
installed on the tracked repositories, with its app ID, private key, and installation ID stored as
repository secrets.

Scope: 6a merges `sync.yml` with `workflow_dispatch` only, adds `ghactivity snapshot` and
`ghactivity verify --db-only` (5.4), and adds the
`githubkit[auth-app]` extra to `pyproject.toml` and `uv.lock`. Its tests: `verify --db-only`
accepting the gz and manifest `ghactivity snapshot` wrote, and halting with exit 1 on a tampered
copy; through
`httpx.MockTransport`, a mocked installation-token response whose `expires_at` lies just over a
minute ahead, a clock moved past it mid-run, and a second token request before the next API request
(githubkit keeps a token until one minute before `expires_at`); and a 401 on an API request still
exiting 1 with the run closed (5.3 step 4). 6b creates release `db` and uploads the Step 5 database
with its manifest (`ghactivity snapshot --verify-report` from Step 5's `verify --exact --report`) as
the first snapshot. It creates release `db-test` the same way, holding snapshots prepared offline:
the corrupted cases below, one with a `pr_files` row deleted (a verify error), one built at the
revision before the last migration, and one holding an extra repository absent from `tracking.toml`
(a sync exit 2). It then runs one normal manual run and one bootstrap run. 6c enables the
schedule and stops tracking `github_activity.db`: it removes the file
from the index, the `!/github_activity.db` exception from `.gitignore`, the `check-added-large-files`
exclude, the `tracked-db-rollback-journal` hook, and the D26 freeze check; replaces the tracked-file
journal test with one on the snapshot `VACUUM INTO` writes; records the D20 WAL decision for the
working file; and documents how to download the latest snapshot.

Acceptance: two consecutive scheduled runs publish, and their manifests list drift, pending, and
failure counts; the bootstrap run publishes, and the next scheduled run starts from its snapshot; a
run makes requests more than 60 minutes after its first installation-token mint, and its log
records at least two mints; against release `db-test`, the snapshot one migration behind head
upgrades and publishes, the snapshot with an untracked repository publishes and the workflow is
marked failed, the snapshot with a deleted `pr_files` row publishes nothing and the workflow is
marked failed, a re-run of `publish` after an interrupted upload completes, and each of these halts
without publishing: a truncated gz, a database changed after
its manifest was written, a manifest whose counts disagree, and, each with a matching manifest, a gz
that does not decompress, a database failing `integrity_check`, one with a foreign-key violation,
and one at a revision unknown to the code.

### Step 7: Documentation rewrite

Scope: README, `CONTRIBUTING.md`, `CLAUDE.md`, and `docs/` rewritten from the code; the data
dictionary from Steps 3-4 completed with the coverage rule and metric definitions; no hardcoded
counts.

## 7. Evidence

Measured 2026-10-16 unless noted. "Index" means the created-order enumeration in section 5.3.

| Topic | Fact | How measured |
|-------|------|--------------|
| Index exactness | 38,195 items across 18 repositories x {PRs, issues}; every scan's item count equals `totalCount`; creation order monotonic | full index |
| Index cost | 404 GraphQL points; Prebid.js PRs alone 12,652 items, 127 points, 125 s | full index |
| `updatedAt` identity | GraphQL `updatedAt` equals REST `updated_at`: 5,599 equal, 68 newer on GitHub, 0 older (Prebid.js) | index vs stored rows |
| Change signal | Over 1,400 recently updated PRs, events landing exactly on `updatedAt`: comments 168, labels 59, unlabels 10, review requests 23, convert to draft 9, force pushes 11, title edits 13, merges 425, closes 547. Never bumping it: cross-references (472 after). Rare: assignment 2 of 268 after; one review 6 s after. Ready-for-review: 0 of 151 after. Not bumping the item at all: closing links added later, label renames, author-association changes (rows below) | GraphQL timeline scan |
| Close and merge stamps | 7,241 closed issues with `closedAt <= updatedAt`; 25,285 merged PRs with `mergedAt <= updatedAt`; no counterexample | index |
| Commit cap | REST PR commits and GraphQL `commits` stop at 250; GraphQL returns the newest 250; paged compare returned 1,535 of 1,535 (`salesagent#1757`), 1,374 and 1,371 on open PRs; attribution identical on 250 of 250 shared commits | live API |
| Compare paging | Docs: without `per_page` or `page` the list is limited to 250 commits | GitHub REST docs |
| Hydration cost | 25 heaviest salesagent PRs: 2 points, about 9 s, 5 of 5 runs succeeded; 10 PRs: 1 point, 5 of 5; 100 issues: 1 point. Cost depends only on the query shape; heavy items cost time, not points | repeated live batches |
| Nested connection cost | Same 25 PRs, only change is `authors(first: 5)` inside `commits(first: 100)`: 2 points without, 27 with | live single-variable comparison |
| Cost formula | GitHub's documented formula (requests per connection, nested ones multiplied by the parents' `first`, divided by 100, rounded, minimum 1) gives 2 for a 25-PR batch, 1 for an index page, and 27 for the nested-connection variant, the measured values; `tests/source/test_documents.py` pins each 5.8 document's cost | `tests/source/graphql_cost.py` |
| Unused fragments | GitHub rejects a document that defines a fragment it does not use ("Fragment Actor was defined, but not used") | live overflow request, 2026-10-17 |
| Node limit | A query requesting more than 500,000 possible nodes answers HTTP 200 with no `data` and `errors[].type` `MAX_NODE_LIMIT_EXCEEDED` ("... requesting up to 1,000,000 possible nodes which exceeds the maximum limit of 500,000"); `source/` treats it like a timeout and splits the request | live oversized query on go-gdpr, 2026-10-17 |
| GraphQL timeouts and resource limits | Over 10 s: "a timeout response and a message reporting that 'We couldn't respond to your request in time'"; "you may receive either a `502` or `504` status code". Too many resources: "partial results along with an error indicating that resource limits were exceeded". No error type names are documented | GitHub GraphQL docs, "Rate limits and query limits for the GraphQL API", read 2026-10-17 |
| Persistent rate limits | "If your request continues to fail due to a secondary rate limit, wait for an exponentially increasing amount of time between retries, and throw an error after a specific number of retries." A primary or secondary limit answers "a `403 Forbidden` or `429 Too Many Requests`" | GitHub REST docs, "Best practices for using the REST API" and "Troubleshooting the REST API", read 2026-10-17 |
| Label paging | `REPOSITORY_LABELS` (5.8) returns golang/go's second label page (44 of 144); no tracked repository has more than 100 labels (largest: prebid-server, 56) | live recording and query, 2026-10-17 |
| Comment ids | `IssueComment` serves `fullDatabaseId` (BigInt, not deprecated) alongside `databaseId` and the node `id`; `prebid-server#4343`'s first comment: `fullDatabaseId` "2890398517" | live introspection and query, 2026-10-17 |
| Comment volume | The 17 public tracked repositories hold 29,559 PRs with 43,519 conversation comments (Prebid.js 23,352, prebid-server 8,425, prebid.github.io 7,622, salesagent 1,634; at most 149 on one PR), against 76,410 reviews and 272,869 commits, which are already stored one row each. 43,519 `pr_comments` rows (no text) with their unique and foreign-key indexes take 2.3 MiB in SQLite, 56 bytes per row | live `totalCount` per PR, 601 points, 2026-10-17; local SQLite |
| Timeouts as HTML | Pages of 100 PRs selecting four nested `totalCount` values answered HTTP 502 with nginx's HTML "502 Bad Gateway" page, not JSON; `source/` classifies by status, so the body's form does not matter | live, prebid-server, 2026-10-17 |
| Lax parsing | pydantic's default (lax) mode reads `"no"` as `False` for a boolean and `"12"` as 12 for an integer | installed pydantic 2.12.5, `TypeAdapter(bool).validate_python("no")` |
| Source vs REST | 43 PRs (seeded sample of the indexes of Prebid.js, prebid-server, salesagent, prebid-mobile-android, prebid.github.io, go-gdpr, plus the recorded fixture PRs) hydrated through `source/` and read again through REST: state, ids, draft, title, body, author, merger, base ref, additions, deletions, changed files, commit count, author association, labels, reviews (id, state, reviewer), files (path, additions, deletions), commit SHAs and linked authors (up to 250), and commenters equal on 40. The 3 differences are REST's: it reports a deleted account as `ghost` (id 10137) where GraphQL returns null (`Prebid.js#12975`), and it reports 0 additions and deletions for some files of large PRs (`salesagent#1501`: 132 of 1,504 files; `#1721`: 93 of 1,473), where GraphQL's per-file counts sum exactly to the PR totals | live cross-check, 2026-10-17 |
| Partial errors | A batch with a missing number returns data for the other aliases plus `errors[{type: NOT_FOUND, path}]`; `issue(number:)` on a PR's number also returns `NOT_FOUND` | live API |
| githubkit 0.14.2 | `graphql.arequest` raises `GraphQLFailed` on any error (its `.response` is the parsed body, without HTTP status or headers) and `PrimaryRateLimitExceeded` on `RATE_LIMITED`; `GitHub.arequest` returns data and errors together; its retry covers 403/429 rate-limit responses and 5xx only; defaults `auto_retry` on (rate limit once, 5xx three times), `http_cache=True`, `follow_redirects=True`, no timeout; accepts `async_transport` | installed source; probe |
| Item ids | `PullRequest.databaseId` and `PullRequestReview.databaseId` are deprecated: "`databaseId` will be removed because it does not support 64-bit signed integer identifiers. Use `fullDatabaseId` instead. Removal on 2024-07-01 UTC." Both are still served. `fullDatabaseId` is a BigInt returned as a JSON string. Prebid.js#15779 returns 4,891,210,785 (above 2^31) from both, equal to its REST `id`. `databaseId` on `User`, `Bot`, `Organization`, and `Mannequin` is not deprecated | live introspection; live query and REST |
| Actor types | `Actor` has five implementers: `Bot`, `EnterpriseUserAccount`, `Mannequin`, `Organization`, `User`; `EnterpriseUserAccount` has no `databaseId` (it has `user`) | live introspection |
| One account, two shapes | dependabot (49699333) is `Bot`/`dependabot` as the author of `Prebid.js#15724` and `dependabot[bot]` as its commit's `GitActor.user` (declared type `User`); REST `pulls/15724/commits` gives `dependabot[bot]`, type `Bot`; `nodes(ids:)` on the commit user's node id returns `Bot`/`dependabot` | live GraphQL and REST |
| Duplicate paths | `Prebid.js#13481`: `changedFiles` 2, `files.totalCount` 3 (`CLAUDE.md` deleted, `CLAUDE.md` added, `package-lock.json` modified), so `changedFiles` is not the file-list length | live query |
| Deleted-account comments | `Prebid.js#12975`: PR author null; 1 of its 2 conversation comments has a null author | live query |
| Dismissed verdicts | `prebid-server-java#4637`: Lightwood13 submitted CHANGES_REQUESTED (5276426920), then a review now DISMISSED (5289072517); `latestOpinionatedReviews` lists only the two APPROVED reviewers. A rule taking the latest APPROVED or CHANGES_REQUESTED review would report CHANGES_REQUESTED | live query |
| Late closing links | `prebid-server-java#4380`, `#4350`, `#4397`: a `ConnectedEvent` on 2026-08-14 linked an issue months after merge, and the PR's `updatedAt` did not change. Adding `fullDatabaseId`, `authorAssociation`, and `closingIssuesReferences(first: 10)` with `nameWithOwner` keeps an index page at 1 point | timeline scan; live index page and `rateLimit(dryRun: true)` |
| Label renames | The prebid-mobile-ios label "4.0 In-App Platfrom" became "4.0 In-App Platform"; issue #1030, which carries it, kept its `updatedAt` | REST events; live query |
| Author association | GraphQL and REST agree on 100 of 100 PRs; on Prebid.js, the earliest merged PR of each of 60 recent first-time authors reads CONTRIBUTOR (59) or MEMBER (1), not FIRST_TIME_CONTRIBUTOR, so the value is computed when read, not stored at creation. `Prebid.js#15715`, merged, whose commits link to no account, still reads FIRST_TIME_CONTRIBUTOR (GitHub: "Author has not previously committed to the repository.") | live queries; introspection |
| Issue transfer | `prebid.github.io#6053`, transferred from Prebid.js on 2025-05-23, has `databaseId` 3087307383, in the range of issues created that day rather than of its July 2024 creation; GraphQL returns `NOT_FOUND` for `Prebid.js#13150`, while REST follows that number to `prebid.github.io#6052` | live query; REST |
| Activity during a run | Over 3 days, at least one in-coverage item was updated in 13% of 5-minute windows and 31% of 15-minute windows (a lower bound: only each item's latest update is visible) | 100 most recently updated PRs and issues per public repository |
| Rate-limit windows | One personal token's GraphQL `rateLimit` readings alternated between two windows with different `remaining` and `resetAt` within seconds; REST `/rate_limit` reported one of them. GitHub: "Because GitHub processes API requests in multiple regions, rate limit values can vary from one response to the next based on the location handling the request." | consecutive `rateLimit(dryRun: true)` readings; GitHub docs |
| Installation tokens | "The installation access token will expire after 1 hour." githubkit's `AppInstallationAuthStrategy` mints a new token before expiry and needs PyJWT and cryptography, which the current environment lacks | GitHub docs; installed githubkit source; `importlib.util.find_spec` |
| Secondary limits | GraphQL may use no more than 60 s of CPU time per 60 s of real time; GitHub advises avoiding concurrent requests; a request taking more than 10 s is terminated. A GraphQL secondary limit returns HTTP 200 or 403, a REST one 403 or 429, each with an error message; GitHub's handling: `retry-after` if present, else the reset time when `x-ratelimit-remaining` is 0, else at least one minute | GitHub docs |
| Nested settings | `SYNC__COMMIT_BATCH_SIZE=50`, `PACING__BURST_ALLOWANCE=99`, and `LOGGING__ROTATION` leave `Settings` at its defaults (25, 10, `10 MB`); `Settings` sets no `env_nested_delimiter` | probe |
| Logging placeholders | 51 of 56 `logger.<level>(...)` calls in `src/` pass `%`-style placeholders, all under `github/` | AST count over `src/` |
| Prototype | Design-equivalent prototype over salesagent and prebid-server: 2,625 of 2,625 PRs and 2,038 of 2,038 issues written; 0 failures; every count invariant held; 406 GraphQL points, 181 REST calls, 950 s sequential | prototype run |
| Edge cases seen | Null PR author 3, null reviewer 7, null issue author 1; overflow pages: files 139, commits 20, reviews 13, comments 1; compare fallback 18; null `mergedBy` on merged PRs (`Prebid.js#13769`, `#14683`) | prototype run; live API |
| Parity | 2,461 PRs compared with the existing file: state, merger, commit count, file count, additions equal; differences are truncated or dropped commit lists, account renames, and changes on GitHub since the last sync | prototype vs existing file vs REST |
| Size | A different prototype run from the row above (2,649 PRs plus 2,049 issues): 19.5 MiB raw, 6.1 MiB gzip. Existing file 52.5 MiB raw, 7.4 MiB gzip | local |
| Shared session | After a mid-run commit, 80 of 100 concurrent reads raise `InvalidRequestError` (session provisioning a new connection) | probe |
| Commit timing | Through `BulkPRIngestionService`, 0 rows committed after about 45 of 60 PRs; all 60 at the end | probe |
| `classify_tags` | Re-syncing an open PR sets it to `None` | probe |
| `sync_failures` | A second resolve of the same PR raises `IntegrityError` | probe |
| Search | `issueCount` 2,198 returns 1,000 nodes; `prebid-integration-monitor#8` absent from search | live API |
| Rate limits | Personal token 5,000 per hour; `GITHUB_TOKEN` 1,000 per hour per repository (REST and GraphQL); GitHub App installation 5,000 rising to 12,500 | GitHub docs |
| Conditional requests | A 304 response does not consume quota (3 conditional requests, used count unchanged) | live API; GitHub docs |
| Permissions | Compare: Contents read; list PRs: Pull requests read; get PR: Pull requests read or Contents read; delete release asset: Contents write | GitHub docs |
| Publishing repository | `prebid/github-activity-db` is public, so its release assets are world-readable (D9) | `gh repo view` |
| Release assets | Each file under 2 GiB; at most 1,000 assets per release; an upload that ends in a 502 "may leave an empty asset with a state of starter"; the asset `digest` field is "string or null"; GitHub renames asset names containing special characters; `gh release upload --clobber` deletes before upload ("If the upload fails, the original assets will be lost.") | GitHub docs; gh source |
| Large files | Git warns above 50 MiB; GitHub blocks files above 100 MiB | GitHub docs |
| Schedules | Public-repo schedules disable after 60 days without repository activity; start of hour is high load; queued jobs may be dropped; `workflow_dispatch` requires the workflow on the default branch | GitHub docs |
| Concurrency | A new run replaces a pending run in the group; running runs are cancelled only with `cancel-in-progress: true` | GitHub docs |
| App token action | `actions/create-github-app-token` v3.2.0 (2026-05-12) and v3.3.0 (2026-10-12); the sync no longer uses it (5.6) | GitHub API |
| SQLite pragmas | The `connect` listener on `engine.sync_engine` sets per connection through aiosqlite: `foreign_keys=1` and `busy_timeout=30000`. Without the listener a connection reports `foreign_keys=0` and `busy_timeout=5000` (Python `sqlite3` default `timeout=5.0`). A file once set to WAL stays WAL (header bytes 18-19 `0202`; rollback `0101`). A foreign-key violation is rejected and `ON DELETE CASCADE` removes children; the same values hold on every session from `get_session()`, and routing `get_engine()` around `build_engine()` fails 2 tests in `tests/db/test_application_engine.py` | `tests/test_db_engine.py` (`TestSqliteConnectionSettings`), `tests/db/test_application_engine.py`; mutation run |
| Connect-listener failure | When a statement in the `connect` listener raises, SQLAlchemy 2.0.45 does not close the DBAPI connection and the aiosqlite 0.22.1 worker thread keeps the process alive. With `PRAGMA journal_mode=DELETE` in the listener, `ghactivity sync retry --dry-run` against a database held with `BEGIN EXCLUSIVE` printed "database is locked" and was still running at 60 s; without that statement, and separately with the connection closed on failure, it exited 1 after 31.6-31.8 s (the 30 s busy timeout). With the close in place and the statement restored, a WAL database held open by another process fails every connection with "database is locked" (exit 1 after 0.5 s), where without the statement the CLI uses the file | CLI runs against a locked file |
| Build constraints | `uv build --build-constraints build-constraints.txt --require-hashes` builds the wheel; with both listed hatchling hashes altered it exits 2 (hash mismatch); with only the wheel's hash altered it succeeds, because uv rejects the wheel and builds hatchling from its sdist, whose hash still matches | local builds with uv 0.7.10, no cache |
| Packaged config | The wheel built from the Step 1 branch (merged as `8c5b16b`) contains `github_activity_db/tracking.toml`; installed with the locked dependencies and run outside the checkout, `Settings().tracked_repos` returns the 18 repositories. A wheel built from `7555a6d` raises `TrackingConfigError` for `<env>/lib/python3.13/config/tracking.toml`; a wheel excluding the file fails the same check | `uv build`, `scripts/wheel_smoke.py` in a new environment |
| All-skipped runs | pytest exits 0 when every collected test is skipped; the previous inline gate exited 0 on that report and `scripts/check_junit.py` exits 1 | report written by a real pytest run |
| Suppression spellings | ruff 0.14.11 honors `# noqa` in any letter case, `#noqa`, `# noqa:F401`, a directive after other comment text, and file-level `# ruff: noqa`, `# ruff: noqa: F401`, `#ruff:noqa`, `# flake8: noqa[: codes]`, but not `# RUFF: NOQA`; `PGH004` flags only the blanket forms. mypy 1.19.1 honors `# type: ignore`, `# type:ignore[...]`, `#type:ignore`, a first-line `# type: ignore`, `# mypy: ignore-errors`, and `# mypy: disable-error-code=...`, but not `# TYPE: IGNORE`, `#mypy:ignore-errors`, or `# type: ignore` after other comment text. The counter also counts `# RUFF: NOQA`, `#mypy:ignore-errors`, and the trailing `# type: ignore`, and does not count `# TYPE: IGNORE`. ruff also stops I001 for `# isort: skip_file`, `# isort: off`, and their `# ruff: isort:` forms, and for `# isort: skip` on the first of two out-of-order imports (0 findings; on the second, 1 finding remains); mypy reports no error inside a `@no_type_check` function, also when the decorator is imported under another name or decorates an `async def` | probe files with `ruff check --isolated` and `mypy --strict` |
| Range suppressions | ruff 0.14.11 with `--preview` honors `# ruff: disable[F401]` on its own line or indented, with or without spaces around `:` and before `[`, and with several codes (`#ruff:disable[F401, E501]`); `# ruff: enable[F401]` ends the range. It does not honor it without `--preview`, in uppercase or mixed case, with no or empty codes, at the end of a code line, or after other comment text. The counter counts every `# ruff: disable` in any letter case and no `# ruff: enable` | probe files with `ruff check --isolated --select F401`, with and without `--preview`, 2026-10-17 |
| Existing file | 0 foreign-key violations; integrity check `ok` | local |
| WAL snapshots | `VACUUM INTO` from a WAL database writes a rollback-journal file (header bytes 18-19 `0101` vs `0202`) that can be queried read-only from a read-only directory | sqlite3 |
| Test-suite flake | pytest-asyncio 1.3.0 on Python 3.13 leaves an unclosed event loop after an async test followed by a test calling `asyncio.run()`; with `filterwarnings = error` the next test fails. Fixed in pytest-asyncio 1.4.0 (#724) | repeated runs on `22551d3` (3 of 3 failed) and with 1.4.0 (5 of 5 passed); release notes |
| WAL with a tracked file | SQLite: any `*-wal` file must be copied together with the database, and swapping or moving journal files between databases is likely to corrupt them | sqlite.org/howtocorrupt.html |
| CI color forcing | typer sets `FORCE_TERMINAL` when `GITHUB_ACTIONS`, `FORCE_COLOR`, or `PY_COLORS` is set; two help-text tests failed only on GitHub Actions and reproduced locally with `GITHUB_ACTIONS=true` | installed typer `rich_utils.py`; CI run on `c7e0146` |
| Alembic on SQLite | alembic 1.18.1, SQLAlchemy 2.0.45, SQLite 3.45.3; two revisions, the second dropping the first's table, creating another, and inserting a row. Default handling, second revision failing: the first table dropped and the second created empty, with the first revision stamped. A statement executed before `context.configure()`: `upgrade` exits 0 with no revision stamped and no row inserted. Transactional DDL (the driver's transaction handling off, `BEGIN` emitted on `begin`, `transactional_ddl=True`), second revision failing: nothing applied, not even the version table | probe through a real script directory and `env.py`, 2026-10-17 |
| Isolation level | Assigning `isolation_level = None` to a Python `sqlite3` connection inside a transaction commits it, after which `PRAGMA foreign_keys=OFF` takes effect; set before the assignment, inside the transaction, the pragma leaves `foreign_keys` at 1 | probe, Python 3.13, SQLite 3.45.3 |
| Batch alter and views | `batch_alter_table(..., recreate="always")` on a table a view reads raises "error in view v: no such table: main.p"; with `PRAGMA legacy_alter_table=ON` it succeeds | probe, same versions |
| CHECK constraints | Batch recreate keeps `CONSTRAINT ck_k_b CHECK (b < 10)` and drops the unnamed column-level `CHECK (a > 0)` and table-level `CHECK (n < 5)`. SQLAlchemy renders `Boolean` as `BOOLEAN` and `Enum` as `VARCHAR(6)`, without a CHECK, on SQLite. Under a `ck` naming convention an explicit `name="ck_prs_state"` becomes `ck_prs_ck_prs_state`, and `op.f("ck_prs2_state")` stays as given. `compare_metadata` reports nothing against a table with an unnamed `UNIQUE (name)` under an ix/ck/fk convention, and `add_constraint` under a `uq` convention | probe, same versions |
| SQLite timestamps | SQLAlchemy's SQLite `DateTime` stores `2024-01-01 10:00:00.000000` (26 characters; microseconds always written) and stores 12:00+02:00 as `2024-01-01 12:00:00.000000`, dropping the offset | probe, same versions |
| Usage-error exit code | `ghactivity sync --no-such-option` and an unknown command exit 2 (click 8.3.1, typer 0.21.1) | CLI at `c1f474b` |
| Lax datetimes | pydantic 2.12.5 `AwareDatetime` in lax mode accepts a TOML `floor` given as the string `"2023-01-01T00:00:00Z"` or the integer 1672531200; strict mode rejects both and accepts a TOML offset date-time | probe |
| Locals in tracebacks | typer 0.21.1 defaults `pretty_exceptions_show_locals` to true, and `cli/app.py` does not override it; `logging.py` sets `diagnose=True` on both stderr handlers, and its optional file handler passes none, so it takes loguru's default (`LOGURU_DIAGNOSE`, true) | installed typer signature and loguru `_defaults.py`; source at `c1f474b` |
| Half-read queries | With a cursor that has fetched 1 of 1,000 rows on one connection, a commit on a second connection (busy timeout 0.5 s) raises "database is locked". SQLAlchemy's aiosqlite dialect fetches every row and closes the cursor inside `execute` unless the cursor is server-side | probe, Python `sqlite3`, rollback-journal mode; installed `sqlalchemy/dialects/sqlite/aiosqlite.py` |
| Run claim | While one connection holds `BEGIN IMMEDIATE` with an uncommitted `running` row, a second claimant using `BEGIN IMMEDIATE` waits, then refuses. With a deferred `BEGIN`, the claimant reads no `running` row and its insert fails at once with "database is locked"; the holder's commit then fails too only while the claimant keeps its transaction open, and succeeds once the claimant rolls back or closes | probe, Python `sqlite3` |
| Loguru placeholders | ruff 0.14.11 PLE1205 flags `logger.warning("brace {}", value)` and passes `logger.warning("percent %s", value)` for `logger = get_logger(__name__)` | `ruff check --isolated --select PLE1205,PLE1206,G` on a probe file |
| Tracked database | `github_activity.db` at `c1f474b`: SHA-256 `c18b2bc50a7e60f822cff185723cabc2846da31bcd8814aa9df46dca0d1b4751`, revision `b3c4d5e6f7g8`, 14,669 pull requests, 0 `user_tags` and 0 `pr_user_tags` rows, `user_version` 0, journal mode `delete` | `shasum -a 256`; `sqlite3` on a copy |
| Quality gates | At `43fcf06`, before Step 1: no CI; pre-commit hooks not installed; ruff 0.14.11 (lock) 10 findings plus 4 format diffs vs ruff 0.8.6 (pre-commit pin) 0 findings plus 1 format diff; mypy clean on 91 files; 608 tests, 0 failures | local |
| Private data | `prebid-agent-skills` is private; the committed file contains its PRs since commit `bf40280` | `gh repo view`; history |

## 8. Open items

| Item | Resolution point |
|------|------------------|
| Intermittent GraphQL failure rate at full scale (prototype needed 0 retries) | Step 5 measures it |
| Secondary-limit responses with more than one request in flight | Step 5 measures before any wider setting |
| Whether a heartbeat commit counts as repository activity for schedule disabling (undocumented); a disabled schedule fails no run | Not provable inside Step 6; the heartbeat is best effort; a consumer-side check of manifest age is later work (section 9) |
| Issues read permission for the App | Step 6b's first manual run exercises it with a real token |
| GraphQL `rateLimit` readings alternating between two windows (seen with a personal token; GitHub documents that values vary by the region handling a request, but not differing `resetAt` values) | Step 2's guard handles it; Step 6 records App-token readings |
| Whether body edits, base-ref changes, review dismissals, and issue label events always change `updatedAt` (not measured; a state change is caught by the index) | A change that does not is missed until the item's next update or a `hydration_version` bump; accepted |
| The error body of a GraphQL secondary limit returned as HTTP 200 (not observed; documented only as "an error message") | Step 2 classifies by type and message; a message it does not recognize is "not understood" (5.3 step 4) and stops the run with exit 1, naming the message; Step 5 records secondary-limit responses |
| A PR file list above 3,000 entries (no tracked PR found) | Validation fails the item if `files` stops short of `totalCount`; Step 5 reports any |
| Compare fallback on a PR whose head fork was deleted (only a live fork tested) | A count mismatch fails the item; Step 5 reports any |
| Full bootstrap time, cost, and size are projections from two repositories | Step 5 measures them |
| Overflow continuations per request (25), the server retry delays (2, 4, 8 s), the reset margin (1 s), retry jitter (25%), the index boundary margin (60 s), the budget reserves (100 points, 100 calls), the longest accepted rate-limit wait (65 min), and the HTTP timeouts (read 30 s, connect 10 s) are chosen, not measured | Step 5 records secondary-limit responses, budget readings, and request times at these values |
| Whether Prebid.js, salesagent, or prebid-server holds an in-coverage dismissed review (the plan's example, `prebid-server-java#4637`, is in none of them) | Step 3b's acceptance queries its database; without one, prebid-server-java joins the acceptance run |
| Whether REST compare and GraphQL give one account's node id in the same form (the recordings use the `MDQ6...` form in both); a lookup checks the returned id against the requested one | A mismatch fails the waiting item, which exit 2 makes visible; Step 3b's acceptance reports any |
| The cost of one commit per item in rollback-journal mode, and of a live Step 3b acceptance run (not measured) | Step 3b's acceptance records its points, REST calls, and time; Step 5 measures the full run |

## 9. Later work

Dashboard integration and replacement of its data layer, a query API, AI enrichment with provenance,
company affiliation, CI check status, releases, additional repositories, path-based exclusion of
generated or data files from line metrics (per-file stats make this possible), and a consumer-side
check that the latest manifest is recent (a disabled schedule fails no run).

Data that cannot be re-fetched from GitHub (user tags, AI enrichment) must be keyed by GitHub
identity (PR or issue `github_id`, user `github_id`), must not cascade from mirror rows, must be
produced where the published file is built, and must be copied from the last good snapshot across a
bootstrap. D28's attribution of deleted accounts cannot be re-fetched either, but it is kept in the
mirror rows and accepted as lost when a row is re-created (D28), so these rules do not cover it;
besides it, D18's "nothing that cannot be re-fetched" holds.
