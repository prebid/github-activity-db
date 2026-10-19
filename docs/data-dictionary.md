# Data Dictionary

Every table, view, and column of the database at the migration head, each marked **consumer**
or **internal**. `tests/test_data_dictionary.py` fails when an object or column at head has no
entry here, or an entry names one that does not exist.

## Consumer interface

- The interface is the `v_*` views, every column of which is consumer, and the table columns
  marked consumer below. Every column holding what GitHub reports is consumer.
- Internal columns can change in any migration.
- Row ids join rows within one file: a pull request's labels, reviews, and commits by `pr_id`,
  its author by `author_id`. The `*_id` columns of consumer tables, and the `id` columns they
  reference, are consumer; other row ids are internal. Row ids change on a rebuild, so never
  store one or compare it across files. Across files, identify a row by its `github_id`
  (`repositories.github_id`, `pull_requests.github_id`, ...), an account with a null
  `github_id` by its `node_id`, and a label, commit, file, closing link, review request, or
  assignee by its pull request's `github_id` and its own key; (`repositories.full_name`,
  `pull_requests.number`) identifies a pull request among tracked repositories within one file,
  and changes when the repository is renamed.
- `PRAGMA user_version` holds the schema version (`SCHEMA_VERSION` in
  `src/github_activity_db/db/schema.py`). It is bumped when a consumer column is removed,
  renamed, or changes meaning, or a view's frozen SQL changes (its rows, its derivation, or its
  layout alone); an added table column, table, or view does not bump it.
  `tests/schema_versions.json` lists each version's consumer columns and, from version 2, the
  SHA-256 of each view's SQL exactly as its migration embeds it (SQLite stores that text without
  its leading and trailing line breaks); on pull requests CI fails when an
  entry, a column of one, or a view digest of one that exists on the base branch is removed or
  changed.
- Timestamps are UTC, stored as `YYYY-MM-DD HH:MM:SS.ffffff` text, so text order is time order.
  Booleans are stored as 0 and 1.
- Values GitHub reports keep GitHub's names in snake case and its values. Every GitHub enum
  except a pull request's and a review's `state` (and `github_users.type`) is GitHub's text,
  stored without a check, such as `review_decision` or `mergeable`, so a value GitHub adds is
  stored. No column of GitHub's data holds a value the database chose, except the
  classifications named below (`targets_default_branch`, `is_pr_author`, `author_also_reviewed`,
  `automation_reason`, `email_automation_reason`), each a rule over stored columns of the same
  file (D29). An `ordinal` column is the entry's position in GitHub's list, numbered from 0.
- The references D28 keeps (a pull request's author and merger, a review's reviewer, a
  comment's author, a commit's account) are null when GitHub returned no actor and the row had
  none stored; GitHub does not say why. A reference the database kept when GitHub later
  returned no actor has its `*_reported` column 0; on a commit, `linked_github_id` holds the
  account id GitHub linked in the latest hydration (D28). Editors and requested accounts take
  GitHub's current value, null included, and `pr_assignees` holds GitHub's current
  `assignedActors` list. Each column's entry gives its further null cases. An account a lookup answered `NOT_FOUND` for keeps its
  row, its `not_found_at` set.
- An account's `type` and the reason of the `[automation]` entry listing it are separate
  columns; nothing merges them into one flag, and the views keep every row (D29).
- The run records (`sync_runs`, `repo_sync_state`, `sync_pending`, `sync_failures`), the
  automation lists, `is_tracked`, `repo_group`, and `pull_requests.hydration_version` describe
  the database's own configuration and runs, not GitHub's data. A view's `relationship` column names the stored row a view row comes
  from.

The decisions cited (D3, D17, ...) are in [docs/redesign-plan.md](redesign-plan.md).

## Tables

### `repositories`

One row per repository the sync has resolved, with every field the resolve reads (redesign plan
5.8), as the latest complete resolve read them.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | consumer | Row id |
| `github_id` | consumer | GitHub's numeric repository id |
| `node_id` | consumer | GitHub's GraphQL node id |
| `owner` | consumer | Owner login, in GitHub's spelling |
| `name` | consumer | Repository name, in GitHub's spelling |
| `full_name` | consumer | `owner/name`; changes when the repository is renamed. With a pull request's `number` it identifies the pull request among tracked repositories |
| `repo_group` | consumer | The group `tracking.toml` assigned at the last resolve, a name the maintainer gives (`Client`, `Server`, `Mobile`, `Docs`, `Agentic`, `Tools`) |
| `is_private` | consumer | GitHub's `isPrivate` |
| `is_archived` | consumer | GitHub's `isArchived` |
| `default_branch` | consumer | The default branch's name |
| `labels_total` | consumer | GitHub's `totalCount` of the repository's labels (`repo_labels` rows) |
| `is_tracked` | consumer | 1 when `tracking.toml` listed the repository at the last sync that checked (redesign plan 5.3 step 1); an untracked repository's rows stay until `ghactivity sync --prune-untracked` (D27) |
| `resolved_at` | consumer | GitHub's `Date` header of the first response of the resolve that wrote the row: the as-of time of its GitHub fields and labels |
| `is_security_policy_enabled` | consumer | GitHub's `isSecurityPolicyEnabled`; null when GitHub returns none |
| `visibility` | consumer | GitHub's `visibility` (`PUBLIC`, `PRIVATE`, ...) |
| `description` | consumer | GitHub's `description`; null when there is none |
| `homepage_url` | consumer | GitHub's `homepageUrl`, a link the owner sets; null when there is none |
| `created_at` | consumer | GitHub's `createdAt` |
| `updated_at` | consumer | GitHub's `updatedAt` |
| `pushed_at` | consumer | GitHub's `pushedAt`; null when GitHub returns none |
| `archived_at` | consumer | GitHub's `archivedAt`; null when the repository is not archived |
| `stargazer_count` | consumer | GitHub's `stargazerCount` |
| `fork_count` | consumer | GitHub's `forkCount` |
| `disk_usage` | consumer | GitHub's `diskUsage`, in kilobytes; null when GitHub returns none |
| `forking_allowed` | consumer | GitHub's `forkingAllowed` |
| `allow_update_branch` | consumer | GitHub's `allowUpdateBranch` |
| `web_commit_signoff_required` | consumer | GitHub's `webCommitSignoffRequired` |
| `has_wiki_enabled` | consumer | GitHub's `hasWikiEnabled` |
| `has_discussions_enabled` | consumer | GitHub's `hasDiscussionsEnabled` |
| `has_projects_enabled` | consumer | GitHub's `hasProjectsEnabled` |
| `has_vulnerability_alerts_enabled` | consumer | GitHub's `hasVulnerabilityAlertsEnabled` |
| `uses_custom_open_graph_image` | consumer | GitHub's `usesCustomOpenGraphImage` |
| `merge_commit_allowed` | consumer | GitHub's `mergeCommitAllowed` |
| `squash_merge_allowed` | consumer | GitHub's `squashMergeAllowed` |
| `rebase_merge_allowed` | consumer | GitHub's `rebaseMergeAllowed` |
| `delete_branch_on_merge` | consumer | GitHub's `deleteBranchOnMerge` |
| `merge_commit_title` | consumer | GitHub's `mergeCommitTitle` setting |
| `merge_commit_message` | consumer | GitHub's `mergeCommitMessage` setting |
| `squash_merge_commit_title` | consumer | GitHub's `squashMergeCommitTitle` setting |
| `squash_merge_commit_message` | consumer | GitHub's `squashMergeCommitMessage` setting |
| `primary_language` | consumer | The name of GitHub's `primaryLanguage`; null when there is none |
| `license_spdx_id` | consumer | The `spdxId` of GitHub's `licenseInfo`; null when GitHub returns no license or no `spdxId` |
| `plan_features_codeowners` | consumer | GitHub's `planFeatures.codeowners` |
| `plan_features_team_review_requests` | consumer | GitHub's `planFeatures.teamReviewRequests` |
| `plan_features_maximum_manual_review_requests` | consumer | GitHub's `planFeatures.maximumManualReviewRequests` |

### `repo_labels`

Each repository's label set as the latest complete resolve read it (D8).

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_id` | consumer | Row id of the repository |
| `node_id` | consumer | GitHub's label id |
| `name` | consumer | Label name |
| `color` | consumer | GitHub's `color` |
| `description` | consumer | GitHub's `description`; null when there is none |
| `is_default` | consumer | GitHub's `isDefault` |
| `created_at` | consumer | GitHub's `createdAt`; null when GitHub returns none |
| `updated_at` | consumer | GitHub's `updatedAt`; null when GitHub returns none |

### `github_users`

Every account the database has stored, kept after a lookup answers `NOT_FOUND` for it (D28).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | consumer | Row id |
| `github_id` | consumer | GitHub's numeric account id; null only for an `EnterpriseUserAccount`, which has none |
| `node_id` | consumer | GitHub's GraphQL node id; the key of an account with a null `github_id` |
| `login` | consumer | Last known login, not unique; an actor sighting sets it when it inserts the account and only lookups change it; each sync refreshes accounts last refreshed more than 24 hours ago, except those with `not_found_at` set |
| `type` | consumer | GitHub's type: `User`, `Bot`, `Organization`, `Mannequin`, or `EnterpriseUserAccount` |
| `name` | consumer | GitHub's `name`; null until the account's first lookup, for a `Bot`, and when GitHub returns none |
| `created_at` | consumer | GitHub's `createdAt`; null until the account's first lookup |
| `updated_at` | consumer | GitHub's `updatedAt`; null until the account's first lookup |
| `linked_user_github_id` | consumer | An `EnterpriseUserAccount`'s `user.databaseId`; null for another type or when GitHub returns none |
| `claimant_github_id` | consumer | A `Mannequin`'s `claimant.databaseId`; null until the account's first lookup, for another type, and when GitHub returns none |
| `refreshed_at` | consumer | GitHub's `Date` header of the response that inserted the row or of its latest lookup that returned the account; a `NOT_FOUND` lookup leaves it |
| `not_found_at` | consumer | GitHub's `Date` header of the first lookup that answered `NOT_FOUND` since the account was last seen; null otherwise. No reference reads it |

### `account_profiles`

A `User`'s or an `Organization`'s public profile as the latest lookup returned it (D31); a
`Bot`, `Mannequin`, or `EnterpriseUserAccount` has no row. A lookup that answers `NOT_FOUND`
leaves the row as it was.

| Column | Use | Meaning |
|--------|-----|---------|
| `account_id` | consumer | Row id of the account |
| `company` | consumer | A user's public profile `company`; null for an organization and when there is none |
| `location` | consumer | The profile's `location`; null when there is none |
| `website_url` | consumer | The profile's `websiteUrl`; null when there is none |
| `description` | consumer | An organization's `description`; null for a user and when there is none |
| `email` | consumer | An organization's public `email`; null for a user and when there is none. A user's profile email is not collected (D31) |
| `twitter_username` | consumer | An organization's `twitterUsername`; null for a user and when there is none |
| `is_verified` | consumer | An organization's `isVerified`; null for a user |
| `archived_at` | consumer | An organization's `archivedAt`; null for a user and when it is not archived |
| `has_sponsors_listing` | consumer | An organization's `hasSponsorsListing`; null for a user |
| `pinned_items_remaining` | consumer | An organization's `pinnedItemsRemaining`; null for a user |
| `refreshed_at` | consumer | GitHub's `Date` header of the lookup that wrote the row |

### `pull_requests`

Pull requests in coverage: open ones, ones merged on or after the coverage floor in
`tracking.toml`, and ones closed without merge on or after it (redesign plan section 3, D3).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | consumer | Row id |
| `repository_id` | consumer | Row id of the repository |
| `number` | consumer | Pull request number |
| `github_id` | consumer | GitHub's numeric pull request id (can exceed 2^31) |
| `node_id` | consumer | GitHub's GraphQL node id |
| `state` | consumer | GitHub's `state`: `OPEN`, `CLOSED`, or `MERGED` |
| `is_draft` | consumer | GitHub's `isDraft` |
| `title` | consumer | Title |
| `body` | consumer | Description, empty when there is none (D19) |
| `author_id` | consumer | Row id of the author's account; null when GitHub returned no author and the row had none stored |
| `author_reported` | consumer | 1 when GitHub returned an author in the row's latest hydration; 0 when it returned none, `author_id` then being a reference kept from earlier or null (D28) |
| `author_association` | consumer | GitHub's current `authorAssociation` (`MEMBER`, `CONTRIBUTOR`, ...), which GitHub computes when read |
| `created_at` | consumer | GitHub's `createdAt` |
| `merged_at` | consumer | GitHub's `mergedAt`; set exactly when `state` is `MERGED` |
| `closed_at` | consumer | GitHub's `closedAt`; set for every `CLOSED` or `MERGED` row (plan 5.3 step 5) |
| `published_at` | consumer | GitHub's `publishedAt`; null when GitHub returns none |
| `last_edited_at` | consumer | GitHub's `lastEditedAt`; null when GitHub returns none |
| `editor_id` | consumer | Row id of the account GitHub lists as the last editor; null when GitHub returns none |
| `includes_created_edit` | consumer | GitHub's `includesCreatedEdit` |
| `maintainer_can_modify` | consumer | GitHub's `maintainerCanModify` |
| `merged_by_id` | consumer | Row id of the merger's account; null on an unmerged pull request, and when GitHub returned no merger and the row had none stored |
| `merged_by_reported` | consumer | 1 when GitHub returned a merger in the row's latest hydration (D28) |
| `base_ref` | consumer | GitHub's `baseRefName` |
| `base_ref_oid` | consumer | GitHub's `baseRefOid` |
| `head_ref_name` | consumer | GitHub's `headRefName` |
| `head_ref_oid` | consumer | GitHub's `headRefOid` |
| `head_repo_github_id` | consumer | Numeric id of GitHub's `headRepository`; null when GitHub returns no head repository or no id for it |
| `head_repo_full_name` | consumer | `nameWithOwner` of GitHub's `headRepository`; null when GitHub returns none |
| `head_repo_owner_node_id` | consumer | Node id of GitHub's `headRepositoryOwner`; null when GitHub returns none |
| `head_repo_owner_login` | consumer | Login of GitHub's `headRepositoryOwner`; null when GitHub returns none |
| `merge_commit_oid` | consumer | Oid of GitHub's `mergeCommit`; null when GitHub returns none |
| `targets_default_branch` | consumer | Classification: whether `base_ref` equals the repository's current `default_branch`, recomputed when the default branch changes |
| `is_cross_repository` | consumer | GitHub's `isCrossRepository` |
| `milestone_number` | consumer | Number of GitHub's `milestone` reference; null when the pull request is in none |
| `milestone_title` | consumer | Title of GitHub's `milestone` reference; null when the pull request is in none |
| `review_decision` | consumer | GitHub's `reviewDecision` when the row was hydrated; null when GitHub returns none |
| `mergeable` | consumer | GitHub's `mergeable` when the row was hydrated; GitHub recomputes it, so it is as of `hydrated_at` |
| `merge_state_status` | consumer | GitHub's `mergeStateStatus` when the row was hydrated, as of `hydrated_at` |
| `can_be_rebased` | consumer | GitHub's `canBeRebased` when the row was hydrated, as of `hydrated_at` |
| `potential_merge_commit_oid` | consumer | Oid of GitHub's `potentialMergeCommit` when the row was hydrated; GitHub returns none "if the pull request is merged, or if the test merge commit is still being generated" |
| `stack_node_id` | consumer | Node id of GitHub's `stack`; null when the pull request is in none |
| `stack_number` | consumer | GitHub's stack `number`, "A number uniquely identifying the stack within its repository"; null when the pull request is in none |
| `stack_size` | consumer | GitHub's stack `size`; null when the pull request is in none |
| `stack_base_ref_name` | consumer | GitHub's stack `baseRefName`; null when the pull request is in none |
| `stack_entry_node_id` | consumer | Node id of GitHub's `stackEntry`; null when GitHub returns none |
| `stack_position` | consumer | GitHub's stack entry `position`, "where 1 is the closest to the base branch"; null when GitHub returns no entry |
| `additions` | consumer | GitHub's `additions` |
| `deletions` | consumer | GitHub's `deletions` |
| `changed_files` | consumer | GitHub's `changedFiles`; a path listed twice counts once, so it can differ from `files_total` |
| `total_comments_count` | consumer | GitHub's `totalCommentsCount`; null when GitHub returns none |
| `commits_total` | consumer | GitHub's `totalCount` of the commits (`pr_commits` rows) |
| `files_total` | consumer | GitHub's `totalCount` of the file list (`pr_files` rows) |
| `reviews_total` | consumer | GitHub's `totalCount` of the submitted reviews (`pr_reviews` rows) |
| `comments_total` | consumer | GitHub's `totalCount` of the conversation comments (`pr_comments` rows) |
| `labels_total` | consumer | GitHub's `totalCount` of the labels applied (`pr_labels` rows) |
| `closing_issues_total` | consumer | GitHub's `totalCount` of `closingIssuesReferences` (`pr_closing_issues` rows) |
| `user_linked_closing_issues_total` | consumer | GitHub's `totalCount` of `closingIssuesReferences` with `userLinkedOnly: true` (rows with `user_linked`) |
| `review_requests_total` | consumer | GitHub's `totalCount` of `reviewRequests` (`pr_review_requests` rows) |
| `assignees_total` | consumer | GitHub's `totalCount` of `assignedActors` (`pr_assignees` rows) |
| `github_updated_at` | consumer | GitHub's `updatedAt` as of the hydration that wrote the row; updates from the index alone leave it |
| `hydrated_at` | consumer | GitHub's `Date` header of the response that read the pull request: the as-of time of its hydrated fields |
| `hydration_version` | consumer | Version of the queries that read the pull request; a column added later is null on rows read at an older version until they are read again |

### `pr_labels`

Labels as currently applied, without history (D8).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `name` | consumer | Label name |
| `label_node_id` | consumer | GitHub's label id |

### `pr_reviews`

Submitted reviews; pending reviews are not stored (D4). Review ids do not follow submission
order (D6), so `submitted_at` gives that order. No review text (D24).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `github_id` | consumer | GitHub's numeric review id |
| `node_id` | consumer | GitHub's GraphQL node id |
| `pr_id` | consumer | Row id of the pull request |
| `reviewer_id` | consumer | Row id of the reviewer's account; null when GitHub returned no author and the row had none stored |
| `reviewer_reported` | consumer | 1 when GitHub returned an author in the review's latest hydration (D28) |
| `state` | consumer | GitHub's `state`: `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, or `DISMISSED` |
| `created_at` | consumer | GitHub's `createdAt` |
| `submitted_at` | consumer | GitHub's `submittedAt`; null when GitHub returns none |
| `published_at` | consumer | GitHub's `publishedAt`; null when GitHub returns none |
| `updated_at` | consumer | GitHub's `updatedAt` |
| `last_edited_at` | consumer | GitHub's `lastEditedAt`; null when GitHub returns none |
| `editor_id` | consumer | Row id of the account GitHub lists as the last editor; null when GitHub returns none |
| `includes_created_edit` | consumer | GitHub's `includesCreatedEdit` |
| `commit_oid` | consumer | Oid of the review's `commit`; null when GitHub returns none |
| `author_association` | consumer | GitHub's `authorAssociation` when the pull request was hydrated |
| `author_can_push_to_repository` | consumer | GitHub's `authorCanPushToRepository` when the pull request was hydrated |
| `inline_comments_total` | consumer | GitHub's `comments.totalCount` for the review: its inline comments (D5) |

### `pr_comments`

Conversation comments, one row each, without their text (D5, D24).

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `github_id` | consumer | GitHub's numeric comment id |
| `node_id` | consumer | GitHub's GraphQL node id |
| `pr_id` | consumer | Row id of the pull request |
| `author_id` | consumer | Row id of the author's account; null when GitHub returned no author and the row had none stored |
| `author_reported` | consumer | 1 when GitHub returned an author in the comment's latest hydration (D28) |
| `created_at` | consumer | GitHub's `createdAt` |
| `published_at` | consumer | GitHub's `publishedAt`; null when GitHub returns none |
| `updated_at` | consumer | GitHub's `updatedAt` |
| `last_edited_at` | consumer | GitHub's `lastEditedAt`; null when GitHub returns none |
| `editor_id` | consumer | Row id of the account GitHub lists as the last editor; null when GitHub returns none |
| `created_via_email` | consumer | GitHub's `createdViaEmail` |
| `includes_created_edit` | consumer | GitHub's `includesCreatedEdit` |
| `author_association` | consumer | GitHub's `authorAssociation` when the pull request was hydrated |

### `pr_commits`

The pull request's commits, each like a comment: its author link and git author, never its
message (D19); co-authors are not stored (D15).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `sha` | consumer | Commit SHA |
| `author_id` | consumer | Row id of the account GitHub links the commit to; null when GitHub links none and the row had none stored, and when the linked account was never stored because a lookup answered `NOT_FOUND` for it (`linked_github_id` then holds its id) |
| `linked_github_id` | consumer | The account id GitHub linked the commit to in the latest hydration, whether or not a lookup found the account; null when it linked none (D28) |
| `author_name` | consumer | Git author name |
| `author_email` | consumer | Git author email |
| `authored_at` | consumer | Git author date, set by the committer's client (D6) |

### `pr_files`

The file list, one row per entry; a path can appear twice.

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `ordinal` | consumer | Position in GitHub's file list, from 0 |
| `path` | consumer | File path |
| `change_type` | consumer | GitHub's `changeType` (`ADDED`, `MODIFIED`, ...) |
| `additions` | consumer | Lines added to the file |
| `deletions` | consumer | Lines deleted from the file |

### `pr_closing_issues`

The pull request's `closingIssuesReferences`, which GitHub describes as "List of issues that may
be closed by this pull request", possibly in repositories the database does not track (D23).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `issue_repo_github_id` | consumer | GitHub's numeric id of the issue's repository |
| `issue_repo_full_name` | consumer | `owner/name` of the issue's repository when the link was last written |
| `issue_number` | consumer | Issue number |
| `issue_github_id` | consumer | The issue's `fullDatabaseId` |
| `user_linked` | consumer | 1 when GitHub also lists the link under `userLinkedOnly: true`, which GitHub describes as "Return only manually linked Issues" |

### `pr_review_requests`

GitHub's `reviewRequests` for the pull request, one row per entry as GitHub currently lists it
(D4).

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `ordinal` | consumer | Position in GitHub's list, from 0 |
| `request_github_id` | consumer | The request's own `databaseId`; null when GitHub returns none |
| `request_node_id` | consumer | The request's own node id |
| `requested_type` | consumer | GitHub's type of the requested reviewer (`User`, `Bot`, `Mannequin`, `Team`, `EnterpriseTeam`); null when GitHub returns no requested reviewer |
| `requested_github_id` | consumer | The requested account's or `Team`'s `databaseId`, or an `EnterpriseTeam`'s `fullDatabaseId`; null when GitHub returns none |
| `requested_node_id` | consumer | The requested account's, team's, or enterprise team's node id; null when GitHub returns no requested reviewer |
| `account_id` | consumer | Row id of the requested account; null for a `Team` or an `EnterpriseTeam`, which is not an account, and when GitHub returns no requested reviewer |
| `team_slug` | consumer | The requested team's or enterprise team's `slug`; null for an account and when GitHub returns no requested reviewer |
| `as_code_owner` | consumer | GitHub's `asCodeOwner` |

### `pr_assignees`

The actors GitHub lists in the pull request's `assignedActors` (D14): users, bots, mannequins,
and organizations.

| Column | Use | Meaning |
|--------|-----|---------|
| `pr_id` | consumer | Row id of the pull request |
| `account_id` | consumer | Row id of the assigned account |

### `automation_accounts`

The `[automation].accounts` list of `tracking.toml`, copied at the start of every sync.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `github_id` | consumer | GitHub's numeric account id |
| `login` | consumer | Login as `tracking.toml` gives it |
| `reason` | consumer | The reason `tracking.toml` gives for the entry |
| `run_id` | consumer | Row id of the run that copied the list |

### `automation_emails`

The `[automation].emails` list of `tracking.toml`, copied at the start of every sync.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `email` | consumer | Commit author email, lowercase ASCII |
| `reason` | consumer | The reason `tracking.toml` gives for the entry |
| `run_id` | consumer | Row id of the run that copied the list |

### `sync_runs`

One row per sync run.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | consumer | Row id |
| `started_at` | consumer | When the run started |
| `deadline_at` | consumer | `started_at` plus `--max-minutes`; null without it |
| `finished_at` | consumer | When the run closed; null while it runs, and on a row another process marked `aborted` |
| `status` | consumer | `running`, `complete`, `time_limited`, `failed`, or `aborted` |
| `exit_code` | consumer | The run's exit code; null while it runs, and on a row another process marked `aborted` |
| `trigger` | consumer | `schedule` or `workflow_dispatch` from the Actions event, else `manual` |
| `code_version` | consumer | `GITHUB_SHA`; else the commit of the git checkout the package ran from, with `-dirty` when its tracked files had changes; else the package version |
| `config_source` | consumer | `packaged`, or the `TRACKING_CONFIG_PATH` path the run read |
| `config_sha256` | consumer | SHA-256 of the `tracking.toml` bytes the run read |
| `graphql_points` | internal | GraphQL rate-limit points spent |
| `rest_calls` | internal | REST requests sent |
| `items_planned` | internal | Distinct items planned for hydration |
| `items_hydrated` | internal | Hydrated items written |
| `items_deleted` | internal | Item rows deleted |
| `failures` | internal | Item failures in the run, accepted ones included |
| `deletions_approved` | consumer | JSON text, keys sorted: `allow_deletions` (`repository` as configured, its `github_id`, `approved` N, and `sets` with `items`' `candidates` and whether they were `deleted`) and `prune_untracked` (each pruned repository's `github_id`, `full_name`, and `items_deleted`), each written with the deletion it records; null when neither applied |

### `repo_sync_state`

Per-repository sync state, written as each run closes.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_id` | consumer | Row id of the repository |
| `last_index_started_at` | consumer | Start of the last complete index the run did not later fail: the as-of time of everything the index wrote |
| `index_pr_total` | consumer | Pull requests that index counted |
| `index_issue_total` | consumer | Issues that index counted; null until issues are indexed |
| `last_success_at` | consumer | `finished_at` of the latest run in which the repository's outcome was `complete` and none of its items failed outside `[sync].accepted_failures` |
| `last_run_id` | consumer | Row id of the latest run that attempted the repository |
| `last_outcome` | consumer | That run's result for the repository |
| `halt_reason` | consumer | Why deletions halted in the latest run that halted them, with the candidate count; cleared by a run whose step 7 ran for the repository without halting, and left by one whose step 7 did not run for it |

### `sync_pending`

Items a time-limited run planned but did not reach: an in-coverage item not yet hydrated.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_id` | consumer | Row id of the repository |
| `item_kind` | consumer | `pull_request` or `issue` |
| `number` | consumer | Item number |
| `run_id` | consumer | Row id of the run that left the item |

### `sync_failures`

Items whose latest attempt failed, in a hydration or in the planner's check of an index entry
(redesign plan 5.3 step 3). The next run plans the item again; its row is deleted when the item is
written, or when the item leaves coverage.

| Column | Use | Meaning |
|--------|-----|---------|
| `id` | internal | Row id |
| `repository_id` | consumer | Row id of the repository |
| `item_kind` | consumer | `pull_request` or `issue` |
| `number` | consumer | Item number |
| `attempts` | internal | Runs in which the item failed |
| `last_error` | consumer | The last error's class and message |
| `first_failed_at` | consumer | When the item first failed |
| `last_failed_at` | consumer | When the item last failed |

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

Every view row comes from one stored row, and every row is kept: a null reference gives null
account columns. A pull request's keys are its repository's `github_id`, `full_name`, and
`is_tracked`, and its `number` and `github_id`. An account carries its `github_id`, `node_id`,
`login`, `type`, and `automation_reason`, the reason of the `automation_accounts` entry for its
`github_id` (null when none lists it).

### `v_github_users`

One row per stored account.

| Column | Use | Meaning |
|--------|-----|---------|
| `github_id` | consumer | GitHub's numeric account id; null for an `EnterpriseUserAccount` |
| `node_id` | consumer | GitHub's GraphQL node id |
| `login` | consumer | Last known login |
| `type` | consumer | GitHub's type |
| `name` | consumer | GitHub's `name`; null until the first lookup, for a `Bot`, and when there is none |
| `created_at` | consumer | GitHub's `createdAt`; null until the first lookup |
| `updated_at` | consumer | GitHub's `updatedAt`; null until the first lookup |
| `linked_user_github_id` | consumer | An `EnterpriseUserAccount`'s user id |
| `claimant_github_id` | consumer | A `Mannequin`'s claimant id |
| `refreshed_at` | consumer | GitHub's `Date` header of the response that inserted the row or of its latest lookup that returned the account; a `NOT_FOUND` lookup leaves it |
| `not_found_at` | consumer | GitHub's `Date` header of the first lookup that answered `NOT_FOUND` since the account was last seen; null otherwise |
| `automation_reason` | consumer | Classification: the reason of the `automation_accounts` entry for the account's `github_id`; null when none lists it |

### `v_account_profiles`

One row per `account_profiles` row, with its account's keys.

| Column | Use | Meaning |
|--------|-----|---------|
| `github_id` | consumer | The account's numeric id |
| `node_id` | consumer | The account's node id |
| `login` | consumer | The account's last known login |
| `type` | consumer | `User` or `Organization` |
| `automation_reason` | consumer | Classification: the reason of the `automation_accounts` entry for the account's `github_id`; null when none lists it |
| `company` | consumer | A user's profile `company` |
| `location` | consumer | The profile's `location` |
| `website_url` | consumer | The profile's `websiteUrl` |
| `description` | consumer | An organization's `description` |
| `email` | consumer | An organization's public `email` |
| `twitter_username` | consumer | An organization's `twitterUsername` |
| `is_verified` | consumer | An organization's `isVerified` |
| `archived_at` | consumer | An organization's `archivedAt` |
| `has_sponsors_listing` | consumer | An organization's `hasSponsorsListing` |
| `pinned_items_remaining` | consumer | An organization's `pinnedItemsRemaining` |
| `refreshed_at` | consumer | GitHub's `Date` header of the lookup that wrote the profile |

### `v_pr_reviews`

One row per stored review.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_github_id` | consumer | The repository's numeric id |
| `repository_full_name` | consumer | `owner/name` of the repository |
| `repository_is_tracked` | consumer | The repository's `is_tracked` |
| `pr_number` | consumer | Pull request number |
| `pr_github_id` | consumer | GitHub's numeric pull request id |
| `review_github_id` | consumer | GitHub's numeric review id |
| `review_node_id` | consumer | The review's node id |
| `state` | consumer | GitHub's review `state` |
| `created_at` | consumer | GitHub's `createdAt` |
| `submitted_at` | consumer | GitHub's `submittedAt` |
| `published_at` | consumer | GitHub's `publishedAt` |
| `updated_at` | consumer | GitHub's `updatedAt` |
| `last_edited_at` | consumer | GitHub's `lastEditedAt` |
| `includes_created_edit` | consumer | GitHub's `includesCreatedEdit` |
| `commit_oid` | consumer | Oid of the review's commit |
| `author_association` | consumer | GitHub's `authorAssociation` when hydrated |
| `author_can_push_to_repository` | consumer | GitHub's `authorCanPushToRepository` when hydrated |
| `inline_comments_total` | consumer | GitHub's count of the review's inline comments |
| `reviewer_reported` | consumer | 1 when GitHub returned the reviewer in the latest hydration |
| `reviewer_github_id` | consumer | The reviewer's numeric id |
| `reviewer_node_id` | consumer | The reviewer's node id |
| `reviewer_login` | consumer | The reviewer's last known login |
| `reviewer_type` | consumer | The reviewer's type |
| `reviewer_automation_reason` | consumer | The reviewer's `[automation]` reason |
| `editor_github_id` | consumer | The last editor's numeric id |
| `editor_node_id` | consumer | The last editor's node id |
| `editor_login` | consumer | The last editor's last known login |
| `editor_type` | consumer | The last editor's type |
| `editor_automation_reason` | consumer | The last editor's `[automation]` reason |
| `is_pr_author` | consumer | Classification: 1 when the reviewer is the pull request's author, 0 when not; null when either reference is null |

### `v_pr_comments`

One row per stored conversation comment.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_github_id` | consumer | The repository's numeric id |
| `repository_full_name` | consumer | `owner/name` of the repository |
| `repository_is_tracked` | consumer | The repository's `is_tracked` |
| `pr_number` | consumer | Pull request number |
| `pr_github_id` | consumer | GitHub's numeric pull request id |
| `comment_github_id` | consumer | GitHub's numeric comment id |
| `comment_node_id` | consumer | The comment's node id |
| `created_at` | consumer | GitHub's `createdAt` |
| `published_at` | consumer | GitHub's `publishedAt` |
| `updated_at` | consumer | GitHub's `updatedAt` |
| `last_edited_at` | consumer | GitHub's `lastEditedAt` |
| `created_via_email` | consumer | GitHub's `createdViaEmail` |
| `includes_created_edit` | consumer | GitHub's `includesCreatedEdit` |
| `author_association` | consumer | GitHub's `authorAssociation` when hydrated |
| `author_reported` | consumer | 1 when GitHub returned the author in the latest hydration |
| `author_github_id` | consumer | The author's numeric id |
| `author_node_id` | consumer | The author's node id |
| `author_login` | consumer | The author's last known login |
| `author_type` | consumer | The author's type |
| `author_automation_reason` | consumer | The author's `[automation]` reason |
| `editor_github_id` | consumer | The last editor's numeric id |
| `editor_node_id` | consumer | The last editor's node id |
| `editor_login` | consumer | The last editor's last known login |
| `editor_type` | consumer | The last editor's type |
| `editor_automation_reason` | consumer | The last editor's `[automation]` reason |
| `is_pr_author` | consumer | Classification: 1 when the comment's author is the pull request's author, 0 when not; null when either reference is null |
| `author_also_reviewed` | consumer | Classification: 1 when the author has a stored review on the same pull request, 0 when not; null when the author is null |

### `v_commit_authors`

One row per stored commit.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_github_id` | consumer | The repository's numeric id |
| `repository_full_name` | consumer | `owner/name` of the repository |
| `repository_is_tracked` | consumer | The repository's `is_tracked` |
| `pr_number` | consumer | Pull request number |
| `pr_github_id` | consumer | GitHub's numeric pull request id |
| `sha` | consumer | Commit SHA |
| `linked_github_id` | consumer | The account id GitHub linked the commit to in the latest hydration |
| `author_name` | consumer | Git author name |
| `author_email` | consumer | Git author email |
| `authored_at` | consumer | Git author date (D6) |
| `account_github_id` | consumer | The linked stored account's numeric id |
| `account_node_id` | consumer | The linked stored account's node id |
| `account_login` | consumer | The linked stored account's last known login |
| `account_type` | consumer | The linked stored account's type |
| `account_automation_reason` | consumer | The linked stored account's `[automation]` reason |
| `email_automation_reason` | consumer | Classification: the reason of the `automation_emails` entry equal to `author_email` with ASCII letters lowercased; null when none |

### `v_pr_relationships`

One row per relationship of a pull request to an account, a team, or an actor GitHub did not
return, from the stored rows, with the pull request's `additions`, `deletions`, and
`changed_files`. `relationship` names the source:
`author` (one per pull request), `merged_by` (one per merged pull request), `review` (one per
review), `comment` (one per comment), `review_request` (one per request), `assignee` (one per
assignee), `commit_author` (one per commit), and `pr_editor`, `review_editor`, and
`comment_editor` (one per pull request, review, or comment that GitHub lists an editor or a
`last_edited_at` for). Columns that belong to another source are null.

| Column | Use | Meaning |
|--------|-----|---------|
| `repository_github_id` | consumer | The repository's numeric id |
| `repository_full_name` | consumer | `owner/name` of the repository |
| `repository_is_tracked` | consumer | The repository's `is_tracked` |
| `pr_number` | consumer | Pull request number |
| `pr_github_id` | consumer | GitHub's numeric pull request id |
| `relationship` | consumer | The stored row the view row comes from |
| `review_github_id` | consumer | The review's id, on `review` and `review_editor` rows |
| `comment_github_id` | consumer | The comment's id, on `comment` and `comment_editor` rows |
| `request_node_id` | consumer | The request's node id, on `review_request` rows |
| `request_github_id` | consumer | The request's `databaseId`, on `review_request` rows |
| `request_ordinal` | consumer | The request's position in GitHub's list, from 0, on `review_request` rows |
| `commit_sha` | consumer | The commit's SHA, on `commit_author` rows |
| `reported` | consumer | The source row's `*_reported` value, on `author`, `merged_by`, `review`, and `comment` rows |
| `account_github_id` | consumer | The account's numeric id |
| `account_node_id` | consumer | The account's node id |
| `account_login` | consumer | The account's last known login |
| `account_type` | consumer | The account's type |
| `account_automation_reason` | consumer | The account's `[automation]` reason |
| `account_not_found_at` | consumer | The account's `not_found_at` |
| `review_state` | consumer | The review's `state`, on `review` rows |
| `review_submitted_at` | consumer | The review's `submitted_at`, on `review` rows |
| `requested_type` | consumer | The requested reviewer's type, on `review_request` rows |
| `requested_github_id` | consumer | The requested account's or team's id, on `review_request` rows |
| `team_slug` | consumer | The requested team's slug, on `review_request` rows |
| `as_code_owner` | consumer | GitHub's `asCodeOwner`, on `review_request` rows |
| `linked_github_id` | consumer | The commit's `linked_github_id`, on `commit_author` rows |
| `author_name` | consumer | The commit's git author name, on `commit_author` rows |
| `author_email` | consumer | The commit's git author email, on `commit_author` rows |
| `email_automation_reason` | consumer | The commit email's `[automation]` reason, on `commit_author` rows |
| `pr_state` | consumer | The pull request's `state` |
| `pr_created_at` | consumer | The pull request's `created_at` |
| `pr_merged_at` | consumer | The pull request's `merged_at` |
| `pr_closed_at` | consumer | The pull request's `closed_at` |
| `base_ref` | consumer | The pull request's base branch name |
| `targets_default_branch` | consumer | The pull request's `targets_default_branch` |
| `merge_commit_oid` | consumer | The pull request's merge commit oid |
| `additions` | consumer | The pull request's `additions` |
| `deletions` | consumer | The pull request's `deletions` |
| `changed_files` | consumer | The pull request's `changed_files` |
