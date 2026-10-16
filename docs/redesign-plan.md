# Redesign Plan

This is the plan of record for rebuilding the GitHub Activity DB as the data layer for the Prebid
activity dashboard. It records the decisions, the design, the execution steps, and the evidence each
design choice rests on. Update the step status table as work lands; do not rewrite history in it.

Measured against `main` at `43fcf06` and live GitHub on 2026-10-16. Counts that drift (PR and issue
totals) are dated; regenerate them with `ghactivity verify` once Step 3 lands.

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
| 1 | Guardrails | in progress |
| 2 | GitHub source layer | not started |
| 3 | New schema, PR sync engine, cutover | not started |
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
| D3 | Pull requests closed without merge are not stored. A tracked PR that closes without merge is deleted; a reopened PR is stored again on its next update. |
| D4 | Requested reviewers are not stored. A reviewer is a user who submitted at least one non-pending review (APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED). The PR author and automation accounts are excluded from reviewer metrics. |
| D5 | Conversation-only commenters are stored as identities with a comment count, separate from reviewers. Comment text is not stored. |
| D6 | No review or comment timestamps. Stored dates: PR created and merged; issue created and closed. Review order comes from the review id. |
| D7 | No timeline history. The database holds the current state of each item. |
| D8 | Labels are stored as currently applied, with no history. Labels are meaningful while a PR is open. |
| D9 | The database file is published as a GitHub release asset. Data from the private tracked repository (`prebid-agent-skills`) is included. |
| D10 | The scheduled sync authenticates as a GitHub App installed on the `prebid` organization. |
| D11 | The scheduled sync runs every 6 hours. |
| D12 | `classify_tags` and `ai_summary` are removed. AI enrichment returns later as a separate table with provenance. |
| D13 | No repositories are added. CI check status and releases are out of scope. |
| D14 | Assignees and requested teams are not stored. Assignment does not reliably change `updated_at` (2 of 268 observed assignment events landed after it), so the value cannot be kept current. |
| D15 | Commit co-authors are not stored. Requesting them inside the commit list raises GraphQL cost from 2 to 27 points for the same 25 PRs. |
| D16 | The request-pacing stack (`github/pacing/*`) is retired at cutover. The new sync makes about 400 GraphQL calls per run and reads its budget from each response. |
| D17 | `user_tags` and `pr_user_tags` stay (empty), re-pointed at the new `pull_requests` table. The `search` and `tags` CLI stubs are removed. |
| D18 | The database is rebuilt from GitHub, not migrated row by row. The current file holds nothing that cannot be re-fetched (0 user tags, 0 sync failures, 0 AI values). |
| D19 | PR and issue bodies are stored. |

## 3. Coverage rule

Floor `F = 2023-01-01T00:00:00Z`, set in `config/tracking.toml`.

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
| 2 | No GitHub user id or account type; bots indistinguishable | Steps 1, 3: `github_users` keyed by GitHub id; automation flag from account type plus a configured list. `prebidjs-release` (99054218) and `claude` (81847) are type `User`. Logins change: `blueseasx` became `blueseasx-prebid`. |
| 3 | Review order lost; final reviewer state unknown | Step 3: one row per review keyed by review id; `v_pr_reviewers` derives the final verdict |
| 4 | Validation silently drops commits (`author: {}`), reviews (`user: null`), files (`sha: null`) | Steps 2, 3: no silent skips; failures are recorded. GraphQL file objects carry no `sha`; unlinked commit authors keep git name and email |
| 5 | 10,895 PRs with file status `unknown` and zero per-file line counts | Step 5: rebuild from GitHub |
| 6 | Closed-unmerged PRs partially stored | D3 |
| 7 | `reviewers` holds pending review requests | D4 |
| 8 | Commenters not captured | Inline review comments already belong to reviews (168 of 168 sampled). Conversation-only commenters: D5 |
| 9 | No review timestamps | D6 |
| 10 | Commit lists truncated at 250 | Step 2: compare endpoint, paged, count asserted |
| 11 | Coverage floor undefined; partial history | Section 3; `verify` |
| 12 | AI fields empty and erased by re-sync | D12 |
| 13 | Draft and accuracy fields discarded | Step 3: `is_draft`, `base_ref`, `targets_default_branch`, `author_association`, `is_cross_repository`, automation flags, per-file stats |
| 14 | Timeline | D7 |
| 15 | Users table | Step 3 |
| 16 | Repository identity by name; tracked list hardcoded | Steps 1, 3: repository GitHub id; config file |
| 17 | PR to closing-issue links | Steps 3, 4 |
| 18 | No scheduled sync | Step 6 |
| 19 | No sync run record | Step 3: `sync_runs`, `repo_sync_state` |
| 20 | API cost (4+ calls per PR before skip checks) | Steps 2, 3: full index plus batched GraphQL |
| 21 | `sync_failures` unique key raises on a second resolve | Step 3: one row per item |
| 22 | One `AsyncSession` shared by concurrent tasks | Step 3: single writer with its own session |
| 23 | Open-PR sweep refetches every open PR each run | Replaced by the full index |
| 24 | Child data stored as JSON blobs | Step 3: normalized tables |
| 25 | Missing indexes | Step 3 |
| 26 | `foreign_keys` off, rollback journal | Step 1 |
| 27 | Model and migration drift (`alembic check` fails) | Step 1 |
| 28 | No query layer | Later (section 9) |
| 29 | 14-day freeze after merge | Removed in Step 3 |
| 30 | Documentation inaccurate | Every step; Step 7 |
| 31 | Bulk sync commits nothing until a repository finishes | Step 3: per-item transactions; crash-resume test |
| 32 | No CI; pre-commit hooks not installed; two ruff versions disagree | Step 1 |
| 33 | Existing file misses in-rule PRs and holds stale logins | Step 5 rebuild |
| 34 | GitHub search caps at 1,000 results and omits items | `verify` uses connection totals and set equality, never search |
| 35 | githubkit `graphql.arequest` discards partial results | Step 2 |
| 36 | `gh release upload --clobber` deletes before uploading | Step 6 |
| 37 | Public-repo schedules disable after 60 days without activity; start-of-hour runs delayed or dropped | Step 6 |
| 38 | The same path can appear twice in one PR's file list | Step 3: file rows keyed by ordinal |
| 39 | Null PR author, reviewer, issue author, and merger occur | Step 3: nullable references |
| 40 | A review can be submitted seconds after the PR `updatedAt` | Step 3: unsettled re-hydration |
| 41 | Issue transfers between repositories | Step 4: upsert by GitHub id; deletions recomputed at delete time |
| 42 | `uv sync --frozen` without `--extra dev` uninstalls dev tools | Step 1 |
| 43 | Pre-commit `type: ignore` and `noqa` audits always exit 0 | Step 1 |

## 5. Design

### 5.1 Schema

Every table has its own integer primary key. GitHub identifiers are unique columns, never primary keys.
Foreign keys use `ON DELETE CASCADE` and are enforced per connection.

| Table | Columns | Keys and constraints |
|-------|---------|----------------------|
| `repositories` | `github_id`, `owner`, `name`, `full_name`, `repo_group`, `is_private`, `is_archived`, `default_branch` | `github_id` unique |
| `github_users` | `github_id`, `login`, `type`, `is_automation` | `github_id` unique |
| `pull_requests` | `repository_id`, `number`, `github_id`, `node_id`, `state`, `is_draft`, `title`, `body`, `author_id`, `author_association`, `created_at`, `merged_at`, `merged_by_id`, `base_ref`, `targets_default_branch`, `is_cross_repository`, `additions`, `deletions`, `changed_files`, `commits_count`, `github_updated_at`, `hydrated_at`, `hydration_version` | unique (`repository_id`, `number`); `github_id` unique; `state` in (`OPEN`, `MERGED`); `merged_at` set iff `MERGED`; author and merger nullable |
| `pr_labels` | `pr_id`, `name` | PK (`pr_id`, `name`) |
| `pr_reviews` | `github_id`, `pr_id`, `reviewer_id`, `state` | `github_id` unique; reviewer nullable; `PENDING` never stored |
| `pr_commenters` | `pr_id`, `user_id`, `comment_count` | PK (`pr_id`, `user_id`) |
| `pr_commits` | `pr_id`, `sha`, `author_id`, `author_name`, `author_email`, `authored_at` | PK (`pr_id`, `sha`); author nullable |
| `pr_files` | `pr_id`, `ordinal`, `path`, `change_type`, `additions`, `deletions` | PK (`pr_id`, `ordinal`) |
| `pr_closing_issues` | `pr_id`, `issue_repo_github_id`, `issue_repo_full_name`, `issue_number` | PK (`pr_id`, `issue_repo_github_id`, `issue_number`) |
| `issues` | `repository_id`, `number`, `github_id`, `state`, `state_reason`, `title`, `body`, `author_id`, `created_at`, `closed_at`, `github_updated_at`, `hydrated_at`, `hydration_version` | unique (`repository_id`, `number`); `github_id` unique; `state` in (`OPEN`, `CLOSED`) |
| `issue_labels` | `issue_id`, `name` | PK (`issue_id`, `name`) |
| `sync_runs` | `started_at`, `finished_at`, `status`, `trigger`, `code_version`, `graphql_points`, `rest_calls`, `items_hydrated`, `items_deleted`, `failures`, `halt_reason` | |
| `repo_sync_state` | `repository_id`, `last_index_at`, `index_pr_total`, `index_issue_total`, `last_success_at`, `last_run_id` | PK `repository_id` |
| `sync_failures` | `repository_id`, `item_kind`, `number`, `attempts`, `last_error`, `first_failed_at`, `last_failed_at` | unique (`repository_id`, `item_kind`, `number`); row deleted on success |
| `user_tags`, `pr_user_tags` | unchanged | FK to new `pull_requests` |

Indexes: foreign-key columns of every child table; `pull_requests (repository_id, state)`,
`(repository_id, merged_at)`, `(author_id)`; `issues (repository_id, state)`, `(author_id)`;
`pr_closing_issues (issue_repo_github_id, issue_number)`.

Views:

- `v_pr_reviewers`: one row per (PR, reviewer). Reviewer excludes the PR author and automation.
  `final_verdict` is the state of the reviewer's latest `APPROVED` or `CHANGES_REQUESTED` review by
  review id; `COMMENTED` if none; `DISMISSED` if only dismissed reviews exist. A later `COMMENTED`
  review does not override an approval (GitHub's `latestOpinionatedReviews` behaves the same way).
- `v_pr_commenters`: commenters excluding the PR author and automation.
- `v_commit_authors`: commits with the automation flag from the linked account or a configured email.

Timestamps: all values are UTC. A `TypeDecorator` over SQLAlchemy `DateTime` rejects naive datetimes
on write and returns timezone-aware values on read. Storage keeps SQLAlchemy's fixed-width format so
text ordering equals time ordering.

### 5.2 Identity and automation

- Users are keyed by GitHub `databaseId` (REST `id`). The login is updated to the latest seen.
- GraphQL `Actor` types with a `databaseId`: `User`, `Bot`, `Organization`, `Mannequin`. A null actor
  (deleted account) stores a null reference.
- Bot logins differ between APIs (`chatgpt-codex-connector` in GraphQL, `chatgpt-codex-connector[bot]`
  in REST; `copilot-pull-request-reviewer[bot]` and `Copilot` share id 175728472). Identity never
  joins on login.
- `is_automation` is true for type `Bot` or a GitHub id in `config/tracking.toml`. Commit identities
  without a linked account are flagged by configured email.

### 5.3 Sync run

1. Resolve each configured repository with GraphQL `repository(owner, name)`; match the stored row by
   `github_id` and update the name on rename. An unresolvable repository fails for this run and
   nothing of it is deleted.
2. Index: enumerate every pull request and issue with
   `pullRequests|issues(first: 100, orderBy: {field: CREATED_AT, direction: ASC})`. Creation order is
   stable, so pagination cannot skip items that change during the scan. Up to 4 repositories in
   parallel. The enumerated count must equal the connection `totalCount`; otherwise the repository
   gets no writes or deletions this run.
3. Plan: hydrate an in-coverage item when it is not stored, its `updatedAt` differs from
   `github_updated_at`, its `hydration_version` is older than the code's, or it is unsettled
   (`hydrated_at - github_updated_at < 10 minutes`). Order the queue open items first, then by
   `updatedAt` descending, so a time-limited run refreshes the newest data first.
4. Hydrate: GraphQL batches of 25 PRs or 100 issues via aliased `pullRequest(number:)` and
   `issue(number:)` fields. Retry a failed batch 3 times with backoff, then split it in half down to
   single items. Read each alias separately: `null` with a `NOT_FOUND` error means the item is gone.
   Overflow beyond 100 reviews, comments, commits, or files pages through `node(id:)`. Above 250
   commits, take the full list from REST
   `compare/{baseRefOid}...{headRefOid}?per_page=100&page=N`. Before each request, if the last
   `rateLimit.remaining` is below a reserve, sleep until `resetAt`.
5. Validate before writing: fetched counts equal `totalCount` for reviews, comments, commits, files,
   labels, and closing issues; the compare `total_commits` equals the PR commit count. A failed item
   is retried once alone, then recorded in `sync_failures` and not written.
6. Write: one writer task with its own session. Each item is one transaction: upsert by `github_id`
   (which also moves a transferred issue), replace child rows, set `hydrated_at`. Fetchers never touch
   the database.
7. Delete: after all writes, for each repository with a complete index, recompute from the database
   the stored items whose `github_id` is absent from that repository's in-coverage set, then delete.
   Halt deletions for a repository when the count exceeds max(25, 2% of its stored rows).
8. Record: update `repo_sync_state` and close the `sync_runs` row. Exit 0 on success, 2 when any item
   failed or a repository halted, 1 on a fatal error. `--max-minutes` stops queueing new work and
   drains the writer; `--dry-run` reports the plan without writing.

### 5.4 Verification

`ghactivity verify` (online): re-index each repository; assert enumerated items equal per-state
`totalCount`; assert the stored set equals the coverage rule applied to the fresh index (report
missing, extra, and stale items); assert stored child row counts equal stored totals. Any mismatch
exits non-zero.

`ghactivity verify --db-only`: `PRAGMA integrity_check`, migration revision equals head, and per-repo
counts at least 98% of the previous manifest. Used before a scheduled sync touches a downloaded file.

GitHub search is never an oracle: it returns at most 1,000 results and omits items
(`prebid/prebid-integration-monitor#8` is absent from search but present in the repository).

### 5.5 Configuration

`config/tracking.toml`, parsed with `tomllib` and validated with pydantic at load. Missing optional
values take defaults.

```toml
[coverage]
floor = "2023-01-01"

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
emails = ["noreply@anthropic.com"]

[sync]
pr_batch_size = 25
issue_batch_size = 100
max_parallel_repos = 4
unsettled_minutes = 10
delete_guard_min_rows = 25
delete_guard_percent = 2.0
```

Proposed groups: Client (Prebid.js, prebid-universal-creative); Server (prebid-server,
prebid-server-java, prebid-cache-java, go-gdpr, openrtb); Mobile (prebid-mobile-android,
prebid-mobile-ios, prebid-mobile-ios-sdk); Docs (prebid.github.io); Agentic (salesagent,
prebid-agent-skills); Tools (professor-prebid, line-item-manager, prebid-integration-monitor,
repo-activity-dashboard, github-activity-db).

### 5.6 Scheduled sync and publication

Workflow `.github/workflows/sync.yml`:

- Triggers: `schedule` at `17 */6 * * *` (start of the hour is a documented high-load time) and
  `workflow_dispatch` with a `bootstrap` input.
- `concurrency: { group: db-sync, cancel-in-progress: false }`. A new run replaces a pending one and
  never cancels a running one.
- Token: `actions/create-github-app-token`, pinned by commit SHA, scoped to owner `prebid` with
  `permission-contents: read`, `permission-pull-requests: read`, `permission-issues: read`,
  `permission-metadata: read`. The workflow's own `GITHUB_TOKEN` (`contents: write`) handles release
  assets and the heartbeat commit.
- Download the newest asset from release `db`; run `verify --db-only`; halt on failure unless
  `bootstrap` is set. Never start from an empty database otherwise.
- `alembic upgrade head`, `ghactivity sync --max-minutes 300`, `ghactivity verify`.
- Snapshot with `VACUUM INTO` (a consistent copy in rollback-journal mode, readable from a
  read-only location; the working file is in WAL mode), gzip it, and upload
  `github_activity-<UTC timestamp>.db.gz` and its manifest (run id, code SHA, migration head,
  per-repo counts, SHA-256) as new assets; never `--clobber`. Keep the newest 28 assets (7 days).
- Heartbeat: when the last commit on `main` is older than 7 days, commit `data/manifest.json`.
- `github_activity.db` is removed from git tracking when the workflow takes over.

A `workflow_dispatch` trigger runs only when the workflow file exists on the default branch, so the
workflow merges first with the manual trigger only, is proven by manual runs, and the schedule is
enabled afterwards.

### 5.7 Code layout

| Disposition | Modules |
|-------------|---------|
| Keep | `config.py` (plus tracking config loader), `logging.py`, `db/engine.py` (plus pragmas), `cli/app.py`, `cli/common.py`, `cli/github.py`, `github/rate_limit/*`, `github/exceptions.py` |
| New | `source/` (GraphQL transport, queries, typed response models, index, hydration, compare); `sync/` (plan, writer, engine, verify, run records) |
| Replace | `db/models.py`, `db/repositories/*` |
| Remove at cutover | `github/pacing/*`, `github/sync/*`, REST PR methods in `github/client.py`, `schemas/github_api.py`, `schemas/nested.py`, `schemas/pr.py`, and their tests |

GraphQL requests go through githubkit's `GitHub.arequest("POST", "/graphql", json=...)`, which
returns `data` and `errors` together; `github.graphql.arequest` raises `GraphQLFailed` on any error
and drops the partial result. Set githubkit options explicitly (`auto_retry`, `http_cache=False`);
the defaults retry rate limits once, retry 5xx three times, and cache HTTP responses.

### 5.8 Query shapes

Index (cost 1 point per 100 items):

```graphql
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { number state updatedAt createdAt isDraft mergedAt }
    }
  }
  rateLimit { cost remaining resetAt }
}
```

Pull request fields per alias (25 aliases cost 2 points on the heaviest PRs measured):

```graphql
id number databaseId state isDraft title body createdAt updatedAt mergedAt closedAt
baseRefName baseRefOid headRefOid isCrossRepository authorAssociation
additions deletions changedFiles
author { ...Actor } mergedBy { ...Actor }
labels(first: 100) { totalCount nodes { name } }
closingIssuesReferences(first: 100) { totalCount nodes { number repository { databaseId nameWithOwner } } }
reviews(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { databaseId state author { ...Actor } } }
comments(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { author { ...Actor } } }
commits(first: 100) { totalCount pageInfo { hasNextPage endCursor }
  nodes { commit { oid authoredDate author { name email user { databaseId login } } } } }
files(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { path changeType additions deletions } }
```

`Actor` selects `__typename login` plus `databaseId` on `User`, `Bot`, `Organization`, and
`Mannequin`. Do not nest a connection inside `commits` (for example `authors(first: N)`); GraphQL
charges for every possible nested node.

## 6. Execution steps

Each step is one pull request from a `feature/<slug>` branch. A step starts after the previous one
merges.

### Step 1: Guardrails

Scope:

- `.github/workflows/check.yml` on `pull_request` and pushes to `main`: `uv sync --frozen --extra dev`,
  `pre-commit run --all-files`, pytest with JUnit output and a non-zero test count, and
  `alembic upgrade head` plus `alembic check` against a temporary database.
- Pre-commit runs ruff and mypy from `uv.lock` (local hooks via `uv run`), so local and CI use one
  version. The `type: ignore` and `noqa` audits fail when over target.
- Fix the existing lint and format findings under the locked ruff.
- Declare the `sync_failures` indexes on the model so `alembic check` passes.
- Per-connection `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout`.
- `config/tracking.toml` and its loader; `Settings.tracked_repos` reads it. Step 1 adds only
  `[[repositories]]` (with `group`); `[coverage]`, `[automation]`, and `[sync]` land in the steps
  that read them, so no section exists without a consumer.
- Documentation for the changed commands and files; this plan.

Tests: pragma values and a rejected foreign-key violation; `alembic check` on a fresh database;
config loading for valid, invalid, and default cases.

Acceptance: CI passes on the exact PR head with a non-empty job list; removing the model index makes
CI fail; removing the pragma listener makes its test fail.

### Step 2: GitHub source layer

Scope: GraphQL transport, index scanner, batch hydrator, overflow pagination, compare fallback,
budget guard, typed response models, typed invariant errors. Not wired into the CLI.

Tests run through githubkit with an injected `httpx.MockTransport`, replaying recorded responses
from public repositories: overflow on each connection, more than 250 commits, null merger and author,
dismissed review, duplicate file path, `NOT_FOUND` alias, intermittent failure then batch split,
`RATE_LIMITED` error.

Acceptance: removing any invariant check makes at least one test fail.

### Step 3: New schema, PR sync engine, cutover

Scope: migration (new tables and views; old PR tables and AI columns dropped; tag tables
re-pointed); writer; planner with coverage rule, deletions, and guards; unsettled re-hydration;
`hydration_version`; run records; `ghactivity sync` and `ghactivity verify`; removal of the old
pipeline.

Tests through the sync service with a fake transport and a real SQLite file: first sync, update,
merge, close then delete, reopen, mass-delete halt, incomplete-index halt, crash mid-run then resume
with committed items intact, unsettled re-hydration, version bump re-hydrates all.

Acceptance: `verify` exact on three live repositories; CI passes.

### Step 4: Issues and closing-issue links

Scope: migration, issue index and hydration, transfers, view joining closing references to stored
issues.

Acceptance: `verify` exact for issues on the same repositories.

### Step 5: Full bootstrap and verification

Scope: sync all tracked repositories into a new database. Projected cost about 1,900 GraphQL points.

Acceptance: `verify` exact for every repository; zero invariant failures; a 50-item random sample
matches REST; every difference from the previous file is explained in the PR.

### Step 6: Scheduled sync and publication

Prerequisite: a GitHub App with read access to Contents, Pull requests, Issues, and Metadata,
installed on the tracked repositories, with its app id and private key stored as repository secrets.

Scope: 6a merges `sync.yml` with `workflow_dispatch` only; 6b runs bootstrap and one normal manual
run; 6c enables the schedule and stops tracking `github_activity.db` in git.

Acceptance: two consecutive scheduled runs pass; a run given a corrupted asset halts without
publishing.

### Step 7: Documentation rewrite

Scope: README, `CLAUDE.md`, and `docs/` rewritten from the code; a data dictionary covering tables,
views, the coverage rule, and metric definitions; no hardcoded counts.

## 7. Evidence

Measured 2026-10-16 unless noted. "Index" means the created-order enumeration in section 5.3.

| Topic | Fact | How measured |
|-------|------|--------------|
| Index exactness | 38,195 items across 18 repositories x {PRs, issues}; every scan's item count equals `totalCount`; creation order monotonic | full index |
| Index cost | 404 GraphQL points; Prebid.js PRs alone 12,652 items, 127 points, 125 s | full index |
| `updatedAt` identity | GraphQL `updatedAt` equals REST `updated_at`: 5,599 equal, 68 newer on GitHub, 0 older (Prebid.js) | index vs stored rows |
| Change signal | Over 1,400 recently updated PRs, events landing exactly on `updatedAt`: comments 168, labels 59, unlabels 10, review requests 23, convert to draft 9, force pushes 11, title edits 13, merges 425, closes 547. Never bumping it: cross-references (472 after). Rare: assignment 2 of 268 after; one review 6 s after. Ready-for-review: 0 of 151 after | GraphQL timeline scan |
| Close and merge stamps | 7,241 closed issues with `closedAt <= updatedAt`; 25,285 merged PRs with `mergedAt <= updatedAt`; no counterexample | index |
| Commit cap | REST PR commits and GraphQL `commits` stop at 250; GraphQL returns the newest 250; paged compare returned 1,535 of 1,535 (`salesagent#1757`), 1,374 and 1,371 on open PRs; attribution identical on 250 of 250 shared commits | live API |
| Compare paging | Docs: without `per_page` or `page` the list is limited to 250 commits | GitHub REST docs |
| Hydration cost | 25 heaviest salesagent PRs: 2 points, about 9 s, 5 of 5 runs succeeded; 10 PRs: 1 point, 5 of 5; 100 issues: 1 point | repeated live batches |
| Nested connection cost | Same 25 PRs, only change is `authors(first: 5)` inside `commits(first: 100)`: 2 points without, 27 with | live single-variable comparison |
| Partial errors | A batch with a missing number returns data for the other aliases plus `errors[{type: NOT_FOUND, path}]` | live API |
| githubkit 0.14.2 | `graphql.arequest` raises `GraphQLFailed` on any error; `GitHub.arequest` returns data and errors; defaults `auto_retry` (rate limit once, 5xx three times), `http_cache=True`, `follow_redirects=True`; accepts `async_transport` | installed source; probe |
| Prototype | Design-equivalent prototype over salesagent and prebid-server: 2,625 of 2,625 PRs and 2,038 of 2,038 issues written; 0 failures; every count invariant held; 406 GraphQL points, 181 REST calls, 950 s sequential | prototype run |
| Edge cases seen | Null PR author 3, null reviewer 7, null issue author 1; overflow pages: files 139, commits 20, reviews 13, comments 1; compare fallback 18; null `mergedBy` on merged PRs (`Prebid.js#13769`, `#14683`) | prototype run; live API |
| Parity | 2,461 PRs compared with the existing file: state, merger, commit count, file count, additions equal; differences are truncated or dropped commit lists, account renames, and changes on GitHub since the last sync | prototype vs existing file vs REST |
| Size | Prototype 2,649 PRs plus 2,049 issues: 19.5 MiB raw, 6.1 MiB gzip. Existing file 52.5 MiB raw, 7.4 MiB gzip | local |
| Shared session | After a mid-run commit, 80 of 100 concurrent reads raise `InvalidRequestError` (session provisioning a new connection) | probe |
| Commit timing | Through `BulkPRIngestionService`, 0 rows committed after about 45 of 60 PRs; all 60 at the end | probe |
| `classify_tags` | Re-syncing an open PR sets it to `None` | probe |
| `sync_failures` | A second resolve of the same PR raises `IntegrityError` | probe |
| Search | `issueCount` 2,198 returns 1,000 nodes; `prebid-integration-monitor#8` absent from search | live API |
| Rate limits | Personal token 5,000 per hour; `GITHUB_TOKEN` 1,000 per hour per repository (REST and GraphQL); GitHub App installation 5,000 rising to 12,500 | GitHub docs |
| Conditional requests | A 304 response does not consume quota (3 conditional requests, used count unchanged) | live API; GitHub docs |
| Permissions | Compare: Contents read; list PRs: Pull requests read; get PR: Pull requests and Contents read; delete release asset: Contents write | GitHub docs |
| Release assets | Each file under 2 GiB; `gh release upload --clobber` deletes before upload ("If the upload fails, the original assets will be lost.") | GitHub docs; gh source |
| Large files | Git warns above 50 MiB; GitHub blocks files above 100 MiB | GitHub docs |
| Schedules | Public-repo schedules disable after 60 days without repository activity; start of hour is high load; queued jobs may be dropped; `workflow_dispatch` requires the workflow on the default branch | GitHub docs |
| Concurrency | A new run replaces a pending run in the group; running runs are cancelled only with `cancel-in-progress: true` | GitHub docs |
| App token action | `actions/create-github-app-token` v3.2.0 (2026-05-12) and v3.3.0 (2026-10-12) | GitHub API |
| SQLite pragmas | A `connect` listener on `engine.sync_engine` yields `foreign_keys=1`, `journal_mode=wal`, `busy_timeout=5000` and rejects a foreign-key violation | probe |
| Existing file | 0 foreign-key violations; integrity check `ok` | local |
| WAL snapshots | `VACUUM INTO` from a WAL database writes a rollback-journal file (header bytes 18-19 `0101` vs `0202`) that can be queried read-only from a read-only directory | sqlite3 |
| Test-suite flake | pytest-asyncio 1.3.0 on Python 3.13 leaves an unclosed event loop after an async test followed by a test calling `asyncio.run()`; with `filterwarnings = error` the next test fails. Fixed in pytest-asyncio 1.4.0 (#724) | bisect on `22551d3` and `43fcf06`; release notes |
| Quality gates | No CI; pre-commit hooks not installed; ruff 0.14.11 (lock) 10 findings plus 4 format diffs vs ruff 0.8.6 (pre-commit pin) 0 findings plus 1 format diff; mypy clean on 91 files; 608 tests, 0 failures | local |
| Private data | `prebid-agent-skills` is private; the committed file contains its PRs since commit `bf40280` | `gh repo view`; history |

## 8. Open items

| Item | Resolution point |
|------|------------------|
| Intermittent GraphQL failure rate at full scale (prototype needed 0 retries) | Step 5 measures it |
| Definition of "repository activity" for schedule disabling is not documented | Step 6 heartbeat commit |
| Whether a transferred issue keeps its `databaseId` | Step 4 upsert handles both; test both |
| Issues read permission for the App | Step 6a exercises it with a real token |
| Ready-for-review bump: 0 of 151 counterexamples (upper bound about 2%) | Unsettled rule and the next update cover misses |
| Closing links added from the issue sidebar may not bump the PR before its next update or merge | Accepted; merged PRs are hydrated at merge |
| Full bootstrap time, cost, and size are projections from two repositories | Step 5 measures them |

## 9. Later work

Dashboard integration and replacement of its data layer, a query API, AI enrichment with provenance,
company affiliation, CI check status, releases, additional repositories, and path-based exclusion of
generated or data files from line metrics (per-file stats make this possible).
