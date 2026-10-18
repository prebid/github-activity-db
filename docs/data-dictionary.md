# Data Dictionary

Every table, view, and column of the database at the migration head, each marked **consumer**
or **internal**. `tests/test_data_dictionary.py` fails when an object or column at head has no
entry here, or an entry names one that does not exist.

## Consumer interface

- The interface is the `v_*` views, every column of which is consumer, and the table columns
  marked consumer below.
- Internal columns can change in any migration.
- Row ids join rows within one file: a pull request's labels, reviews, and commits by `pr_id`,
  its author by `author_id`. The `*_id` columns of consumer tables, and the `id` columns they
  reference, are consumer; other row ids are internal. Row ids change on a rebuild, so never
  store one or compare it across files. Across files, identify a row by its `github_id`
  (`repositories.github_id`, `pull_requests.github_id`, ...), and a label, commit, file, or
  closing link by its pull request's `github_id` and its own key; (`repositories.full_name`,
  `pull_requests.number`) identifies a pull request among tracked repositories within one file,
  and changes when the repository is renamed.
- `PRAGMA user_version` holds the schema version (`SCHEMA_VERSION` in
  `src/github_activity_db/db/schema.py`). It is bumped when a consumer column is removed,
  renamed, or changes meaning; an added column or view does not bump it.
  `tests/schema_versions.json` lists each version's consumer columns, and on pull requests CI
  fails when an entry, or a column of one, that exists on the base branch is removed.
- Timestamps are UTC, stored as `YYYY-MM-DD HH:MM:SS.ffffff` text, so text order is time order.
  Booleans are stored as 0 and 1.
- An account is automation when its `type` is `Bot` or its `github_id` is in
  `automation_accounts`; a commit is automation when its linked account is, or its author email,
  with ASCII letters lowercased, is in `automation_emails`.
- A stored reference to an account is kept when GitHub later returns the actor as null (D28). A
  null reference means GitHub returned no account and the row had no stored reference: an
  account deleted before this row was written, which may still be stored for other rows; on a
  commit, also an email GitHub links to no account; and `merged_by_id` is null on an open pull
  request. Reviews and comments with a null reference are left out of the views (D24, D28).

The decisions cited (D3, D17, ...) are in [docs/redesign-plan.md](redesign-plan.md).

## Tables

### `repositories`

One row per repository the sync has resolved.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | consumer | Row id |
| `github_id` | consumer | GitHub's numeric repository id |
| `owner` | consumer | Owner login, in GitHub's spelling |
| `name` | consumer | Repository name, in GitHub's spelling |
| `full_name` | consumer | `owner/name`; changes when the repository is renamed. With a pull request's `number` it identifies the pull request among tracked repositories |
| `repo_group` | consumer | The group `tracking.toml` assigns (`Client`, `Server`, `Mobile`, `Docs`, `Agentic`, `Tools`) |
| `is_private` | consumer | Whether the repository is private |
| `is_archived` | consumer | Whether the repository is archived |
| `default_branch` | consumer | The default branch's name |
| `is_tracked` | consumer | 1 when `tracking.toml` listed the repository at the last sync that checked (redesign plan 5.3 step 1); an untracked repository's rows stay until `ghactivity sync --prune-untracked` (D27), which arrives with Step 3b's sync |

### `github_users`

Every account the database has stored, kept after GitHub deletes it (D28).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | consumer | Row id |
| `github_id` | consumer | GitHub's numeric account id |
| `node_id` | consumer | GitHub's GraphQL node id |
| `login` | consumer | Last known login, not unique; each sync refreshes accounts last refreshed more than 24 hours ago |
| `type` | consumer | `User`, `Bot`, `Organization`, or `Mannequin` |
| `refreshed_at` | internal | When the row was inserted or last refreshed |
| `deleted_at` | consumer | When a lookup found the account deleted; null otherwise. References to the account stay |

### `pull_requests`

Pull requests in coverage: open ones, and ones merged on or after the coverage floor in `tracking.toml` (redesign plan section 3). One closed without merge is not stored (D3).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | consumer | Row id |
| `repository_id` | consumer | Row id of the repository |
| `number` | consumer | Pull request number |
| `github_id` | consumer | GitHub's numeric pull request id (can exceed 2^31) |
| `node_id` | consumer | GitHub's GraphQL node id |
| `state` | consumer | `OPEN` or `MERGED` |
| `is_draft` | consumer | Whether the pull request is a draft |
| `title` | consumer | Title |
| `body` | consumer | Description, empty when there is none (D19) |
| `author_id` | consumer | Row id of the author's account; null when GitHub returned no author and the row had none stored |
| `author_association` | consumer | GitHub's current value (`MEMBER`, `CONTRIBUTOR`, ...), stored as given |
| `created_at` | consumer | When the pull request was opened |
| `merged_at` | consumer | When it was merged; set exactly when `state` is `MERGED` |
| `merged_by_id` | consumer | Row id of the merger's account; null when GitHub returned no merger and the row had none stored |
| `base_ref` | consumer | Name of the base branch |
| `targets_default_branch` | consumer | Whether `base_ref` is the repository's current default branch |
| `is_cross_repository` | consumer | Whether the head branch is in another repository |
| `additions` | consumer | Lines added, as GitHub reports |
| `deletions` | consumer | Lines deleted, as GitHub reports |
| `changed_files` | consumer | GitHub's changed-file count; a path listed twice counts once, so it can differ from `files_total` |
| `commits_total` | consumer | Number of commits |
| `files_total` | consumer | Number of entries in the file list (`pr_files` rows) |
| `reviews_total` | consumer | Number of submitted reviews (`pr_reviews` rows) |
| `comments_total` | consumer | Number of conversation comments (`pr_comments` rows) |
| `labels_total` | consumer | Number of labels applied (`pr_labels` rows) |
| `closing_issues_total` | consumer | Number of issues the pull request closes (`pr_closing_issues` rows) |
| `github_updated_at` | consumer | GitHub's `updatedAt` as of the hydration that wrote the row; updates from the index alone leave it |
| `hydrated_at` | internal | GitHub's response time for the request that read the pull request |
| `hydration_version` | internal | Version of the queries that read the pull request; a change re-reads every pull request |

### `pr_labels`

Labels as currently applied, without history (D8).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `name` | consumer | Label name |

### `pr_reviews`

Submitted reviews; pending reviews are not stored (D4). Review order is `github_id` order (D6).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `github_id` | consumer | GitHub's numeric review id |
| `pr_id` | consumer | Row id of the pull request |
| `reviewer_id` | consumer | Row id of the reviewer's account; null when GitHub returned no reviewer and the row had none stored |
| `state` | consumer | `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, or `DISMISSED` |

### `pr_comments`

Conversation comments, one row each, without their text (D5, D24).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `github_id` | consumer | GitHub's numeric comment id |
| `pr_id` | consumer | Row id of the pull request |
| `author_id` | consumer | Row id of the author's account; null when GitHub returned no author and the row had none stored |

### `pr_commits`

The pull request's commits; co-authors are not stored (D15).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `sha` | consumer | Commit SHA |
| `author_id` | consumer | Row id of the account GitHub links the commit to; null when GitHub links none and the row had none stored |
| `author_name` | consumer | Git author name |
| `author_email` | consumer | Git author email |
| `authored_at` | consumer | Git author date, set by the committer's client (D6) |

### `pr_files`

The file list, one row per entry; a path can appear twice.

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `ordinal` | consumer | Position in GitHub's file list |
| `path` | consumer | File path |
| `change_type` | consumer | GitHub's change type (`ADDED`, `MODIFIED`, ...), stored as given |
| `additions` | consumer | Lines added to the file |
| `deletions` | consumer | Lines deleted from the file |

### `pr_closing_issues`

Issues the pull request closes, possibly in repositories the database does not track (D23).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `issue_repo_github_id` | consumer | GitHub's numeric id of the issue's repository |
| `issue_repo_full_name` | consumer | `owner/name` of the issue's repository |
| `issue_number` | consumer | Issue number |

### `automation_accounts`

The `[automation].accounts` list of `tracking.toml`, copied at the start of every sync.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `github_id` | consumer | GitHub's numeric account id |
| `login` | consumer | Login as `tracking.toml` gives it |
| `reason` | consumer | Why the account counts as automation |

### `automation_emails`

The `[automation].emails` list of `tracking.toml`, copied at the start of every sync.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `email` | consumer | Commit author email, lowercase ASCII |
| `reason` | consumer | Why commits from the address count as automation |

### `sync_runs`

One row per sync run.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `started_at` | internal | When the run started |
| `deadline_at` | internal | `started_at` plus `--max-minutes`; null without it |
| `finished_at` | internal | When the run closed; null while it runs |
| `status` | internal | `running`, `complete`, `time_limited`, `failed`, or `aborted` |
| `exit_code` | internal | The run's exit code; null while it runs |
| `trigger` | internal | `schedule`, `workflow_dispatch`, or `manual` |
| `code_version` | internal | Commit or package version of the code that ran |
| `graphql_points` | internal | GraphQL rate-limit points spent |
| `rest_calls` | internal | REST requests sent |
| `items_planned` | internal | Distinct items planned for hydration |
| `items_hydrated` | internal | Hydrated items written |
| `items_deleted` | internal | Item rows deleted |
| `failures` | internal | Item failures in the run, accepted ones included |
| `deletions_approved` | internal | JSON text describing an approved deletion; null when none was approved |
| `halt_reason` | internal | Why the run halted; null while it runs or when it did not halt |

### `repo_sync_state`

Per-repository sync state, written as each run closes.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_id` | internal | Row id of the repository |
| `last_index_started_at` | internal | Start of the last complete index the run did not later fail |
| `index_pr_total` | internal | Pull requests that index counted |
| `index_issue_total` | internal | Issues that index counted; null until issues are indexed |
| `last_success_at` | internal | When the last fully successful run finished |
| `last_run_id` | internal | Row id of the latest run that attempted the repository |
| `last_outcome` | internal | That run's result for the repository |
| `halt_reason` | internal | Why deletions halted; null otherwise |

### `sync_pending`

Items a time-limited run planned but did not reach.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_id` | internal | Row id of the repository |
| `item_kind` | internal | `pull_request` or `issue` |
| `number` | internal | Item number |
| `run_id` | internal | Row id of the run that left the item |

### `sync_failures`

Items whose last hydration failed; a row is deleted when the item succeeds.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `repository_id` | internal | Row id of the repository |
| `item_kind` | internal | `pull_request` or `issue` |
| `number` | internal | Item number |
| `attempts` | internal | Runs in which the item failed |
| `last_error` | internal | The last error's class and message |
| `first_failed_at` | internal | When the item first failed |
| `last_failed_at` | internal | When the item last failed |

### `user_tags`

Kept from the first schema; nothing writes tags yet (D17).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `name` | internal | Tag name |
| `description` | internal | Tag description |
| `color` | internal | Tag color |
| `created_at` | internal | When the tag was created |

### `pr_user_tags`

Tags applied to pull requests; empty until a tag producer exists (D17).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | internal | Row id of the pull request |
| `user_tag_id` | internal | Row id of the tag |
| `created_at` | internal | When the tag was applied |

### `alembic_version`

The migration revision the file is at.

| Column | Use | Meaning |
|--------|-----|---------|
| `version_num` | internal | Alembic revision id |

## Views

### `v_github_users`

Every stored account, with its automation flag.

| Column | Use | Meaning |
|--------|-----|---------|
| `github_id` | consumer | GitHub's numeric account id |
| `node_id` | consumer | GitHub's GraphQL node id |
| `login` | consumer | Last known login |
| `type` | consumer | `User`, `Bot`, `Organization`, or `Mannequin` |
| `deleted_at` | consumer | When a lookup found the account deleted; null otherwise |
| `is_automation` | consumer | 1 when the account is automation |

### `v_pr_reviewers`

One row per pull request and reviewer, for stored reviewers who are neither the pull request's
author nor automation (D4).

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_full_name` | consumer | `owner/name` of the repository |
| `pr_number` | consumer | Pull request number |
| `pr_github_id` | consumer | GitHub's numeric pull request id |
| `reviewer_github_id` | consumer | Reviewer's numeric account id |
| `reviewer_login` | consumer | Reviewer's last known login |
| `final_verdict` | consumer | State of the reviewer's latest `APPROVED`, `CHANGES_REQUESTED`, or `DISMISSED` review by review id, else `COMMENTED`; `DISMISSED` means the last verdict was withdrawn |
| `comment_count` | consumer | The reviewer's conversation comments on the pull request (D5) |

### `v_pr_commenters`

One row per pull request and conversation commenter who submitted no review on it, excluding
the pull request's author and automation (D5).

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_full_name` | consumer | `owner/name` of the repository |
| `pr_number` | consumer | Pull request number |
| `pr_github_id` | consumer | GitHub's numeric pull request id |
| `commenter_github_id` | consumer | Commenter's numeric account id |
| `commenter_login` | consumer | Commenter's last known login |
| `comment_count` | consumer | The commenter's conversation comments on the pull request |

### `v_commit_authors`

Every stored commit, with its linked account and automation flag.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_full_name` | consumer | `owner/name` of the repository |
| `pr_number` | consumer | Pull request number |
| `pr_github_id` | consumer | GitHub's numeric pull request id |
| `sha` | consumer | Commit SHA |
| `author_github_id` | consumer | Numeric id of the account GitHub links the commit to; null when there is none |
| `author_name` | consumer | Git author name |
| `author_email` | consumer | Git author email |
| `authored_at` | consumer | Git author date (D6) |
| `is_automation` | consumer | 1 when the commit is automation |
