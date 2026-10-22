# Redesign Plan

This is the plan of record for rebuilding the GitHub Activity DB as the data layer for the Prebid
activity dashboard. It records the decisions, the design, the execution steps, and the evidence each
design choice rests on. Update the step status table as work lands; do not rewrite history in it.

Pre-Step-1 facts were measured against `main` at `43fcf06`, Step 1 facts on the PR #5 branch (merged
as `8c5b16b`), and live GitHub on 2026-10-16. On 2026-10-16 the whole plan was audited against live
GitHub, GitHub's documentation, the installed libraries, and `main`, and corrected. On 2026-10-17
Step 3 was planned against `main` at `c1f474b`, and the library behaviours it relies on were probed
with the installed versions (section 7); Step 3a's review probes ran on the PR #11 branch (merged as
`6fedb05`). On 2026-10-18 the plan and `main` at `6fedb05` were audited against D29, and the
collection D29 calls for was measured against live GitHub (section 7). On 2026-10-21 Step 3b-2b
was planned against `main` at `2a85eb9`, and the timeline lookup's `since` and `filteredCount` were
measured against live GitHub (section 7). The same day the plan and `main` at `2a85eb9` were
audited for silent failures and silently wrong data under D37, and D38 records the decisions
(gaps 98-110). Counts that drift (PR and issue totals) are dated; regenerate them with
`ghactivity verify` once Step 3b-2b lands.

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
| 3 | New schema, PR sync engine, cutover (pull requests 3a, 3b-1, 3b-2a-1, 3b-2a-2a, 3b-2a-2b-1, 3b-2a-2b-2, 3b-2a-2c, 3b-2a-2d, 3b-2a-3, 3b-2b-1, and 3b-2b-2) | in progress: 3a done (PR #11), 3b-1 done (PR #14), 3b-2a-1 done (PR #19), 3b-2a-2a done (PR #21), 3b-2a-2b-1 done (PR #24), 3b-2a-2b-2 done (PR #25), 3b-2a-2c done (PR #26) |
| 4 | Issues, closing-issue links, and default-branch activity (pull requests 4a and 4b) | not started |
| 5 | Full bootstrap and verification (pull request 5, and 5b when Step 5 does not show that every measured edit kind changes `updatedAt`, or finds an index contradiction that recurs) | not started |
| 6 | Scheduled sync and publication | not started |
| 7 | Documentation rewrite and code audit | not started |

## 1. Scope

In scope: the database, the sync that fills it, its verification, and its scheduled publication.

Out of scope until this plan completes: the dashboard application (`prebid/repo-activity-dashboard`)
and replacing its current data layer, a query API, AI enrichment, deriving an account's company
affiliation (GitHub's public profile `company` is stored, D31), CI check status, releases, and repositories not in the tracked list.

Within scope the database collects and distinguishes data and never interprets it (D29).

## 2. Decisions

| ID | Decision |
|----|----------|
| D1 | Scope is the database only (section 1). |
| D2 | Coverage rule B (section 3). |
| D3 | Pull requests closed without merge are stored as GitHub reports them, with state `CLOSED` and `closed_at`, under the coverage rule (section 3); a reopened PR's row is updated in place. Until 2026-10-18 this decision deleted them. |
| D4 | Review requests are stored, one row per requested reviewer as GitHub currently lists it: an account (`User`, `Bot`, `Mannequin`), a `Team`, or an `EnterpriseTeam`, and whether it was requested as code owner. A reviewer is an account that submitted at least one review in a state the reviews query requests (APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED; never PENDING, so `totalCount` counts exactly the stored reviews). Whether a reviewer is the PR's author, its GitHub type, and its `[automation]` entry are columns, never filters (D29). |
| D5 | Conversation comments (the PR `comments` connection) are stored one row per comment with its author, times, and author association, never its text (D24). Inline review comments belong to reviews and are not stored as rows: each review stores GitHub's count of its comments (`PullRequestReview.comments.totalCount`). Whether a commenter also reviewed the PR is a column, never a filter (D29). |
| D6 | Stored dates: PR created, merged, closed, published, and last edited; issue created and closed; review created, submitted, published, updated, and last edited; comment created, published, updated, and last edited; commit authored (the git author date, set by the committer's client), and a default-branch commit also committed (D30); repository created, updated, pushed, and archived; label created and updated; account created and updated; organization archived (D31); activity event time (D30). Review ids do not follow submission order (section 7, "Review order"), so submission order comes from `submitted_at`. |
| D7 | No timeline history. The database mirrors GitHub's current state: a value GitHub changes is overwritten, except that a null actor never replaces a reference D28 keeps (the row's `*_reported` column records it); a child row a re-hydration no longer returns is removed (5.3 step 6); an item GitHub no longer lists is removed (5.3 step 7). History the database could keep instead is later work (section 9). |
| D8 | Labels are stored as currently applied, with no history, by name and GitHub's label id; each repository's current label set is stored with its color, description, and default flag. |
| D9 | The database file is published as a GitHub release asset. Data from the private tracked repositories (`prebid-agent-skills`, and from 2026-10-18 `prebidjs-releaser` and `automation-testing`, D13) is included. The release is in the public repository `prebid/github-activity-db`, so every asset is world-readable, private-repository rows included; that is accepted (the data is not proprietary, and the maintainer may make `prebid-agent-skills` public). The release is the only published copy: no hosted database server is run, since its storage is not worth the cost (the maintainer, 2026-10-18); each consumer downloads the latest snapshot and queries its own copy (5.6, "Consumer access"). |
| D10 | The scheduled sync authenticates as a GitHub App installed on the `prebid` organization. |
| D11 | The scheduled sync runs every 6 hours. |
| D12 | `classify_tags` and `ai_summary` are removed. AI enrichment is out of scope (section 1); where its output would live is decided when it is planned, apart from GitHub's facts (D29). |
| D13 | The tracked repositories are those `tracking.toml` lists. On 2026-10-18 the maintainer added the `prebid` repositories pushed to since 2025-04-18 that hold content and were not tracked: `prebid-cache`, `user-sync`, `shared-id`, `Shared-id-v2`, `prebid-mobile-ios-adapters`, `prebid-js-build-generator`, `code-scanner`, `currency-file`, `currency-file-generator`, and the private `prebidjs-releaser` and `automation-testing` (section 7, "Added repositories"); `prebid-im-streamlit` (retired, the maintainer's decision), `marketing-email-bot`, and the empty or placeholder private repositories are not tracked. Until then this decision added none. CI check status and releases are out of scope. |
| D14 | Assignees are stored, one row per assigned actor as GitHub currently lists it (`assignedActors`, which includes bots, where `assignees` lists users only); requested teams are stored as review requests (D4). Assignment events can land after `updatedAt` (2 of 268 observed, each within a minute), a lag the unsettled rule in 5.3 absorbs. |
| D15 | Commit co-authors are not stored. Requesting them inside the commit list raises GraphQL cost from 2 to 27 points for the same 25 PRs. |
| D16 | The request-pacing stack (`github/pacing/*`) is retired at cutover. The new sync makes about 400 index calls per run (about 800 with the online verify) plus hydration, and reads its budget from each response; from Step 4b each run also reads every default-branch history (1 point per 100 commits; Prebid.js has 4,560 since the floor) and every activity log (REST). |
| D17 | `user_tags` and `pr_user_tags` stay (empty), re-pointed at the new `pull_requests` table. The `search` and `tags` CLI stubs are removed. Until a tag producer exists, a rebuild, or a PR GitHub no longer lists, removes a PR's tags; a producer must first meet the identity rule in section 9. |
| D18 | The database is rebuilt from GitHub, not migrated row by row, and the rebuild holds GitHub's current state (D7). The first file's past snapshots of pending review requests (`reviewers`) and assignees are not migrated (section 7, "First-schema requests and assignees"); otherwise it holds nothing that cannot be re-fetched (0 user tags, 0 sync failures, 0 AI values). |
| D19 | PR and issue titles and bodies are stored; GitHub's HTML and plain-text renderings of them are not. A commit is stored like a comment: on a pull request, one row per commit with its sha, author link, git author name and email, and authored date, and nothing else GitHub reports on it (message, committer, committed date, parents, per-commit counts), by the maintainer's choice that the commit list is the weakest measure. A default-branch commit adds its committed date and GitHub's counts, its only time and size outside a pull request (D30). |
| D20 | SQLite stays in rollback-journal mode while `github_activity.db` is tracked in git. WAL mode persists in a file once set, so `scripts/check_db_journal.py` checks the tracked file's header as a pre-commit hook and a test. The connection listener does not set the journal mode: that statement needs a lock, so every connection would fail with "database is locked" while another process has a WAL database open. In WAL mode committed transactions can sit in a git-ignored `*-wal` file until a checkpoint, and SQLite documents separating a database from its journal as a corruption path. WAL is reconsidered in Step 6, after the database leaves git. |
| D21 | `tracking.toml` ships inside the package (`src/github_activity_db/tracking.toml`) and is read through `importlib.resources`, so an installed wheel and a checkout read the same list. `TRACKING_CONFIG_PATH` names a different file. |
| D22 | The CI test gate allows no skipped tests (`scripts/check_junit.py --max-skipped 0`). A test that must skip in CI needs the allowance raised in the workflow, in the same change. pytest reports `xfail` as skipped, so the same applies to an `xfail`. |
| D23 | Closing-issue links are read by the index as well as by hydration, and the stored links are compared with the index on every run, so a link added after merge, which does not change the PR's `updatedAt`, is captured: a PR whose indexed links differ from its stored links is planned for hydration (5.3 step 3). Hydration also reads which links GitHub lists as manually linked (`userLinkedOnly`), at no added cost (section 7). |
| D24 | Conversation comments are stored one row per comment, its `github_id`, author, and times and never its text, so a comment by an account the database never stored is a row with a null author, never dropped (D28). Review bodies, inline review comment text, and commit messages are not stored either. |
| D25 | A run, scheduled or manual, publishes when its starting database is valid (the downloaded snapshot passed `verify --db-only`, or `bootstrap` started from an empty database), sync exited 0 or 2, verify exited 0 (no failing class and no unverified repository, 5.4), and the run had no `repo` input or its release is not `db` (5.6). The manifest records the sync exit code and verify's counts per class. A final job that runs whatever the others did then marks the workflow failed if sync exited 2, verify failed, or nothing was published, so a failure is visible after the data is saved. |
| D26 | The Step 5 database is not committed; it becomes the first snapshot of release `db` (Step 6b). From Step 3 until 6c removes it, the tracked `github_activity.db` keeps its pre-Step-3 content: the default `DATABASE_URL` moves to a git-ignored file, and `scripts/check_frozen_db.py` fails when the file's SHA-256 differs from the digest it pins, as a pre-commit hook on the file (so CI's `--all-files` run checks it) and as a test; the tests also fail when git stores the file other than as one regular file, and the `tracked-db-no-sidecar` hook refuses a committed `-journal` or `-wal` file beside it, which SQLite would apply on the next open, and the `-shm` index that goes with a `-wal`. |
| D27 | A stored repository no longer listed in `tracking.toml` is reported, with exit 2, on every run without `--repo` (5.3 steps 1 and 8), and its rows stay until `ghactivity sync --prune-untracked` deletes them; the prune, on the operator's explicit flag, removes rows section 1 puts out of scope (repositories not in the tracked list) and is recorded in `sync_runs.deletions_approved` (5.1). |
| D28 | The database keeps a person attached to everything it attributed to them. A stored account stays stored after a lookup returns `NOT_FOUND` for it, with its last known login and type, and every reference to it stays: when GitHub later returns that actor as null, as a re-hydration does once GitHub stops returning the account, the writer keeps the stored reference of the same object (the PR's author and merger, the issue's author, the review's reviewer or the comment's author with the same `github_id`, this PR's commit with the same SHA, and the default-branch commit with the same repository and SHA, Step 4b), and the row's `*_reported` column records that GitHub returned no actor in that hydration (5.1). Other actor references (editors, requested reviewers, event actors) take GitHub's current value, null included, and the assignees are GitHub's current `assignedActors` list. `not_found_at` (named `deleted_at` until Step 3b-1) records when a lookup returned `NOT_FOUND`; it changes no reference, the 24-hour refresh skips the account, and a later sighting of the account clears it. GitHub does not say why it returns a null actor (`Prebid.js#13769` is merged, with a null `mergedBy` and an existing author), so a null reference means only that GitHub returned no actor and the object had none stored, and an account never stored has no `github_users` row. A non-null actor GitHub returns replaces the stored one. On a commit, `linked_github_id` holds the account id GitHub linked in the latest hydration whether or not a lookup found the account, so a link whose lookup returned null is told apart from an email linked to no account. The attribution lives in the stored rows and cascades with them, so anything that re-creates a row from GitHub drops it for accounts GitHub no longer returns: a bootstrap (it starts from an empty file), a transferred issue (new `github_id`), and a repository pruned and re-added (D27). |
| D29 | The database collects and distinguishes data; it does not interpret it. Within scope (section 1) it stores what GitHub reports about the items it holds, under GitHub's names and values. No consumer surface leaves rows out, merges several facts into one verdict or flag, reports a total GitHub did not report, attaches a value GitHub did not give, chooses, orders, or reduces rows to one value, or describes a value with a meaning GitHub does not state. Counts GitHub reports (`totalCount`) are stored; the views are row-level, so aggregation, counting included, is the consumer's. A column the database computes is allowed only as a classification: a deterministic function of stored columns in the same file (equality, existence, or membership in a `tracking.toml` list) whose rule the data dictionary states. These are `targets_default_branch` (against the repository's current default branch), `is_pr_author`, `author_also_reviewed`, `automation_reason`, and `email_automation_reason`. `pr_closing_issues.user_linked` stores GitHub's second list (`userLinkedOnly: true`) as a flag on each link of the full list, which holds every user-linked link (5.3 step 5), so the flagged links reproduce that list's members. The `ordinal` columns hold each entry's position in GitHub's list, numbered from 0, which keeps GitHub's order. A fact GitHub reports on an object the database already requests is collected when it adds no cost, unless a decision or the field rules exclude it (D5, D15, D19, D24, D31, D32; section 7, "Field dispositions" and "Empty fields"). Columns that record the database's own configuration and runs, not GitHub's data (`is_tracked`, `repo_group`, `hydration_version`, the automation tables, and the run records `sync_runs`, `repo_sync_state`, `sync_pending`, and `sync_failures`), and a view's `relationship` label naming the stored row a view row comes from, are outside these rules; the data dictionary defines each. A fact that needs further requests or connections is collected only when a decision admits it at its measured cost: D4, D5, D14, and D23 admit review requests, assignees, each review's inline-comment count, and the manually linked closing issues, which add none; section 9 lists the rest. D7's mirror is not interpretation: an overwrite with GitHub's current value, and a removal GitHub made, are GitHub's changes, and the exceptions are marked: D28's kept references (by `*_reported`, and for commits by `linked_github_id`), D28's kept accounts (by `not_found_at`), and D27's untracked repositories (by `is_tracked`). |
| D30 | Each tracked repository's default-branch activity is stored as GitHub reports it (Step 4b): every event in GitHub's repository activity log for the default branch, with its id, type, time, actor, ref, and before and after commits; GitHub's compare of each event's before and after commits, as compare returns it (its status, counts, merge base, commits, and files); and the default branch's commit history since the floor, one row per commit with its author like `pr_commits`, its committed date, and GitHub's line and file counts for it, a default-branch commit's only time and size outside a pull request (D19). Nothing classifies an event or a commit as a direct push or as reviewed: a consumer joins a `pr_merge` event to its PR through the PR's `merge_commit_oid` (each of the 11 measured events' `after` equalled its PR's merge commit, 6 merge-commit and 5 squash merges; no rebase merge was measured), and a commit to the event that brought it through the stored compare commits. Before the log starts, the commits a rebase merge wrote match no merge commit and no event, so they join to no PR (no rebase merge was measured; section 8). GitHub's log starts on 2023-03-07 for Prebid.js and prebid-server, and `Commit.pushedDate` is deprecated as no longer supported and returned null on the history commits read, so for changes before the log starts the commit history is the only record, with the committed date in place of a push time (section 7, "Repository activity"). |
| D31 | Account profiles are stored, one `account_profiles` row per `User` or `Organization` a lookup has read (a `Bot`, `Mannequin`, or `EnterpriseUserAccount` has none), keyed by the account's `github_users` row (`account_id`), which a consumer reaches from the account's `github_id`, or `node_id`, through `github_users` or `v_account_profiles` (5.1), as GitHub's public profile reports them at the latest lookup: a `User`'s `company`, `location`, and `websiteUrl`, and an `Organization`'s `description`, `email`, `location`, `websiteUrl`, `twitterUsername`, `isVerified`, `archivedAt`, `hasSponsorsListing`, and `pinnedItemsRemaining`. The maintainer chose the user fields; the rest of a user's profile (biography, pronouns, Twitter, status, hireable and GitHub-program flags) is not collected. An organization's other fields are not selected when GitHub returns them only to its members or admins: `webCommitSignoffRequired`, `membersCanForkPrivateRepositories`, `ipAllowListEnabledSetting`, `ipAllowListForInstalledAppsEnabledSetting`, and `notificationDeliveryRestrictionEnabledSetting` return FORBIDDEN to a non-member, which nulls the whole organization in the response, and `requiresTwoFactorAuthentication` and `organizationBillingEmail` need `admin:org`, so selecting any of them would fail lookups of other organizations. The sponsorship amounts describe the token's access, not the organization: `estimatedNextSponsorsPayoutInCents` and `monthlyEstimatedSponsorsIncomeInCents` were 0 for each of 12 organizations with a sponsors listing, and GitHub returns `totalSponsorshipAmountAsSponsorInCents` only to the account or a user who can manage its sponsorships. `anyPinnableItems` (true) and `interactionAbility` (null) were the same on all 20 organizations read (D32; section 7, "Account profiles"). A mannequin's `email` is not selected (section 8). The public release publishes them (D9), as GitHub's public profiles already show them. A `User`'s profile `email` is not collected: GitHub requires the `user:email` or `read:user` scope for it (the token used here has neither, and the scheduled App's access is not measured), and commit emails, which GitHub links to accounts, are stored per commit (`author_email` with `linked_github_id`, D28), so an email held elsewhere matches a commit email to its account. Organization memberships are not collected: GraphQL's `organizations` lists the memberships the token can see, so it would publish private ones, and REST's public list (`/users/{username}/orgs`) was empty for both accounts read (section 7, "Account profiles"). |
| D32 | Field decisions of 2026-10-18 (the maintainer): a PR's merge state (`mergeable`, `mergeStateStatus`, `canBeRebased`, `potentialMergeCommit`) is collected as of the PR's latest hydration, and open PRs are not re-hydrated each run for it (5.1); a PR's stack and stack entry are collected; auto-merge requests, merge-queue entries, and the auto-merge and merge-queue settings are not (none of the repositories read uses them, section 7, "Merge state and stacks"); projects are not (they need the `read:project` scope and a connection); suggested reviewers and a referenced milestone's or team's own fields are not; and a field empty or identical across the measured sample is not collected, except a repository's archive and visibility status, its owner, the merge-method settings, and stacks (section 7, "Empty fields"). |
| D33 | `prebid-agent-skills` is never queried by an agent. Commands an agent runs (sync, verify, the REST sampler, the difference report, recordings, and `ghactivity github test`) name their repositories with `--repo` and leave it out; its rows enter the database through the scheduled App's workflow (Step 6) or a run the maintainer starts. Recordings come from public repositories only (Step 2). |
| D34 | Decision of 2026-10-20 (the maintainer): wherever stored data must tell runs apart, a run is its `sync_runs` row id, stored with the data, never a clock reading and never state the writer or the engine keeps in memory, which neither a later run nor a reader of the file can see. `sync_failures.last_run_id` names the latest run in which the item failed, and `attempts` adds one only for a failure in another run, whatever the two runs' clocks read (5.1, gap 91). |
| D35 | Engine decisions of 2026-10-20 (the maintainer): Step 3b-2a-2b is two pull requests, the scenario model and the engine with `--dry-run` (section 6). A run can stop at any point: every write is its own transaction and every close updates its row only while it is `running`, so whatever ends a run without its close leaves the row `running` for the stale-row rule or `--break-lock` to recover, and nothing shields a writer transaction from an interrupt (5.3, gap 93). The deadline is reached when a request or a wait is refused for it (5.3 step 4), and the engine hydrates and writes one batch at a time (5.3 step 6). An `AccountMissing` refusal is an item failure, so an engine defect still publishes (5.3 step 6, D25); two entries resolving to one repository fail only the later one (5.3 step 1, gap 92); and D27's exit 2 applies to runs without `--repo`. |
| D36 | Verify decisions of 2026-10-21: Step 3b-2b is two pull requests, the REST sampler and then verify with the live acceptance (section 6). Verify writes nothing to the database and claims no run: before any request it reads `sync_runs` and exits 1 while a row holds the lock by the claim's rule, so a stale or crashed run stays for the next claim or `sync --break-lock` (5.3). `--repo` limits verify to the repositories it names, as it limits a sync (D27). A verify that never starts writes no report; once it starts, `--report` is written on every exit verify's own code handles, and one that cannot be written turns an exit 0 or 2 into 1, because Step 6a reads it, while an interrupt exits 130 either way; verify's printed output follows sync's rule (5.3 step 8). Every repository failure 5.3 gives a resolve, an index, or a hydration batch, a repository error during a timeline lookup, and from Step 4b a log or history read verify cannot complete make a repository unverified, and a verify in which every repository it attempts is unverified, the deadline having stopped none, is fatal. verify reads each repository's stored rows in two statements before indexing it, and from Step 4b its stored events and default-branch commits in one statement each before the first request of its log and history reads, and its report records its own points, REST calls, and time (5.4, gap 94). |
| D37 | Decision of 2026-10-21 (the maintainer): an error is better than a silent failure or silently wrong data. Where the code cannot check something, or cannot accept what GitHub returned, it reports a failure that stops publication or turns the run red, never a pass. For verify: an unchecked item and an unverified repository fail verify in both modes, not only `--exact`, and a lookup whose answer verify cannot accept is an error (5.4, 5.8, gap 97), so D25 does not publish a database verify could not check. |
| D38 | Decisions of 2026-10-21 applying D37 to the sync and to the steps not yet built (gaps 98-110). The sync exits 2 when a 24-hour account refresh fails, when the run reaches its deadline, and when a hydration contradicts the run's own index after one retry (Step 3b-2a-3); the failures a batch made final before a deadline are recorded, not left pending. An `accepted_failures` entry accepts one error class and one cause, the first line of the failure's message, of an item, an account's refresh, or from Step 4b a compare, so a permanent failure can be accepted without turning every run red while a new cause is not. A PR with more closing links than the index carries is hydrated every run. Every node id a sighting, a lookup, or a commit link reports for a stored account is stored, and `sync_runs` records the `--repo` filter, each configured entry's `github_id`, and its first commit-link lookups and those GitHub answered `NOT_FOUND`. The source refuses what it cannot read (gap 104), and the run records refuse what they cannot write and mark `-unknown` what they cannot determine (gap 105). Step 4b marks a compare file list at GitHub's 300-file cap and counts it in verify's report and the manifest, completes an activity read only at the floor or at the log's first event, as a read in ascending order finds it, fails a history read whose count still differs after a rescan, and records a compare that still fails, which an entry can accept. Step 5 measures which edits leave `updatedAt` unchanged. Step 6 compares the App's reads with the token's before any App run publishes to `db`, pins the database file the job checked, tests that every exit-code `if:` compares a non-empty string, and ties a snapshot to the job's own sync. A schedule that stops is the one silent case this repository cannot see: no run starts, so nothing turns red; 5.6 states the staleness rule a consumer applies, and building it belongs to the dashboard's integration (section 9). Left as they are, because the stored data already says what happened: a commit-linked account a lookup answers `NOT_FOUND` (D28; now counted per run and in the manifest, and from Step 6a a run whose `links_not_found` exceeds the share of its `link_lookups` Step 5 sets, with at least Step 5's minimum number of lookups, exits 2 (an account an earlier lookup answered `NOT_FOUND` counted in neither), since a lookup form GitHub stopped resolving would look the same per link), a compare at GitHub's 300-file cap and one GitHub refuses (marked by `files_at_cap` and `http_status`, counted in verify's report and the manifest), a REST git author without a name or email (stored null, as GitHub reports none), deletions recorded as a count (`items_deleted`, and from Step 4b `events_deleted` and `history_commits_deleted`; the snapshots retained for 7 days hold the identities; older ones are not kept), which accounts failed a refresh (printed; the count is stored, the run is red unless an account entry accepts the failure, and the accounts are retried), an `author_association` difference (verify reports it as drift with its own count: GitHub computes association when read and changes it without moving `updatedAt`, section 7, so no read tells a change from a defect), and the merge state as of `hydrated_at` (D32). New tables and columns of GitHub-derived data on a populated file follow gap 110's rule (5.1). |
| D39 | Decision of 2026-10-21 (the maintainer). Nothing is planted or tampered, no flaw is seeded, and there is no mutation testing, under any name. No test, review, or acceptance changes a file or a copy of one (other than a stub or configuration the inputs below allow), or writes a file, a row, a revision, a call site, or a decoy holding a flaw, to see whether something catches it, and none measures how many such flaws are found. A test's or an acceptance's inputs are GitHub's recorded responses; stubs of a response GitHub or the environment can send (a status, a body, a timeout, an interrupt, a failing `git`, a clock), which a test may build in memory from a recorded unit changed into that shape; calls the code under test must refuse, and configuration it must refuse or that names a state GitHub can produce (a repository that does not resolve, one no longer listed); valid rows a test writes (through the models with `tests/db/seed.py`, or in SQL); and databases and files this project's own code wrote, at any revision. Each test asserts the exact result. A check over stored data, files, or source that only a defect of this code could fail is tested on its passing side, with the exact count of the rows, files, or values it compared, so it cannot pass on nothing; a check that GitHub, the environment, or a caller can fail is tested on both sides. A fix comes with a regression test that fails against the parent commit's code. Tests merged before D39 that change a file or a copy of one, or write a file, a revision, a call, a connection, or a decoy holding a flaw, to see whether a check catches it are replaced in Step 3b-2a-2d. |

## 3. Coverage rule

Floor `F = 2023-01-01T00:00:00Z`, set in `tracking.toml` `[coverage]` (added in Step 3b-2a-1). The
loader rejects a floor that is not a TOML offset date-time and converts it to UTC.

- A pull request is in coverage when its state is `OPEN`, or its state is `MERGED` and
  `mergedAt >= F`, or its state is `CLOSED` and `closedAt >= F` (D3). (`mergedAt >= createdAt`
  always holds, so "created or merged on or after F" reduces to the merged clause.)
- An issue is in coverage when its state is `OPEN`, or `createdAt >= F`, or `closedAt >= F`.
- Default-branch activity (D30): an activity event is in coverage when its `timestamp` is at or
  after F, and a default-branch commit when the history's `since: F` lists it (whether `since`
  filters on the committed or the authored date is not measured, section 8).

An index entry whose state its dates contradict (`CLOSED` without `closedAt`, `MERGED` without
`mergedAt`, or `mergedAt` with another state) counts as in coverage, so it is never a deletion
candidate, and sync records it as an item failure without hydrating it (5.3 step 3). The index
passes such an entry through rather than failing the repository; once an index lists the item with
consistent values, the rule applies to it as to any other.

Every input to the rule is immutable once set except state and `closedAt`, so an item does not enter
coverage because of housekeeping. A PR that closes, reopens, and closes again gets a later
`closedAt`, so a PR in coverage stays in it. The alternative rule "updated on or after F" admitted 567
extra PRs and 22 extra issues whose only post-floor activity was events such as branch deletion (one
branch cleanup on 2023-07-05 touched 150 Prebid.js PRs created before 2023, some from 2016).

Coverage on 2026-10-16: 11,848 pull requests (open and merged; the closed-unmerged PRs D3 now adds
were not counted) and 4,857 issues across the 18 repositories then tracked. The 11 repositories
added on 2026-10-18 (D13) hold 289 pull requests in all, not filtered by the rule (section 7,
"Added repositories").

## 4. Gap register

Every gap found in the audit, and where it is resolved.

| # | Gap | Resolution |
|---|-----|------------|
| 1 | No issues table or issue sync | Step 4 |
| 2 | No GitHub user id or account type; bots indistinguishable | Step 3: `github_users` keyed by GitHub id; type and login from GraphQL actor sightings and node lookups (5.2); the account's type, and the `[automation]` entry beside it, never merged (gap 83). `prebidjs-release` (99054218) and `claude` (81847) are type `User`. Logins change: `blueseasx` became `blueseasx-prebid`. |
| 3 | Review order lost; final reviewer state unknown | Step 3: one row per review keyed by review id; Step 3b-1: each review's created and submitted times (D6); no view derives a verdict (D29, gap 82) |
| 4 | Validation silently drops commits (`author: {}`), reviews (`user: null`), files (`sha: null`) | Steps 2, 3: no silent skips; failures are recorded. GraphQL file objects carry no `sha`; unlinked commit authors keep git name and email; comments GitHub returns with a null author are stored with a null author unless their author was already stored (D24, D28) |
| 5 | 10,895 PRs with file status `unknown` and zero per-file line counts | Step 5: rebuild from GitHub |
| 6 | Closed-unmerged PRs partially stored | D3: stored with state `CLOSED` (Step 3b-1 collects them; 3b-2a-2a writes them) |
| 7 | `reviewers` holds pending review requests | D4: review requests stored as GitHub lists them (Step 3b-1 collects them; 3b-2a-2a writes them) |
| 8 | Commenters not captured | Inline review comments already belong to reviews (168 of 168 sampled); each review stores their count (D5). Conversation commenters: D5, D24 |
| 9 | No review timestamps | D6: review and comment times (Step 3b-1 collects them; 3b-2a-2a writes them) |
| 10 | Commit lists truncated at 250 | Step 2: compare endpoint, paged, count asserted |
| 11 | Coverage floor undefined; partial history | Section 3; `verify` |
| 12 | AI fields empty and erased by re-sync | D12 |
| 13 | Draft and other fields discarded | Step 3: `is_draft`, `base_ref`, `targets_default_branch`, `author_association`, `is_cross_repository`, automation list entries, per-file stats, per-connection totals |
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
| 39 | Null PR author, reviewer, commenter, issue author, and merger occur | Step 3: nullable references, kept for stored accounts (D28); comments GitHub returns with a null author stored with a null author (D24) unless their author was already stored (D28) |
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
| 52 | The migration test passed while migrating `DATABASE_URL` (by default the tracked database) if `alembic/env.py` ignored its URL override; two `tracking.toml` guards (unknown top-level keys, empty values) had no test | Step 1: the migration test checks the file it migrates and the head revision; guard tests |
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
| 68 | `scripts/count_suppressions.py` does not count ruff's `# ruff: disable[...]` range suppression, which ruff 0.14.11 honors only in preview | Dormant while `preview` is off (`pyproject.toml` sets none). Resolved before Step 3: the counter counts each `# ruff: disable` in any letter case, never `# ruff: enable`, and counts the spellings section 7 lists as honored (section 7, "Range suppressions"). It refuses (exit 1) a file it may read differently from ruff: any non-UTF-8 coding declaration, a lone CR, a file pre-commit's file tagging (identify) marks python or pyi without a lowercase `.py` or `.pyi` suffix, such as `module.PY`, `app.wsgi`, or an executable with a `#!/usr/bin/env python3` shebang, or a file identify cannot read (section 7, "Counter file forms") |
| 69 | click exits 2 on every usage error (an unknown option or command, an out-of-range value), the code D25 publishes on, so a mistyped workflow input would publish the unchanged database | Step 3a: a run that never starts exits 1 (5.3 step 8) |
| 70 | Under alembic's SQLite defaults each revision commits on its own and schema changes are not transactional: a revision that fails after dropping a table leaves it dropped, with the previous revision stamped. A statement executed on the migration connection before `context.configure()` makes alembic treat the transaction as external and never commit: `upgrade` exits 0 with no revision stamped | Step 3a: one transaction per upgrade, nothing executed before `context.configure()` (5.1) |
| 71 | A batch alter of a table that a view reads fails ("error in view ...: no such table") | Step 3a: migrations drop and recreate the dependent views (5.1) |
| 72 | Batch recreate drops unnamed CHECK constraints; SQLAlchemy emits no CHECK for `Boolean` or `Enum` on SQLite; `alembic check` compares neither | Step 3a: every CHECK named, through a naming convention; the 5.1 comparison test compares CHECK names |
| 73 | An explicit `DATABASE_URL` naming the tracked file (the `.env` line `docs/development.md` shows) is migrated without complaint: the tag-row guard passes on its 0 tag rows | Step 3a: `alembic/env.py` refuses the repository-root `github_activity.db` (5.1, D26) |
| 74 | Nothing stops `sync` or `verify` from running against an older revision or an empty file (the new default URL creates one on first connect), and the wheel ships no `alembic/` directory (`pyproject.toml` packages only `src/github_activity_db`), so the installed code cannot read the revision chain | Step 3a: a packaged tuple of revision ids, kept equal to the chain by a test; Step 3b-2a-2: `sync` (the head check 3b-2a-2a's, called by the engine), and Step 3b-2b-2: online `verify`, exit 1 unless the database is at head (5.3, 5.4) |
| 75 | typer prints local variable values in tracebacks by default and `cli/app.py` does not turn it off; every loguru handler `setup_logging` adds shows them too (both stderr handlers set `diagnose=True`, and the optional file handler takes loguru's default, also true). Step 6 runs the CLI in a public repository's Actions logs, which mask only registered secrets | Step 3a: typer's `pretty_exceptions_show_locals` and `diagnose` on all three handlers off (5.7) |
| 76 | `source/` imports httpx directly (`client.py`, `transport.py`), but `pyproject.toml` does not declare it; it is installed only as a githubkit dependency | Resolved before Step 3a: `pyproject.toml` declares `httpx>=0.28`, the line `uv.lock` resolves (0.28.1) and the tests run on; githubkit allows `>=0.23.0,<1.0.0`. `tests/test_declared_dependencies.py` fails when an `import` or `from` statement in a `src/**/*.py` file (`TYPE_CHECKING` blocks and function bodies included) names a module that no declared dependency provides; imports by string (`importlib.import_module`, `__import__`) and `.pyi` stubs are not read. Installed metadata (`importlib.metadata.packages_distributions`) maps only top-level names, so an import is accepted only when every distribution installing its top-level name is declared: a namespace package that several distributions share is reported with each of them, because a declared sibling must not stand in for the provider. A provider whose metadata names no importable module (an editable install recorded only as a `.pth` file) is not seen |
| 77 | In rollback-journal mode a read left half-consumed on another connection blocks a commit until the busy timeout, which then raises "database is locked"; the comment on `SQLITE_BUSY_TIMEOUT_MS` in `db/engine.py` says a lock fails the item. Through the aiosqlite dialect a query fetches every row and closes its cursor inside `execute`, so only a server-side cursor leaves a result half-read; a reader in another process (an open `sqlite3` shell) can still hold the lock | Step 3b-2a-1: no code in `src/` reads through a server-side cursor (`stream`, `stream_scalars`, `stream_results=True`, `yield_per`), the snapshot reader first; Step 3b-2a-2b-2: a database error in the writer is fatal (5.3 step 4); Step 3b-2a-2a: the comment is corrected |
| 78 | `source/users.py` checks a looked-up node's id against the requested node id, but not its `databaseId` against the commit link's, which only `sync/` holds | Step 3b-2a-2b-2: a mismatch fails the items waiting on the link (5.2) |
| 79 | Hydration raises `RepositoryNotFound` or `RepositoryShapeError` when a batch's `repository` is null; 5.3 assigned repository failures only to resolve and index | Step 3b-2a-2b-2: a row of the 5.3 step 4 table |
| 80 | Gap 44's rule (new code uses `{}` placeholders) has no gate, and ruff cannot be one: PLE1205 flags a correct `logger.warning("brace {}", value)` and passes `logger.warning("percent %s", value)` | Step 3a: an AST check over `src/`, run as a pre-commit hook (so CI's `--all-files` run checks it) |
| 81 | At `6fedb05`, `v_pr_reviewers` and `v_pr_commenters` leave out the PR author's own reviews and comments, accounts of type `Bot`, accounts listed in `[automation]`, and reviews and comments with a null author, and `v_pr_commenters` also leaves out commenters who reviewed the PR | Step 3b-1: row-level views that keep every row and carry each condition as a column (5.1, D29) |
| 82 | `v_pr_reviewers.final_verdict` reduces a reviewer's reviews to one state by a rule the database chose, ordered by review id, which does not follow submission order (section 7, "Review order") | Step 3b-1: removed; GitHub's own `reviewDecision` is stored on the PR (D29) |
| 83 | `is_automation` merges GitHub's type `Bot` with the maintainer's lists into one flag, computed from the latest run's copy of `tracking.toml` | Step 3b-1: the account's type and the matching `[automation]` entry's reason as separate columns, and the run that copied the lists (5.2) |
| 84 | Facts fetched and then dropped: each PR's `baseRefOid` and `headRefOid` (used only for the compare request and the page check), the account id GitHub links a commit to when the lookup returns null, and the repository's node id and label set | Step 3b-1 collects them; 3b-2a-2a writes them (5.1) |
| 85 | A PR with an `EnterpriseUserAccount` actor is an item failure and is not stored (5.2 at `6fedb05`) | Step 3b-1 collects such an actor and 3b-2a-2a writes it, the account keyed by its node id (5.2) |
| 86 | When a value was read is internal: `hydrated_at`, `hydration_version`, `refreshed_at`, `repo_sync_state`, `sync_failures`; a consumer cannot tell how current a row is or that an item failed | Step 3b-1: consumer columns (5.1) |
| 87 | The schema-version rule and its lock name only columns, so a change to which rows a view returns ships under the same `user_version` | Step 3b-1: a change to a view's rows or derivation bumps the version, and the lock entry records each consumer view's frozen-SQL SHA-256 (5.1) |
| 88 | Nothing records which `tracking.toml` produced `repo_group`, `is_tracked`, and the automation tables when `TRACKING_CONFIG_PATH` overrides the packaged file or the checkout is changed | Step 3b-2a-2a: `sync_runs.config_source` and `config_sha256` (5.1); Step 6a: the manifest (5.6) |
| 89 | Wording that gives data a meaning GitHub does not state: a null actor called a deleted account (the data dictionary, `cli/github.py`), "final verdict", a dismissal called a withdrawal, "reviewer metrics", "review activity", closing links said to close an issue (GitHub: "List of issues that may be closed by this pull request"), the `source/` docstrings that call a null actor or `NOT_FOUND` deleted, `github_users.deleted_at` itself, and `tracking.toml` groups described as "for reporting" | This plan (2026-10-18); Step 3b-1 (the data dictionary, `cli/github.py`, the docstrings in `source/convert.py`, `source/users.py`, and `source/records.py`, and `deleted_at` renamed `not_found_at`); Step 3b-2a-1 (the `tracking.toml` comment) |
| 90 | An account a lookup returned `NOT_FOUND` for, which GitHub links again only through commits, is never looked up again, so its `not_found_at` and login stay as they were | Step 3b-2a-2b-2: a commit link to a stored account with `not_found_at` set queues it for a lookup (5.2) |
| 91 | `sync_failures.attempts` told runs apart by comparing the stored `last_failed_at` with the run's start time, so a failure in a run that started, by its clock, at or before the previous failure did not count, and no column named the run that failed | Step 3b-2a-2a: `sync_failures.last_run_id` (migration `2d2ba76b177c`; D34, 5.1) |
| 92 | `plan()` raises `ValueError` when two configured entries resolve to one `github_id` (an old name and the new one both listed), and nothing catches it, so the run would end as an unexpected error | Step 3b-2a-2b-2: the later entry fails for the run, naming both, and makes the exit 2 (5.3 step 1) |
| 93 | A cancel that lands inside a transaction on the writer's connection can leave the abandoned connection holding the write lock; the run's close then fails after the busy timeout and the row stays `running` (measured: a cancel during a slow `SELECT` 3 of 3, during a slow `INSERT` 0 of 3, among alternating short `SELECT`s and `INSERT`s 3 of 24; section 7, "Cancelled writer transaction") | Step 3b-2a-2b-2: accepted; the row is left as a crash leaves it, for the stale-row rule or `--break-lock` (5.3) |
| 94 | 5.4 left verify's edges unstated: its lock check (the claim's rule runs only inside the claim, which writes); whether `--repo` limits the untracked repositories it reports; whether `--report` is written when verify stops before comparing or when the path cannot be written; the class of the resolve and index failures it did not list (not found, no default branch, a label list that fails its count, a number listed twice or out of creation order, a response the resolve or index rejects), beyond the three it did (a fetch that still fails, a second count mismatch, a resolve to another `github_id`), and of a repository error during a lookup; a verify that verifies no repository; when it reads the stored rows, which a sync that claims after its lock check can change; which events match a link difference GitHub reports only in the totals; and where its cost is recorded, since it writes no `sync_runs` row | Step 3b-2b-2 (5.4, D36) |
| 95 | `timelineItems(since:)` keeps only items created after `since`, so a lookup passing the boundary would miss an event at it; GitHub's schema describes `filteredCount` as the count after applying `before` and `after`, and names neither `itemTypes` nor `since` | Step 3b-2b-2: `since` one second before the boundary, and `filteredCount` used as measured (5.8; section 7, "Timeline `since`") |
| 96 | The REST sampler's lists have no stop rule (`Transport.rest_get` returns no `Link` header); the recorder's default mode rebuilds `recorded/` from what it records, and `--only documents` drops every unit kind but the compare and rate-limit kinds it keeps; and its check that a repository is public, after the name list it refuses before any request, runs on a resolve, which a REST-only recording does not make | Step 3b-2b-1 for the sampler and its REST units, recorded in their own directory; Step 3b-2b-2 for the timeline units (5.8) |
| 97 | Without `--exact`, an unchecked item and an unverified repository did not fail verify, and a timeline lookup whose entries differed from `filteredCount` left its item unchecked, so the scheduled verify (5.6, no `--exact`) could exit 0 and D25 publish a database it had not checked | D37: both fail verify in every mode, and an answer verify cannot accept is an error (5.4, 5.8) |
| 98 | A 24-hour account refresh that still fails, or whose answer the writer refuses (another `databaseId` for a stored node id), left the run `complete` with exit 0; the only trace was a printed line, and a test pinned exit 0 | Step 3b-2a-3: counted in `sync_runs.refresh_failures`, listed in the report, and exit 2 unless an account entry in `[sync].accepted_failures` accepts it (5.3 step 4, 5.5, D38) |
| 99 | A run whose deadline came after every index completed (in hydration, its lookups, or the refresh) exited 0, and a deadline inside a batch turned the item failures it had computed into pending items | Step 3b-2a-3: a run that reaches its deadline exits 2, which D25 still publishes with the workflow red, and the failures a batch made final before the deadline are recorded (5.3 steps 6 and 8, D38) |
| 100 | A PR the run's own index listed in coverage that hydration then answered `NOT_FOUND`, or returned out of coverage, was neither written, failed, nor pending, and nothing reported it; a hydration older than the index entry was written without a check | Step 3b-2a-3: each is an item error (5.3 steps 4 and 5, D38) |
| 101 | An `accepted_failures` entry accepted any failure of its item, so a new cause stayed out of exit 2 | Step 3b-2a-3: an entry names the error class and the cause it accepts, the first line of the failure's message, which carries no value that changes between runs, and may name an account (5.5, D38) |
| 102 | A PR with more than the 10 closing links the index carries was not re-hydrated when a link beyond the 10th was swapped with equal totals | Step 3b-2a-3: such a PR is hydrated every run (5.3 step 3, D38). Verify keeps comparing the totals and the indexed links: the sync re-reads every link of such a PR each run, and verify's child-count check holds the stored rows to the hydrated total |
| 103 | A `databaseId` reported under a second node id kept the stored node id and dropped the reported one; only a printed line recorded it; the sync used a commit link's node id, which the source reads, only as a lookup key, never storing or checking it | Step 3b-2a-3: `account_node_ids` stores every node id a sighting, a lookup, or a commit link reports for an account (5.1, 5.2, D38) |
| 104 | Source checks that accepted what they should refuse: a REST response without readable rate-limit headers, a GraphQL `errors` present but not a list (`{}`, `""`), a compare above 250 commits checked by count but not against the commits GraphQL returns, and an index page from a repository other than the one resolved | Step 3b-2a-3 (5.3 steps 2, 4, and 5, D38); the oldest `total_commits - 250` commits of such a compare stay checked by count alone, since no GraphQL read reaches them |
| 105 | Run records and setup that hid a failure: `code_version` without `-dirty` when `git status` failed, a close that ignored outcomes it could not write, a missing `[automation]` table emptying the automation lists, `foreign_keys` never read back, `alembic stamp` accepted, a wheel check comparing repository names only, a log file no record reaches, and no record of a run's `--repo` filter | Step 3b-2a-3 (5.1, 5.3 step 8, 5.5, D38) |
| 106 | Step 4b: compare `files` stop at GitHub's documented 300 per comparison with no total, so a capped list read as complete; the activity read had no paging or completeness rule; a history count mismatch had no outcome; a refused compare was never retried; the log's first event was not stored; a compare without `files` had no rule; a compare that always fails could not be accepted; and the events and history commits a run deleted were not counted | Step 4b (5.1, 5.5, D38) |
| 107 | Step 6: an App token may return null where the maintainer's token read a value; a stopped schedule turns nothing red; a missing `DATABASE_URL` builds from empty; an empty exit-code output compares equal to 0 in `if:`; and `snapshot` did not check that the job's own sync ran | Step 6 and 5.6 (D38), except the stopped schedule: no run starts, so this repository cannot see it; 5.6 states the consumer's staleness rule, which the dashboard's integration builds (section 9) |
| 108 | The data dictionary presented head-repository, milestone, and team names as GitHub's current values, though they are as of `hydrated_at` | Step 3b-2a-3: described as of `hydrated_at` (D38) |
| 109 | Edits that may leave `updatedAt` unchanged (body, base ref, review dismissal, issue label events) were accepted as missed | Step 5 measures at least 20 cases of each that no later change the index sees follows; a kind not shown to change `updatedAt` gets an index-side check or a periodic re-hydration in pull request 5b, before Step 6a (section 8, D38) |
| 110 | A GitHub-derived column a later migration adds to a table without `hydration_version` (accounts, compares), or a new table of such data, is never filled for existing rows | 5.1: such a migration adds a per-row read version, which the refresh or the compare reader reads, or, before Step 6b only, refuses while the file holds the rows it would leave unread (D38) |

## 5. Design

### 5.1 Schema

Entity tables (`repositories`, `github_users`, `pull_requests`, `pr_reviews`, `pr_comments`, `issues`,
`repository_activities`, `sync_runs`, `sync_failures`) have their own integer primary key; GitHub
identifiers there are unique columns, never primary keys. Child tables use the composite keys
listed. References to `repositories`, `pull_requests`, `issues`, and `repository_activities` use
`ON DELETE CASCADE`; references to `github_users` use `ON DELETE SET NULL`, except those in a
primary key (`pr_assignees.account_id`, `account_profiles.account_id`,
`account_node_ids.account_id`), which use `ON DELETE CASCADE`;
references to `sync_runs` take SQLite's default (no action).
Application connections enforce foreign keys. Migrations run with `foreign_keys=OFF`, because
alembic's SQLite batch mode drops and recreates tables and an enforced drop cascades into child rows.
The migration engine's connect listener sets it on the DBAPI connection, before any transaction
opens (the pragma has no effect inside one), and raises unless `PRAGMA foreign_keys` then returns 0;
it does both before it turns off the driver's own transaction handling, because putting the
connection into autocommit (`isolation_level = None`) commits a transaction already open on it
(section 7, "Isolation level"). It also refuses a connection already inside a transaction, and
it is registered ahead of SQLAlchemy's own connect handling, whose first-connect rollback would
otherwise hide an open transaction from both checks (section 7, "Connect listener order").
Nothing executes on the SQLAlchemy connection before `context.configure()`: a statement there makes
alembic treat the transaction as external and never commit (gap 70). An upgrade is one transaction:
the listener turns off the driver's own transaction handling, a `begin` listener emits `BEGIN
IMMEDIATE`, and
`context.configure()` gets `transactional_ddl=True`, so a failed revision leaves the file at the
revision the upgrade started from, with no revision of that upgrade applied. `PRAGMA
foreign_key_check` runs inside that transaction after the last revision and raises on any row,
before the commit. It runs before every command commits, one that runs no revision (`current`,
`check`, and a `stamp` that changes nothing) included; a `stamp` that changes the revision is
refused by an `on_version_apply` callback, which raises on alembic's `MigrationInfo.is_stamp`
inside the command's transaction, so nothing commits, and a `stamp` at head has no step, except
with `--purge`, which empties the version table first, so it has a step and is refused (gap 105):
alembic loads each revision module inside the transaction, so a module's top-level code can change
rows or the schema without a revision running (section 7, "Revision module code"), and a file that
already holds a violation fails every command that connects. A migration that batch-alters a table a
view reads drops those views first and recreates them afterwards from its own frozen SQL;
`legacy_alter_table` stays off, so a view left in place fails the migration (gap 71). Migrations
import only the standard library, alembic, and SQLAlchemy, never application code, and name every
index, CHECK constraint, and foreign key they create through `op.f()`: env.py passes
`Base.metadata` as `target_metadata`, and alembic applies its naming convention to every migration's
operations, old revisions included (on a fresh build, the unnamed foreign keys of `01421d8dfaeb`
and `a2b3c4d5e6f7` take the head convention's names, which is harmless only because Step 3 drops
those tables). The convention does not change after Step 3's migration.
`downgrade()` raises (5.4); offline `--sql` mode is refused, because these checks need a live
connection. `alembic/env.py` refuses to migrate the repository-root
`github_activity.db` while D26 freezes it (gap 73): its connect listener asks SQLite which file
it opened (`PRAGMA database_list`) and refuses the tracked file by any path, URI, symlink, or hard
link, and any file named `github_activity.db`, such as another checkout's copy. A path predicted
from the URL is not enough: the driver drops `..` as text, so a path through a symlinked directory
names another file once symlinks are followed. It migrates SQLite only, by a file path: SQLite URI
filenames are refused, because URI parameters (`nolock`, `immutable`, `vfs`) can remove the
locking an upgrade relies on. An SQLite authorizer on the migration connection allows only the
actions it lists, the main database's DDL and DML and the reads alembic and reflection make, so
`ATTACH`, temporary objects (`TEMP` or a `temp.` name), virtual tables, and `SAVEPOINT` are
refused. It allows `BEGIN` only
to open the upgrade's transaction, `COMMIT` only after `foreign_key_check` passed, and `ROLLBACK`
only once the upgrade or its commit failed. A pragma may set a value only for `user_version` and
the pragmas SQLAlchemy's reflection reads with an argument (`table_info`, `table_xinfo`, `index_list`, `index_info`, `foreign_key_list`), so
`journal_mode`, `legacy_alter_table`, and
`ignore_check_constraints` keep the values the connection opened with. It refuses every
statement while the upgrade is open and no transaction is (SQLite ends one by itself on
`INSERT OR ROLLBACK` or `RAISE(ROLLBACK)`). SQLite runs the authorizer when it prepares a statement, so the migration
connection keeps no prepared-statement cache, which would otherwise rerun a statement prepared
before that rollback without the check (section 7, "Prepared statements"). So no SQL statement a
revision runs commits part of an upgrade or changes how it runs; Python code in a revision can
still remove the authorizer, or feed one `executemany` statement rows while running others, which
no check here prevents.

`Base.metadata` names indexes, CHECK constraints, and foreign keys by convention, and every CHECK has
a name: batch recreate keeps named CHECKs and drops unnamed ones (gap 72). Migrations pass names
through `op.f()`, which keeps the convention from prefixing a given name twice. Unique constraints
stay out of the convention, which would otherwise report the kept `user_tags`' unnamed `UNIQUE` to
`alembic check`; new ones carry explicit `uq_<table>_<columns>` names. Columns are NOT NULL unless this section names them nullable, the source record's
value is optional, or the column has no value until a later write: `sync_runs.finished_at` and
`exit_code` (null while the run is `running`), and every `repo_sync_state` column
except `repository_id`, `last_run_id`, and `last_outcome` (`index_issue_total` stays null until
Step 4 indexes issues). The kept tag tables keep their current nullability (`user_tags.description`
and `color`, `pr_user_tags.created_at`).

| Table | Columns | Keys and constraints |
|-------|---------|----------------------|
| `repositories` | `github_id`, `node_id`, `owner`, `name`, `full_name`, `repo_group`, `is_private`, `is_archived`, `default_branch`, `labels_total`, `is_tracked`, `resolved_at`, and one column for each further field the resolve selects (5.8), named in snake case (`stargazerCount` as `stargazer_count`; an object's field as `primary_language`, `license_spdx_id`, `plan_features_codeowners`, `plan_features_team_review_requests`, and `plan_features_maximum_manual_review_requests`) | `github_id` unique; `node_id` unique; the GitHub fields as the latest complete resolve read them (5.8), nullable as GitHub's are, and `resolved_at` the GitHub `Date` header of that resolve's first response, written only with them; names take GitHub's spelling; `full_name` indexed, not unique (the consumer key (`full_name`, `number`) is unique among `is_tracked` rows); `is_tracked` set by 5.3 step 1 (D27); `repo_group` as `tracking.toml` assigned it at the last resolve |
| `repo_labels` | `repository_id`, `node_id`, `name`, `color`, `description`, `is_default`, `created_at`, `updated_at` | PK (`repository_id`, `node_id`); the repository's labels as the latest complete resolve read them (D8); `description`, `created_at`, and `updated_at` nullable as GitHub's are |
| `github_users` | `github_id`, `node_id`, `login`, `type`, `name`, `created_at`, `updated_at`, `linked_user_github_id`, `claimant_github_id`, `refreshed_at`, `not_found_at` | `github_id` unique, null only for type `EnterpriseUserAccount`, which has no `databaseId` (5.2); `node_id` unique; `type` and `login` not null, from GraphQL only (5.2); `type` in (`User`, `Bot`, `Organization`, `Mannequin`, `EnterpriseUserAccount`); `name`, `created_at`, `updated_at`, and `claimant_github_id` as GitHub reports them, null until the account's first lookup (`Bot` has no `name`); `linked_user_github_id` is an `EnterpriseUserAccount`'s `user.databaseId` and `claimant_github_id` a `Mannequin`'s `claimant.databaseId`, null for another type or when GitHub returns none; `refreshed_at` is the GitHub `Date` header of the response that inserted the row or of its latest lookup that returned the account (5.2); `not_found_at` (named `deleted_at` at `6fedb05`) is the `Date` header of the first lookup that returned `NOT_FOUND` since the account was last seen, null otherwise, and no reference reads it (D28); profile fields are in `account_profiles` (D31) |
| `pull_requests` | `repository_id`, `number`, `github_id`, `node_id`, `state`, `is_draft`, `title`, `body`, `author_id`, `author_reported`, `author_association`, `created_at`, `merged_at`, `closed_at`, `published_at`, `last_edited_at`, `editor_id`, `includes_created_edit`, `maintainer_can_modify`, `merged_by_id`, `merged_by_reported`, `base_ref`, `base_ref_oid`, `head_ref_name`, `head_ref_oid`, `head_repo_github_id`, `head_repo_full_name`, `head_repo_owner_node_id`, `head_repo_owner_login`, `merge_commit_oid`, `targets_default_branch`, `is_cross_repository`, `milestone_number`, `milestone_title`, `review_decision`, `mergeable`, `merge_state_status`, `can_be_rebased`, `potential_merge_commit_oid`, `stack_node_id`, `stack_number`, `stack_size`, `stack_base_ref_name`, `stack_entry_node_id`, `stack_position`, `additions`, `deletions`, `changed_files`, `total_comments_count`, `commits_total`, `files_total`, `reviews_total`, `comments_total`, `labels_total`, `closing_issues_total`, `user_linked_closing_issues_total`, `review_requests_total`, `assignees_total`, `github_updated_at`, `hydrated_at`, `hydration_version` | unique (`repository_id`, `number`); `github_id` unique; `state` in (`OPEN`, `CLOSED`, `MERGED`), GitHub's `PullRequestState`; `merged_at` set iff `MERGED`; author, merger, and editor nullable; `closed_at`, `published_at`, `last_edited_at`, `merge_commit_oid`, the head repository and its owner, the milestone, and `review_decision` nullable as GitHub's are (the head repository is null when GitHub returns none, as for a deleted fork); the milestone is GitHub's reference to it (number and title), not a stored milestone; the merge state and stack columns are nullable as GitHub's are, `stack_node_id`, `stack_number`, `stack_size`, and `stack_base_ref_name` null when GitHub returns no stack, and `stack_entry_node_id` and `stack_position` when it returns no stack entry; a stack is identified within its repository by `stack_number`, and `stack_position` is GitHub's position, 1 closest to the base branch |
| `pr_labels` | `pr_id`, `name`, `label_node_id` | PK (`pr_id`, `name`) |
| `pr_reviews` | `github_id`, `node_id`, `pr_id`, `reviewer_id`, `reviewer_reported`, `state`, `created_at`, `submitted_at`, `published_at`, `updated_at`, `last_edited_at`, `editor_id`, `includes_created_edit`, `commit_oid`, `author_association`, `author_can_push_to_repository`, `inline_comments_total` | `github_id` unique; reviewer and editor nullable; `state` in (`APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`); `submitted_at`, `published_at`, `last_edited_at`, and `commit_oid` nullable as GitHub's are; `inline_comments_total` is GitHub's `comments.totalCount` for the review (D5); no review text (D24) |
| `pr_comments` | `github_id`, `node_id`, `pr_id`, `author_id`, `author_reported`, `created_at`, `published_at`, `updated_at`, `last_edited_at`, `editor_id`, `created_via_email`, `includes_created_edit`, `author_association` | `github_id` unique; author and editor nullable; `published_at` and `last_edited_at` nullable as GitHub's are; no comment text (D24, D28) |
| `pr_commits` | `pr_id`, `sha`, `author_id`, `linked_github_id`, `author_name`, `author_email`, `authored_at` | PK (`pr_id`, `sha`); author nullable; `linked_github_id` is the account id GitHub linked the commit to in the latest hydration (GraphQL `GitActor.user.databaseId`, REST compare `author.id`), null when it linked none (D28); no message (D19) |
| `pr_files` | `pr_id`, `ordinal`, `path`, `change_type`, `additions`, `deletions` | PK (`pr_id`, `ordinal`); `ordinal` is the position in GitHub's file list, from 0 |
| `pr_closing_issues` | `pr_id`, `issue_repo_github_id`, `issue_repo_full_name`, `issue_number`, `issue_github_id`, `user_linked` | PK (`pr_id`, `issue_repo_github_id`, `issue_number`); `issue_github_id` is the issue's `fullDatabaseId`; `user_linked` is whether GitHub also lists the link under `userLinkedOnly: true`, which GitHub describes as "Return only manually linked Issues"; `issue_repo_full_name` is the name GitHub reported when the link was last written |
| `pr_review_requests` | `pr_id`, `ordinal`, `request_github_id`, `request_node_id`, `requested_type`, `requested_github_id`, `requested_node_id`, `account_id`, `team_slug`, `as_code_owner` | PK (`pr_id`, `ordinal`); one row per entry of GitHub's `reviewRequests` (D4), `ordinal` its position from 0, `request_github_id` and `request_node_id` being the request's own `databaseId` (nullable as GitHub's is) and `id`; `requested_type` is GitHub's `__typename` (`User`, `Bot`, `Mannequin`, `Team`, or `EnterpriseTeam`, which GitHub's `RequestedReviewer` union also holds and which has `fullDatabaseId` in place of `databaseId`), null with the other requested columns when GitHub returns no requested reviewer; `account_id` references `github_users` for an account and is null for a team or an enterprise team, which is not an account and is identified by `requested_github_id` (an enterprise team's `fullDatabaseId`) and `team_slug` |
| `pr_assignees` | `pr_id`, `account_id` | PK (`pr_id`, `account_id`); one row per actor GitHub lists in `assignedActors` (D14): a `User`, `Bot`, `Mannequin`, or `Organization` |
| `issues` | `repository_id`, `number`, `github_id`, `node_id`, `state`, `state_reason`, `title`, `body`, `author_id`, `created_at`, `closed_at`, `labels_total`, `github_updated_at`, `hydrated_at`, `hydration_version` | unique (`repository_id`, `number`); `github_id` unique; `state` in (`OPEN`, `CLOSED`); author nullable |
| `issue_labels` | `issue_id`, `name` | PK (`issue_id`, `name`) |
| (Step 4a) | | The two issue rows above are the Step 3 plan. Step 4a completes them under D29 the way Step 3b-1 completes the PR rows: `issues.author_reported` (D28), the label id on `issue_labels` (D8), the issue's other free fields (`author_association`, `last_edited_at`, `published_at`, milestone, issue type, the parent and the `duplicateOf` issue by repository id and number), `issue_comments` and `issue_assignees` tables shaped like `pr_comments` and `pr_assignees` with their totals, and their 5.4 child-count checks |
| `repository_activities` | `repository_id`, `github_id`, `node_id`, `activity_type`, `ref`, `before_sha`, `after_sha`, `occurred_at`, `actor_github_id`, `actor_id` | Step 4b (D30); `github_id` unique (GitHub's event `id`); `activity_type` is GitHub's value as text (`push`, `force_push`, `branch_creation`, `branch_deletion`, `pr_merge`, `merge_queue_merge` in githubkit 0.14.2's model); `occurred_at` is GitHub's `timestamp`; `actor_github_id` is the REST actor's `id` and `actor_id` its `github_users` row once a lookup resolves it (5.2), both null when GitHub returns no actor |
| `repository_activity_compares` | `activity_id`, `http_status`, `status`, `ahead_by`, `behind_by`, `total_commits`, `merge_base_sha`, `files_at_cap`, `error` | Step 4b; PK `activity_id`; `files_at_cap` is true when compare's first page, the only page GitHub lists files on, returned 300 files, the cap GitHub documents for one comparison, so its file rows may be partial (gap 106); `error` is the class and message of a compare that still failed after its retries, whose 2xx first page had no `files` list, whose fetched commits differ from `total_commits`, or whose later page answered 404 or 422 (Step 4b), with the other columns null, and null once GitHub answers it; GitHub's REST compare of `before_sha...after_sha` as it returns them, so the files and counts run from the merge base, not from `before_sha`, when the push diverged (section 7, "Compare"); no row when either SHA is all zeros (a branch creation or deletion); `http_status` records the response, so a refused compare is a row with the other columns null (a compare across a force push, `5110f3d...fccb1f5`, returned 200, section 7) |
| `repository_activity_compare_files` | `activity_id`, `ordinal`, `filename`, `previous_filename`, `status`, `additions`, `deletions` | Step 4b; PK (`activity_id`, `ordinal`); compare's `files` entries as returned |
| `repository_activity_compare_commits` | `activity_id`, `ordinal`, `sha`, `author_id`, `linked_github_id`, `author_name`, `author_email`, `authored_at` | Step 4b; PK (`activity_id`, `ordinal`); compare's `commits` list in full, each commit stored like a `pr_commits` row (D19), so a commit an event brought joins to its event; written once with its compare and re-read only when a read version asks for it (5.1, column rules), so its author link is the one GitHub returned then |
| `default_branch_commits` | `repository_id`, `sha`, `author_id`, `linked_github_id`, `author_name`, `author_email`, `authored_at`, `committed_at`, `parent_count`, `additions`, `deletions`, `changed_files` | Step 4b; PK (`repository_id`, `sha`); the default branch's history since the floor as GitHub lists it (D30); the author link as in `pr_commits`, a replace keeping the stored link of the row with the same SHA when GitHub returns none (D28); `parent_count` is GitHub's `parents.totalCount`; `changed_files` is GitHub's `changedFilesIfAvailable`, null when GitHub returns none; no message (D19) |
| `automation_accounts` | `github_id`, `login`, `reason`, `run_id` | integer primary key; `github_id` unique; replaced from `tracking.toml` at the start of every run, `run_id` naming that run (a reference to `sync_runs`) |
| `account_profiles` | `account_id`, `company`, `location`, `website_url`, and an organization's `description`, `email`, `twitter_username`, `is_verified`, `archived_at`, `has_sponsors_listing`, and `pinned_items_remaining`, `refreshed_at` | PK `account_id`, a reference to `github_users` (`ON DELETE CASCADE`); rows only for a `User` or an `Organization` (D31); each lookup that returns the account replaces the whole row with the profile it returned, through the writer and in the transaction that writes `github_users`' login and type, so no earlier value survives a field GitHub now returns as null or a change of type; a lookup that returns `NOT_FOUND` leaves the row unchanged, marked by `github_users.not_found_at` (D28); every field nullable as GitHub's is, the organization fields null for a user and `company` null for an organization; `refreshed_at` as on `github_users` |
| `account_node_ids` | `account_id`, `node_id`, `first_run_id`, `last_run_id` | Step 3b-2a-3; PK (`account_id`, `node_id`); `node_id` unique; every node id GitHub reported for the account in an actor sighting, a lookup, or a commit link, the one `github_users.node_id` holds included, with the first and latest runs that reported it (`first_run_id` and `last_run_id`, references to `sync_runs`), so GitHub's second node id for one `databaseId` is stored, not dropped (5.2, gap 103); its migration refuses a file whose `github_users` holds rows, since no file was published before Step 6b (gap 110); consumer |
| `automation_emails` | `email`, `reason`, `run_id` | integer primary key; `email` unique, stored lowercase ASCII (SQLite's `lower()`, which the views apply to commit emails, folds only ASCII letters); replaced the same way |
| `sync_runs` | `started_at`, `deadline_at`, `finished_at`, `status`, `exit_code`, `trigger`, `code_version`, `config_source`, `config_sha256`, `graphql_points`, `rest_calls`, `items_planned`, `items_hydrated`, `items_deleted`, `failures`, `refresh_failures`, `link_lookups`, `links_not_found`, `repo_filter`, `entries`, `deletions_approved`, and from Step 4b `events_deleted` and `history_commits_deleted` | `status` in (`running`, `complete`, `time_limited`, `failed`, `aborted`), set as 5.3 step 8 says; `deadline_at` is `started_at` plus `--max-minutes`, null without it; `trigger` is `schedule` or `workflow_dispatch` from `GITHUB_EVENT_NAME`, `manual` when it is unset, and `unknown` for any other value (gap 105); `code_version` is `GITHUB_SHA`, else the commit of the checkout the package runs from (with `-dirty` appended when `git status` lists changes to tracked files, untracked files ignored, and `-unknown` when it fails or times out, gap 105; the package runs from a checkout only when its directory or a parent holds a `.git` entry, and then `git ls-files --error-unmatch` exiting 1 means the package is not tracked, while any other failure of a git call, exit 128 and a missing `git` included, records the package version, or the commit when `rev-parse` answered, followed by `-unknown`; the package runs from a checkout when git tracks its `__init__.py`), else the package version; `config_source` is `packaged` or the `TRACKING_CONFIG_PATH` path made absolute (symlinks not followed), and `config_sha256` the digest of the `tracking.toml` bytes the run read (gap 88); `items_planned` counts the distinct items planned for hydration (in-place updates excluded), `items_hydrated` the hydrated items written, and `items_deleted` the item rows step 7 and the prune deleted, and from Step 4b `events_deleted` and `history_commits_deleted` the activity events and default-branch commits Step 4b deleted (nullable, null for a run before Step 4b, each written in its deletion's transaction); `failures` counts this run's item failures, accepted ones included, and `refresh_failures` the accounts whose refresh failed this run (5.3 step 4), `link_lookups` the commit-link accounts this run looked up that no earlier lookup answered `NOT_FOUND` (neither an account stored with `not_found_at`, so none of gap 90's lookups, nor one a stored commit row links by `linked_github_id` without an account), and `links_not_found` those of them a lookup answered `NOT_FOUND` (5.2), `repo_filter` a JSON array of the `--repo` values as given, sorted, null for a run without `--repo`, and `entries` a JSON array, in `tracking.toml` order, of each configured entry's name and the `github_id` of the stored row 5.3 step 1 matched it to, else the `github_id` it resolved to, null when it resolved to none, written when the run closes; `refresh_failures`, like `failures`, counts accepted ones; the migration that adds these five refuses a file whose `sync_runs` holds rows (gap 110); `deletions_approved` is null or JSON with `allow_deletions` (the repository, the approved N, and for each guarded set, step 7's items and from Step 4b the activity events and the default-branch commits, its candidate count and whether its deletion ran) and `prune_untracked` (each pruned repository's `github_id`, `full_name`, and items deleted), written as `{"allow_deletions": {"repository", "github_id", "approved", "sets": {"items": {"candidates", "deleted"}}}, "prune_untracked": [{"github_id", "full_name", "items_deleted"}]}` with sorted keys, in the transaction of the deletion it records, as `items_deleted` is; every column is consumer except the run counters (5.1, consumer interface) |
| `repo_sync_state` | `repository_id`, `last_index_started_at`, `index_pr_total`, `index_issue_total`, `last_success_at`, `last_run_id`, `last_outcome`, `halt_reason`, and from Step 4b `activity_read_at`, `activity_first_event_at`, `activity_last_outcome`, `activity_halt_reason`, `history_read_at`, and `default_branch_commits_total` | PK `repository_id`; `activity_first_event_at` is the `timestamp` of the oldest event the latest complete activity read listed, so a consumer can tell a quiet period from a log that does not reach back (gap 106); `activity_read_at` is the GitHub `Date` header of the first page of the latest activity-log read that completed and was written; `history_read_at` and `default_branch_commits_total` are the first page's `Date` header and the `totalCount` of the latest history read that replaced the rows, written only with that replace; `activity_last_outcome` in (`complete`, `time_limited`, `failed`, `halted`); `activity_halt_reason` holds a Step 4b deletion halt's reason and candidate counts and is cleared by a run whose Step 4b did not halt (Step 4b); `last_run_id` is the latest run that attempted the repository, or found it untracked, and `last_outcome` its result, in (`complete`, `time_limited`, `failed`, `incomplete_index`, `halted`, `untracked`), both written by step 8, or by the close of a fatal or interrupted run (5.3 step 8); `last_index_started_at` is written only when that index completed and the repository did not fail later in the run, a time-limited repository included (its unreached items are in `sync_pending`), and never by the close of a fatal or interrupted run; `last_success_at` is the run's `finished_at` when `last_outcome` is `complete` and no item of the repository failed outside `[sync].accepted_failures`; `halt_reason` holds step 7's halt and is cleared by a run whose step 7 ran for the repository and did not halt, a run whose step 7 did not run for it leaving it |
| `sync_pending` | `repository_id`, `item_kind`, `number`, `run_id` | PK (`repository_id`, `item_kind`, `number`); `item_kind` in (`pull_request`, `issue`); items a time-limited run planned but did not reach; step 8 replaces the rows of each completely indexed repository that did not fail later in the run (a repository gone during hydration keeps its rows, 5.3 step 4) with this run's unreached items, so a run that reached them all leaves none; consumer, so an in-coverage item not yet hydrated is told apart from one outside coverage |
| `sync_failures` | `repository_id`, `item_kind`, `number`, `attempts`, `last_error`, `first_failed_at`, `last_failed_at`, `last_run_id` | unique (`repository_id`, `item_kind`, `number`); `item_kind` in (`pull_request`, `issue`); `attempts` counts the runs in which the item failed: `last_run_id` is the latest such run, and a failure adds one only when its run is another, whatever the runs' clocks read; `last_error` is the error's class and message; row deleted on success, or by step 7 when its repository's in-coverage set no longer lists the item |
| `user_tags`, `pr_user_tags` | unchanged except `created_at`, which uses the 5.1 timestamp type with a `datetime.now(UTC)` default | FK to new `pull_requests` |

Column rules:

- `*_total` columns hold the validated `totalCount` of the matching connection; `commits_total`
  equals the compare `total_commits` above 250 commits. `changed_files` is GitHub's figure, which
  counts a path that appears twice in the file list once; it is never compared with file rows.
  `total_comments_count` is GitHub's `totalCommentsCount`, stored as GitHub reports it.
- `author_reported`, `merged_by_reported`, and `reviewer_reported` are true when GitHub returned a
  non-null actor for that reference in the row's latest hydration, so a reference D28 kept is told
  apart from one GitHub returns today; `pr_commits.linked_github_id` does the same for commits.
  The other actor references (`editor_id`, requests, event actors) take GitHub's current value,
  null included, and `pr_assignees` is GitHub's current `assignedActors` list (D28).
- The merge state (`mergeable`, `merge_state_status`, `can_be_rebased`,
  `potential_merge_commit_oid`) is GitHub's when the PR was hydrated, so an open PR's stored merge
  state is as of its latest hydration (`hydrated_at`), which can be older than the latest run.
  GitHub recomputes it as the base branch moves and checks finish; whether that changes the PR's
  `updatedAt` is not measured (section 8). Re-hydrating every open PR each run is not done (D32):
  by the formula it costs about 60 points per run (30 per-repository batches of 2 points for the
  463 open PRs of the 28 tracked repositories other than `prebid-agent-skills`, 2026-10-18, before
  overflow); the decision was taken on an earlier estimate of 36 points that pooled PRs across
  repositories. GitHub says `potentialMergeCommit` "will not return a value if the pull request is
  merged, or if the test merge commit is still being generated". Auto-merge requests and merge
  queues are not collected (D32; section 7, "Merge state and stacks").
- The `author_association` of reviews and comments, a review's `author_can_push_to_repository`,
  and `review_decision` are GitHub's values when the PR was hydrated (`hydrated_at`); GitHub
  computes them when read.
- `targets_default_branch` is `base_ref = repositories.default_branch` for the repository's current
  default branch. It is written with each PR and recomputed for the repository's PRs when step 1 of
  5.3 sees the default branch change.
- `author_association` is GitHub's current value, which GitHub computes when read; the index keeps
  it current (5.3 step 3).
- `hydration_version` is per kind (PR, issue) and is compared by inequality, so reverting code
  re-hydrates what the reverted code wrote. A change to a kind's query documents (its alias fields
  and overflow continuations) or a migration that adds or redefines a GitHub-derived column of a
  table that may hold rows bumps that kind's version in the same pull request. Step 3b-1 changes
  the pull request documents, so the pull-request version becomes 2. No sync has written the new
  schema before 3b-2a-2, so Step 3b-1's migration refuses a database holding rows in any table it
  adds a column to (the hydrated tables, `repositories`, `github_users`, `sync_runs`, and the
  automation tables), as Step 3a's refuses tag rows, and its added columns need no value for existing rows. A
  column a later migration adds to tables that hold rows is null on rows hydrated at an older
  version until they re-hydrate, and `hydration_version` is a consumer column, so a reader can tell.
  A table without `hydration_version` (`github_users`, `account_profiles`, the Step 4b compare
  tables) to which a later migration adds a GitHub-derived column gains a per-row read version:
  the refresh (5.2) re-reads an account whose version differs from the code's, and Step 4b's
  compare reader re-requests a compare whose version differs, keeping the stored row and
  recording the refusal when GitHub refuses it. Before Step 6b the migration may instead refuse
  while the table holds rows; from Step 6b every published file holds such rows, so a refusal
  would stop every scheduled run until a bootstrap, and only the read version is allowed. So a
  column is never left unread on old rows. A migration that adds a table of GitHub-derived data
  refuses only while the file holds rows no later run reads for it, so no row reads as "none" where
  nothing was recorded; a table or column every run fills for every stored parent, whose unread
  state is already marked, does not refuse (Step 4a's issue tables, Step 4b's activity, compare,
  and history tables and `repo_sync_state` columns, unread while `activity_last_outcome` or
  `history_read_at` is null). A run counter added after Step 6b is nullable, null meaning not
  recorded, since a refusal there would force a bootstrap (gap 110).
- `state_reason` is GitHub's value including `DUPLICATE` (5.8). Descriptive enums, every GitHub
  enum column except a PR's, issue's, and review's `state` (`github_users.type` keeps its CHECK),
  such as `change_type`, `author_association`, `state_reason`, `merge_state_status`, and
  `activity_type`, are text without a CHECK, so a new GitHub value is stored, not rejected.

Indexes: foreign-key columns of every child table and of `pr_reviews` and `pr_comments`, with
`pr_reviews (pr_id, reviewer_id, github_id)` and `pr_comments (pr_id, author_id)` in place of
their `pr_id` indexes: the Step 3a views looked up one account's reviews and comments on one PR,
and with single-column indexes the planner read every row of the account for each PR (section 7,
"View lookups"); `v_pr_comments.author_also_reviewed` still looks up one account's reviews on one
PR; `pull_requests (repository_id, state)`,
`(repository_id, merged_at)`, `(author_id)`, `(merged_by_id)`, `(merge_commit_oid)`;
`issues (repository_id, state)`, `(author_id)`; `pr_closing_issues (issue_repo_github_id,
issue_number)`; `repository_activities (repository_id, occurred_at)` and `(after_sha)`, for the D30
join with `merge_commit_oid`; and every foreign-key column Steps 3b-1, 3b-2a-3, and 4b add (`editor_id`,
`pr_review_requests.account_id`, `pr_assignees.account_id`,
`repository_activities.actor_id`, the commit tables' `author_id`, the automation tables'
`run_id`, and `account_node_ids`' `first_run_id` and `last_run_id`), and `repository_activity_compare_commits (sha)` for D30's commit-to-event join. A test checks that every
foreign-key column leads an index (a composite primary key counts), except
`pr_user_tags.user_tag_id`, which this section keeps unchanged.

Views join stored rows with their consumer keys, one view row per stored row: no view leaves a row
out, aggregates rows, or computes a column outside D29's list. Where a reference is null, the
account columns are null and the row stays. Every PR-level view carries the PR keys: the
repository's `github_id`, `full_name`, and `is_tracked`, and the PR's `number` and `github_id`.
Every account in a view carries its `github_id`, `node_id` (the key of an account with a null `github_id`), `login`,
`type`, and `automation_reason` (the reason of the `automation_accounts` entry for its `github_id`,
null when none lists it), side by side, never merged into one flag. Step 3b-1 replaces `v_pr_reviewers` and `v_pr_commenters`, which left
rows out and derived a verdict (gaps 81 and 82), with `v_pr_reviews`, `v_pr_comments`, and
`v_pr_relationships`, and bumps `schema_version` to 2:

- `v_github_users`: every `github_users` column except `id`, plus `automation_reason`.
- `v_account_profiles`: one row per `account_profiles` row, with the account's `github_id`,
  `node_id`, `login`, `type`, and `automation_reason` and every profile column except
  `account_id`.
- `v_pr_reviews`: one row per `pr_reviews` row, with the PR keys, every review column except the
  row ids, the reviewer and the editor as accounts, and `is_pr_author` (the reviewer is the PR's
  author; null when either reference is null).
- `v_pr_comments`: one row per `pr_comments` row, with the PR keys, every comment column except the
  row ids, the author and the editor as accounts, `is_pr_author`, and
  `author_also_reviewed` (the author has a `pr_reviews` row on the same PR; null when the author is
  null).
- `v_commit_authors`: one row per `pr_commits` row, with the PR keys, every `pr_commits` column
  except `pr_id` and `author_id`, the linked account, and `email_automation_reason` (the reason of
  the `automation_emails` entry equal to `lower(author_email)`, null when none).
- `v_pr_relationships`: one row per relationship of a PR to an account, a team, or an actor GitHub
  did not return, from the stored rows,
  with a `relationship` column naming its source: `author` (one per PR, also when the reference is
  null), `merged_by` (one per merged PR, also when GitHub returned no merger), `review` (one per
  review), `comment` (one per comment), `review_request` (one per request), `assignee` (one per
  assignee), `commit_author` (one per commit), and `pr_editor`, `review_editor`, and
  `comment_editor` (one per PR, review, or comment that GitHub lists an editor or a `last_edited_at`
  for, also when it returned no editor). Each row carries the PR keys, its source row's key (review or comment `github_id`,
  request `request_node_id` with `request_github_id` beside it and `ordinal` as GitHub's position,
  commit `sha`), the source row's `*_reported`
  value where it has one, the account with its `not_found_at`, the review's `state` and
  `submitted_at` on a review row, `requested_type`, `requested_github_id`, `team_slug`, and
  `as_code_owner` on a request row, `linked_github_id`, `author_name`, `author_email`, and
  `email_automation_reason` on a commit row (null on other rows), and the PR's `state`,
  `created_at`, `merged_at`, `closed_at`, `base_ref`, `targets_default_branch`, `merge_commit_oid`,
  `additions`, `deletions`, and `changed_files`.

Each view lists its columns explicitly and identifies rows only by consumer keys (repository
`github_id` and `full_name`, PR `number` and `github_id`, review and comment `github_id`, review
request `request_node_id`, account
`github_id`, or `node_id` when `github_id` is null, and `login`, commit `sha`), never by row ids. The views compare `lower(author_email)` with
`automation_emails`.

Reviews and comments by accounts the database never stored stay in `pr_reviews` and `pr_comments`
with a null author, and the views keep them as rows with null account columns (D24, D28). Each migration that creates or replaces a view embeds a
frozen copy of its SQL as a literal module constant, never a shared mutable constant, so an old
migration keeps producing its own revision's view. Views are not declared in `Base.metadata` (a
metadata table for a view makes `alembic check` report `add_table`). `alembic check` compares
neither views nor CHECK constraints, so a test compares a database migrated to head with the head
models built by `metadata.create_all`, structurally (`PRAGMA table_xinfo`, `index_xinfo`, and
`foreign_key_list`, and CHECK constraints by name and exact text), and each view
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
excluded, each marked consumer or internal. Every GitHub-derived column of a table that mirrors
GitHub is consumer. The columns that say how current a row is are consumer
(gap 86): `github_updated_at`, `hydrated_at`, and `hydration_version` on items,
`repositories.resolved_at`, `github_users.refreshed_at`, `account_profiles.refreshed_at`, every
`repo_sync_state` column (`last_index_started_at` is the as-of
time of everything the index wrote), `sync_failures`' repository, kind, number, error, and failure
times, `sync_pending` (an in-coverage item a time-limited run did not reach), and every
`sync_runs` column except the run counters (`graphql_points`, `rest_calls`, `items_*`,
`failures`, `refresh_failures`, `link_lookups`, `links_not_found`, and from Step 4b `events_deleted`
and `history_commits_deleted`), which with the tag tables are internal. Integer
row ids change on a rebuild, but a pull request's labels, reviews, comments, commits, files, and
closing links, and its author and merger, are reachable only through them, so the foreign-key
columns of consumer tables and the row ids they reference are consumer, valid within one file; other
row ids are internal. Consumers never store a row id or compare one across files, where a row is
identified by its `github_id` (an account with a null `github_id` by its `node_id`), and a child row
without one (a label, commit, file, or closing link) by its pull request's `github_id` and its own
key; (`full_name`, `number`) identifies a pull request among tracked repositories within one file
and changes with a rename. Views carry no row ids. `PRAGMA user_version` holds `schema_version`, set by
migrations (1 from Step 3a, 2 from Step 3b-1) and bumped when an interface column is removed,
renamed, or changes meaning, or a consumer view's frozen SQL changes, which covers a change to its
rows or derivation (gap 87); additive changes (a new table column, table, or view) do not bump it,
while a new column on an existing view changes its frozen SQL and bumps it. A lock file maps each
`schema_version` to its consumer (table or view, column) pairs and, from Step 3b-1, to the SHA-256
of each consumer view's frozen SQL, its exact text: whitespace inside a literal or at the end of a
comment can change a view's rows, so nothing is normalized, and a change of layout alone also
needs a new version. A test fails when the current version's entry differs from the consumer pairs
the data dictionary marks at head (a listed pair missing at head, or a consumer pair at head not
listed) or from the frozen SQL of the views at head, and on pull requests the CI lock step (Step 2)
also fails when a pull request deletes an entry, or removes a pair or changes a view digest in one,
that exists at the base commit; an additive change adds its consumer pairs and new views to the
current entry. A change of meaning in a column is left to review. The manifest (5.6) carries the
same number.

### 5.2 Identity and automation

- Users are keyed by GitHub `databaseId` (REST `id`). Item ids (PRs, reviews, issues) come from
  `fullDatabaseId`, a BigInt that GraphQL returns as a JSON string; `PullRequest.databaseId` and
  `PullRequestReview.databaseId` are deprecated because they do not support 64-bit ids. Actor ids come
  from `databaseId`, which `User`, `Bot`, `Organization`, and `Mannequin` do not deprecate.
- GraphQL `Actor` types with a `databaseId`: `User`, `Bot`, `Organization`, `Mannequin`. A null actor
  (GitHub returned none, and does not say why; D28) stores a null reference, unless D28 keeps the
  reference (a PR's author or merger, an issue's author, a review's reviewer, a comment's author, a
  commit's link) and the object already has one stored. The fifth type, `EnterpriseUserAccount`,
  has no `databaseId`: it is stored with a null `github_id`, keyed by its node `id`, with its
  `user.databaseId` in `linked_user_github_id`, and never substituted by that user (gap 85). It
  cannot be listed in `[automation].accounts`, which keys by `github_id`.
- One account comes back in more than one shape. dependabot (49699333) is `Bot`/`dependabot` as a PR
  author and `dependabot[bot]` as a commit's `GitActor.user`, whose declared type is `User`; REST uses
  `dependabot[bot]`. `copilot-pull-request-reviewer[bot]` and `Copilot` share id 175728472. Identity
  never joins on login. A sighting or lookup whose `databaseId` is stored under another node id
  refers to the stored row, which keeps its node id; the run reports the second one and stores it in
  `account_node_ids`, which holds every node id GitHub reported for the account (gap 103). One whose
  node id is stored under another `databaseId` fails the item, or the lookup, as does an item whose
  sightings and lookups report one node under two `databaseId` values, a node id in
  `account_node_ids` included. A commit link resolves by its `databaseId`; the node id it carries
  (GraphQL `GitActor.user.id`, REST compare `author.node_id`) is stored in `account_node_ids` and
  checked as a sighting's is (gap 103). A commit-link lookup that answers `NOT_FOUND` is counted in
  `sync_runs.links_not_found`, beside `link_lookups`, when no earlier lookup answered `NOT_FOUND`
  for it, and listed in the report (D28 stores the commit as before); from Step 6a, a run whose
  `links_not_found` exceeds the share of its `link_lookups` Step 5 sets, with at least the minimum
  number of lookups Step 5 sets, exits 2 (5.3 step 8), since a lookup form GitHub stopped
  resolving looks the same per link.
- Type and login come only from GraphQL: actor sightings (PR author, merger, review author, comment
  author, editor, requested reviewer, assignee), which carry `__typename`,
  and `nodes(ids:)` lookups. A commit-linked account (GraphQL `GitActor.user` or the REST compare
  `author`) and an activity event's REST actor never set them. An item whose commit-linked account
  is not yet stored is written only after `nodes(ids:)` (100 ids per request) resolves that account
  in the same run; a failed lookup is an item error, and a `NOT_FOUND` result for an account with no
  stored values stores the commit without a linked account, `linked_github_id` keeping the id GitHub
  linked (D28). Lookups run after each hydration batch, for the commit-linked accounts that are
  neither stored, nor written or looked up earlier in the run, nor sighted in the same batch; each
  record reaches the writer with the accounts its links need (a lookup's answer, or an actor
  sighting in the record or in another record of its batch), so its own transaction inserts any
  account row it needs and checks every node the item reports (above). The writer refuses, writing
  nothing, a link to an account it has neither stored nor been given. An item whose lookup failed
  gets one more lookup holding only its own ids, unless the failed request held only those (5.3
  step 4). A looked-up account whose `databaseId` differs from the commit link's fails the items
  waiting on it (gap 78). Each run also refreshes users last refreshed more than 24 hours ago and
  without `not_found_at`, so renamed logins converge, and looks up every stored account with
  `not_found_at` set that a hydrated commit links to (gap 90), as part of the refresh and under its
  rules (5.3 step 4). A lookup that returns `NOT_FOUND` for a stored account keeps its row, its last
  known values, its `account_profiles` row, and every reference to it, and sets `not_found_at`; an
  account seen again, in an actor sighting or a lookup, has `not_found_at` cleared (D28).
- Only lookups change a stored account's login and type, and they also set `name`, `created_at`,
  `updated_at`, a mannequin's `claimant_github_id`, an `EnterpriseUserAccount`'s
  `linked_user_github_id`, and a `User`'s or `Organization`'s `account_profiles` row (D31),
  which the 24-hour refresh keeps current. An actor sighting inserts an account not
  yet stored and clears `not_found_at`, but leaves login and type to the refresh, so an account seen
  in two shapes (175728472) does not change login with the order items are written. `refreshed_at`
  is the GitHub `Date` header of the response that inserted the row or of its latest lookup that
  returned the account, the same clock as `hydrated_at`; a `NOT_FOUND` lookup sets `not_found_at`
  from its own `Date` header and leaves `refreshed_at`. The refresh runs after the last hydration
  write and before Step 4b's readers and step 7, oldest `refreshed_at` first, and stops at the deadline; in it, a
  `databaseId` that differs from the stored `github_id` is a failed refresh, which makes the exit 2
  unless an account entry in `[sync].accepted_failures` accepts it (5.3 step 4). Accounts that no
  stored row references any more stay stored and refreshed: within one file, `github_users` holds
  every account it ever stored (a bootstrap starts without them, section 9).
- Automation lists: `[automation].accounts` and `[automation].emails` name accounts and commit
  emails the maintainer lists, each with a reason. Each run copies both lists into
  `automation_accounts` and `automation_emails` with its run id, and the views show the matching
  entry's reason beside GitHub's account type (5.1); nothing is filtered by them, and nothing merges
  the two into one flag (D29). A config change shows on the next run without re-hydration, and
  `sync_runs.config_sha256` names the file the lists came from (gap 88).

### 5.3 Sync run

A run starts by claiming `sync_runs`. On a dedicated connection it issues `BEGIN IMMEDIATE`
explicitly (SQLAlchemy's default SQLite transaction handling emits no `BEGIN` before a `SELECT`), and
in that transaction refuses to start while another row is `running` with a null `deadline_at` or one
less than 10 minutes in the past, inserts its own row, and replaces `automation_accounts` and
`automation_emails` from `tracking.toml`. `--break-lock` marks a blocking row `aborted` first. In
the same transaction a `running` row whose `deadline_at` is 10 or more minutes past is marked
`aborted`. A refused claim inserts nothing and exits 1, naming the blocking run; `--break-lock` with
no blocking row proceeds and says so. A row another process marked `aborted` (`--break-lock`, or a
stale reclaim) keeps a null `exit_code`, and every close updates its row only while it is `running`,
so a run whose row was broken exits 1 and says so instead of overwriting `aborted`. An interrupted
run closes its row as `aborted` with `exit_code` 130, the code the CLI exits with on an interrupt,
which D25 does not publish on, and then re-raises the interrupt, whatever the close found. Every
write is its own transaction, so a run can stop at any point and the next run starts from what is
committed. Whatever ends a run without its close leaves its row `running` for the stale-row rule or
`--break-lock` to recover: a crash, SIGKILL, SIGTERM (Python installs no handler for it), a second
interrupt, a close that fails (the run exits 1), or a cancel inside a writer transaction that kept
the write lock (gap 93). With a deferred `BEGIN`, a claim racing an uncommitted one does not refuse:
it reads no `running` row and its insert fails with "database is locked" (section 7, "Run claim"). A
claimant that waits on another's uncommitted `BEGIN IMMEDIATE` past the 30 s busy timeout exits 1
with SQLite's "database is locked", without the blocking run's id, which it cannot read. Before the
claim (for `--dry-run`, before step 1), the run checks that the database file exists (connecting to
a missing path would create an empty file), that it is at the code's migration head, and that
`user_version` equals `schema_version`, reading `alembic_version` and `PRAGMA user_version` through
the application engine, never through alembic, whose commands take the write lock; it exits 1
otherwise (gap 74).

The engine (Step 3b-2a-2b-2) is the function `ghactivity sync` calls: a run that never starts raises
before any `sync_runs` row (step 8), and a run that starts returns its exit code and report, except
an interrupt, which is re-raised.

1. Resolve each configured repository, in `tracking.toml` order, with the resolve query (5.8). `followRenames` is on by
   default, so a renamed repository resolves under its configured name. Match the stored row by
   `github_id` and update owner, name, full name, default branch, `node_id`, and every other
   repository field the resolve reads (5.8), and replace its `repo_labels` rows with the labels the
   resolve read, after checking their count against `labels_total` and their node ids distinct;
   when the default branch changed, recompute `targets_default_branch` for its PRs. A repository that
   cannot be resolved, has no default branch, or resolves to a different `github_id` than its stored
   row (the `is_tracked` row whose `full_name` equals the configured name, ignoring case) fails for
   this run, and nothing of it is deleted; when no configured repository resolves and the deadline
   stopped none of the resolves, the run is fatal (exit 1). A stored repository that a configured
   entry resolves to gets `is_tracked = true` and its `repo_group` from the config. A stored
   repository is marked `is_tracked = false` and reported (D27) only when the run has no `--repo`
   filter, every configured entry resolved to its stored `github_id` or to a new repository, and
   none resolved to this repository's `github_id`. A run with `--repo` attempts only the
   repositories it names: it neither marks nor reports untracked repositories, and writes no
   `repo_sync_state` for others. A configured repository that never resolved has no `repositories`
   row, so it has no `repo_sync_state` row either; the exit code and the log report it. A stored
   repository whose configured entry failed to resolve gets `last_outcome = failed`, and one the
   deadline kept from resolving gets `incomplete_index`; either makes the exit 2. Two configured
   entries that resolve to one `github_id` (an old name and the new one both listed) fail the later
   entry in `tracking.toml` order, naming both (gap 92): it writes nothing and counts as an entry
   that failed to resolve. The resolve query also reads the repository's labels; a stored item that
   carries a label the repository no longer has is planned for hydration, because renaming or
   deleting a label changes no item's `updatedAt`.
2. Index, in the same order: enumerate every pull request and issue with
   `pullRequests|issues(first: 100, orderBy: {field: CREATED_AT, direction: ASC})` (5.8). The index
   carries each item's `fullDatabaseId`, state, and dates (`createdAt`, `updatedAt`, `mergedAt`,
   and `closedAt`, which the coverage rule reads for a closed PR), and for PRs `isDraft`,
   `authorAssociation`, and the first 10 closing-issue links. The repository's
   `last_index_started_at` is the GitHub `Date` header of the first request of its completed scan
   (the PR connection's when both are indexed) minus 60 s, because the header has one-second
   resolution and is set after the page is read. Creation order is stable, so an update during
   the scan cannot make pagination skip an item; a number listed twice or out of creation order
   fails the repository, as does a page whose repository `databaseId` differs from the resolved
   `github_id` (gap 104). The enumerated count must equal the `totalCount`
   returned with the last page. A transfer or deletion during the scan breaks that equality, so on a
   mismatch the connection is rescanned once; a second mismatch leaves the repository with no item
   writes, in-place updates, or deletions this run (step 1's repository row and labels, read by the
   resolve, are still written). One GitHub request is in flight at a time: GitHub limits GraphQL to 60 s of
   server CPU time per minute and advises against concurrent requests, and parallel scans were never
   measured. Step 5 measures before any wider setting.
3. Plan by `github_id`. Hydrate an in-coverage item when it is not stored, its `updatedAt` differs
   from `github_updated_at`, its index `state` differs from the stored `state`, its
   `hydration_version` differs from the code's for its kind, it is unsettled
   (`hydrated_at - github_updated_at < 10 minutes`), it carries a label its repository no
   longer has (step 1), its indexed links differ from its stored links (its link `totalCount`
   differs from `closing_issues_total`, an indexed link is not stored, or, when the index lists all
   of them, a stored link is not indexed; D23), the index lists fewer distinct links
   than the smaller of its `totalCount` and 10 (a link listed twice), its link `totalCount` exceeds
   the 10 links the index carries, so a change beyond the 10th is never missed (gap 102), or it has
   a `sync_failures` row, so a failed item is retried even when nothing else changed. A link change
   goes through hydration, which reads the fields the index does not (`user_linked`, the issue's
   id). Update in place without hydration, for items not planned for hydration in this run:
   `is_draft` and `author_association` from the index (ready-for-review is not proven to change
   `updatedAt`, 0 of 151 counterexamples; association changes do not change it);
   `repo_sync_state.last_index_started_at` is their as-of time. The writer applies in-place updates
   before the first hydrated write; a hydrated item takes every field from its hydration response,
   except that a null actor never replaces a reference D28 keeps. Order the hydration queue: items
   in `sync_pending` first, then open items, then by `updatedAt` descending, so a time-limited run
   refreshes the newest data first and the next run finishes what it left. The queue spans the
   attempted repositories: steps 1-3 run for all of them before any hydration, and each batch takes
   the first unbatched item and further unbatched items of the same repository and kind, in queue
   order, up to that kind's batch size (25 PRs or 100 issues, step 4). The
   in-place updates of every completely indexed repository are applied even when the deadline stops
   a later repository's index. `sync_pending` only orders the queue: a pending item none of the
   conditions above selects is not hydrated. An in-coverage index entry whose number is stored under
   a different `github_id` is an item failure; the stored row then leaves through step 7 (an entry
   outside coverage is not hydrated, so its number is no failure). An index entry whose state its
   dates contradict is an item failure and is not hydrated, whatever the conditions above say; its
   stored row stays (section 3). Neither failure is an in-place update: an item failure writes
   nothing for the item (step 4).
   A failure the engine detects itself (these two, a hydrated `github_id` other than the index's, a
   review or comment id stored under another PR, a per-batch lookup's `databaseId` other than the
   commit link's) is recorded at once, without the solo retry, which would repeat a deterministic
   result. The three contradictions of the run's own index that step 5 checks (an alias `NOT_FOUND`, an item
   outside coverage, an earlier `updatedAt`) are the exception: a lagging read can clear, so the
   engine requests the item once more alone before it records the failure.
   The planner is a pure function over snapshots of the stored rows, which `--dry-run` reuses. The
   run reads its snapshot once, after the claim and before step 1 (a dry run, after the head check).
4. Hydrate: GraphQL batches of 25 PRs or 100 issues of one repository via aliased
   `pullRequest(number:)` and `issue(number:)` fields (5.8). Read each alias separately. Overflow
   beyond 100 entries on any connection (reviews, comments, commits, files, labels, closing issues,
   user-linked closing issues, review requests, assignees) pages through `node(id:)` with the same
   node selection as the connection's first page; continuations from several items share one
   request, and each also reads
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
   | Server or transport error | other 5xx; a GraphQL server fault at any path; connection error; client timeout; a connection the server broke off or a body it encoded wrongly; a GraphQL `errors` key whose value is not a list, `null` included (gap 104); a response without data or errors, without a readable `Date`, or with an error path that is not strings and list indices (never trimmed, since an exact path is what makes an item gone) | Retry up to 3 more times (2, 4, 8 s, with jitter), then split |
   | Item gone | alias `null` with `NOT_FOUND` | The hydrator reports it gone and does not retry it. In a sync, the run's own index listed the number in coverage, so GitHub's two answers disagree: the engine requests the item once more alone, and a second `NOT_FOUND` is an item failure (step 5, gap 100); an index that no longer lists the number removes the item and its failure (step 7). In verify's lookup by number, removed since sync (5.4) |
   | Item error | another alias error; partial alias data; a value the typed models reject; a failed check in step 5; HTTP 404/422 on a compare page | Retry the item once alone, then record it in `sync_failures` and write nothing for it |
   | Repository gone during hydration | a batch's `repository` is null (`RepositoryNotFound`, `RepositoryShapeError`; gap 79) | The repository fails for the rest of the run: its written items stay, step 7 skips it, and its `sync_pending` rows are unchanged |
   | Local database error | any error the database raises while the writer writes (a lock held past the busy timeout, an unexpected constraint failure; gap 77) | Fatal: roll back the item, close the run, exit 1 |
   | Not understood | a GraphQL error without a path, or no data, that matches none of the classes above (a rejected document, an unrecognized limit message); a missing or malformed `rateLimit`; a 2xx REST response whose body is JSON and whose `Date` is readable but which lacks readable `x-ratelimit-remaining` and `x-ratelimit-reset` (gap 104; a 2xx whose body is not JSON or whose `Date` is unreadable is a server error, and other statuses take the other rows of this table, whatever their headers); another status (400, a GraphQL 404 or 422) | Fatal: close the run, exit 1 |
   | Unauthorized or forbidden | HTTP 401; a failed credential request (an App token mint answering 4xx without a rate-limit signal); HTTP 403 with no rate-limit signal (a missing permission); an HTTP client failure outside the network (a redirect loop, a proxy or URL problem, a missing auth dependency) | Fatal: close the run, exit 1 |

   Typed response models check types strictly: GitHub's JSON has exact types, and lax coercion
   would read `"no"` as false. `BigInt` ids must be strings of digits and timestamps ISO strings.

   Splitting continues down to single items. A request that cannot split retries like a server
   error (3 more times, 2, 4, 8 s) in the timeout class too. A single item whose own request still
   fails after those retries is recorded as failed without the solo retry, which would repeat that
   request; every other item error gets its solo retry. A resolve or index page that still fails
   fails its repository for this run. A compare page that still fails is an item error. A
   user lookup failure is scoped to what it covers, one node or one request of 100 ids, and is an
   item error for each item waiting on those accounts (5.2). A failed 24-hour refresh (a lookup that
   still fails after its retries, an answer the typed models or the id check reject, or a
   `databaseId` other than the stored `github_id`) keeps the stored values (D28), is listed in the
   report, counted in `sync_runs.refresh_failures`, retried next run, and makes the exit 2 unless
   an account entry in `[sync].accepted_failures` accepts it (D37, gap 98). Fields that drive logic
   (`state`, review `state`) are closed enums in the typed models; descriptive enums are text (5.1).

   Any other exception after the claim is fatal too. A fatal run closes as `failed` (exit 1), with
   step 7's halt reasons when step 7 ran.

   The `--max-minutes` deadline applies to every request; the run reaches it when a request or a
   wait is refused for it. At the deadline the run issues no new requests, treats a repository whose
   index is unfinished as incomplete, records `sync_pending`, and runs steps 7 and 8. Without
   `--max-minutes`, a rate-limit wait longer than a named constant fails the run (exit 1).

   Budget, per pool: GraphQL readings come from `rateLimit { cost remaining resetAt }` in every
   response. One token's readings can alternate between two windows, so the guard keeps the lowest
   `remaining` per `resetAt` and waits until every window below the reserve has reset. REST readings
   come from the `x-ratelimit-remaining` and `x-ratelimit-reset` headers of compare responses. Below
   the reserve (a named constant above the largest measured request cost) the guard sleeps until the
   reset, subject to the deadline rule above. `sync_runs.graphql_points` is the sum of
   `rateLimit.cost`; `rest_calls` counts REST requests.
5. Validate before writing: the fetched count equals `totalCount` for reviews (requested without
   `PENDING`, so the counts agree by construction), comments, commits, files, labels, closing
   issues, user-linked closing issues, review requests, and assignees; every user-linked closing
   issue is also in the full closing list; every overflow page reports the first page's `totalCount`; a null connection or null node
   is a failure, never a count of zero; the compare `total_commits` equals the PR's commit
   `totalCount`, the fetched compare commits equal `total_commits`, and the commit connection's first
   page equals compare's commits at positions `total_commits - 250` to `total_commits - 151`, from
   0, in order (gap 104); the returned number equals
   the request (the batch names the resolved repository); `state` is `MERGED` exactly when
   `mergedAt` is set, and a `CLOSED` or `MERGED` PR has `closedAt`, which the coverage rule reads;
   the entries the database keys (labels, closing issues, user-linked closing issues, reviews,
   comments, commits, assignees, each assignee by its node id and by its `github_id`) and the
   review requests, by their own id, are distinct; every page with a next page has entries and a new cursor; and an error
   outside every item fails the request, which then splits, and an item it fails alone gets its
   solo retry, since no retry of the transport preceded it. The hydrated PR's `github_id` equals the
   one the index listed for its number, and a review or comment whose `github_id` is stored under
   another PR fails the item. A PR that closed without merge between index and hydration is written
   with state `CLOSED` (D3). A hydration that contradicts the run's own index (an alias `NOT_FOUND`,
   an item outside coverage, an `updatedAt` earlier than the index entry's) is requested once more
   alone by the engine, which holds the index, and a second contradiction is an item failure of class
   `IndexContradiction`, whose cause names which of the three it is, recorded before any account
   lookup for it (gap 100), and the run's report lists each contradiction with its kind and
   whether the retry cleared it; a deadline that refuses that request
   leaves the item in `sync_pending`, and the batch's other checked items are written as at any
   deadline. Above 250 commits GraphQL's commit connection holds the newest 250 (section 7, "Commit
   cap"), so its first page is compare's 100 commits from position `total_commits - 250`, as the
   recording of Prebid.js #8858 shows (254 commits, the page at positions 4-103). Only that page is
   read, since the other 150 would cost continuations, and no GraphQL read reaches the oldest
   `total_commits - 250` commits, where a wrong base adds or drops commits, so the count checks are
   their only check.
6. Write: one batch at a time, on the writer's one connection. The run hydrates a batch, checks it
   (step 5), looks up its accounts (5.2), and writes each of its items before it requests the next
   batch; at the deadline it writes the fetched items whose accounts it has and records the rest in
   `sync_pending`. The hydrator delivers a batch's outcomes together (Step 2), so a deadline inside
   the batch's own requests (a split half, a solo retry, an overflow or compare page) writes none
   of its items. The hydrator hands over, with the deadline, the outcomes it had made final (a
   failure after its solo retry, or one that gets none), and those failures are recorded (gap 99);
   the rest of the batch waits in `sync_pending`, which puts it first in the queue of the next run
   that indexes the repository completely and selects it (step 3). Each item is one transaction:
   upsert by `github_id` with `INSERT ... ON CONFLICT (github_id) DO UPDATE`, which keeps the row id
   (never `INSERT OR REPLACE`, which deletes the row and cascades); replace the hydrated child rows
   (`pr_labels`, `pr_reviews`, `pr_comments`, `pr_commits`, `pr_files`, `pr_closing_issues`,
   `pr_review_requests`, `pr_assignees`, `issue_labels`), where a new review or comment with a null
   author takes the stored author of the row with the same `github_id`, a commit with a null link
   takes the stored link of this PR's row with the same SHA, and a null PR or issue author or merger
   keeps the stored one (D28), each with its `*_reported` column false (`linked_github_id` null for
   a commit); store the validated totals; set `hydrated_at`. Fetchers never touch the database. A
   writer refusal for a link the engine did not supply (`AccountMissing`) is an item failure. Every
   writer transaction is one `BEGIN IMMEDIATE`. One that writes for the run first checks the run's
   row is still `running`, and the close updates the row only while it is `running`, so a run
   another process aborted writes nothing more; the claim, which has no row yet, and the writer's
   reads, which write nothing, do not check it. A database error invalidates the writer's connection
   before the close, because a `COMMIT` that failed on a lock leaves SQLite's transaction open on
   it. The writer holds one connection for the run and binds its session to it: the engine's
   `NullPool` gives a session bound to the engine a new connection, with the connect listener rerun,
   for every committed transaction, so `get_session()`'s pattern would open one connection per item.
   A transferred issue arrives with a new `github_id` and number and is inserted in its new
   repository; step 7 removes the old row.
7. Delete: after all writes, the refresh's included, for each repository with a complete index that
   did not fail during hydration (step 4), recompute from the database, read once, the stored items
   whose `github_id` is absent from that repository's in-coverage set, then delete
   them, together with `sync_failures` rows for items absent from it. Halt deletions for a repository
   when the count exceeds max(25, 2% of its stored PR and issue rows, counted in the planner's
   snapshot before this run's writes), and record the halt and the
   candidate count in `repo_sync_state.halt_reason`; the `sync_failures` rows of items absent from
   the in-coverage set are removed even then. `ghactivity sync --allow-deletions owner/name=N` (given at
   most once, N at least 1, naming a configured repository)
   deletes all of that repository's candidates when there are at most N; otherwise the guard decides
   as without the flag, so a count within max(25, 2%) is still deleted. It applies N to each guarded
   set separately (the items here, and from Step 4b the
   repository's activity events and default-branch commits), and records the approval in
   `sync_runs.deletions_approved`. `--prune-untracked` deletes
   the repositories that were untracked when the run started and are still untracked after step 1
   (their rows and, by cascade, everything stored under them; `github_users` stay), outside the
   delete guard, counts their items in `sync_runs.items_deleted`, and lists each pruned repository
   in `sync_runs.deletions_approved` (D27). It is
   refused, after step 1, when any configured entry failed to resolve or `--repo` is given; a refused
   prune deletes nothing and makes the exit 2. A repository dropped from `tracking.toml` is pruned
   by a run after the one that marks it untracked, which exits 2; a pruned repository is no longer
   stored, so it makes no exit 2.
8. Record: update `repo_sync_state` and close the `sync_runs` row. Exit 0 when every repository
   indexed completely and nothing failed; 2 when an item failed in this run (other than one listed
   in `[sync].accepted_failures`), a repository failed, had an incomplete index, or halted, a repository's Step 4b activity read failed
   or halted, a stored
   repository is untracked in a run without `--repo`, a prune was refused (step 7), a refresh
   failed (step 4, other than one an account entry accepts), the run reached its deadline (status
   `time_limited`, whatever it stopped: hydration, the refresh, or a Step 4b read; gap 99), or, from
   Step 6a, its `links_not_found` exceeded the share of its `link_lookups` Step 5 sets, with at least
   the minimum number of lookups Step 5 sets (5.2); 1 on a fatal error. The exit code is decided after the prune. An accepted failure is still recorded and
   reported; it names the repository and number, the account, or a compare, with the error class, the
   cause, and a reason in `tracking.toml`, so a permanent failure does not turn every run red, and a
   failure of another class or cause is not accepted (5.5, gap 101). An item entry
   is reported as not having failed when its repository's outcome in this run is `complete`,
   `time_limited`, or `halted` and its item neither failed in this run nor is pending (5.5); an
   account entry, only when this run's refresh looked the account up and the lookup succeeded; a
   compare entry, only when this run requested that compare and GitHub answered it with a 2xx first
   page holding `files` and every commit (Step 4b).
   `--max-minutes` stops queueing new hydration, records in `sync_pending` the planned items it did
   not reach (a repository that failed during hydration keeps its rows, step 4), still runs step 7
   for complete repositories and step 8, and sets status `time_limited`; unreached items are not
   item failures, but the deadline makes the exit 2 (D37). Step 8 writes `last_run_id` and
   `last_outcome` for every repository the run attempted. A close given an outcome for a repository
   it finds no stored row for raises inside its transaction before it writes, so nothing is written
   and the run exits 1 with its row `running` for the stale-row rule or `--break-lock`, as a close
   that fails does (gap 105). `--dry-run` takes no lock, inserts no `sync_runs` row,
   and writes nothing: it runs steps 1-3 as the run would and reports step 1's updates, the planned
   hydrations with their reasons, the in-place updates, and the delete candidates. It exits as the
   run would for every step-8 condition steps 1-3 can show (a failed or incompletely indexed
   repository, an untracked stored repository, a deletion halt, a refused prune, an unaccepted item
   failure the planner detects), and, like every reader in `src/`, uses no server-side cursor, since
   a half-read query blocks a running sync's commit (gap 77).

   `status` records how the run ended and `exit_code` its result. `complete`: it reached step 8
   without reaching the deadline; `time_limited`: it reached the deadline (step 4) at any point, the
   refresh included; `failed`: exit 1; `aborted`: `--break-lock`, a reclaimed stale row, or an
   interrupt. A repository's `last_outcome` is the first that applies of `untracked`, `failed`,
   `incomplete_index` (a count mismatch after the rescan, or the deadline before its resolve or
   during its index), `halted`, `time_limited` (planned items left unreached), and `complete`; step
   8 also writes `last_outcome = untracked` and `last_run_id` for every stored repository it finds
   untracked. The close of a fatal or interrupted run also writes `last_run_id` and
   `last_outcome = failed` for every repository it attempted, and step 7's `halt_reason` for one
   whose step 7 ran, never `last_index_started_at`, because its committed writes are in the file. A
   deadline before any repository resolves makes the run `time_limited`, not fatal. A run that never
   starts exits 1 before any `sync_runs` row: a usage error (click's own code for it is 2, the code
   D25 publishes on; gap 69), an invalid `tracking.toml`, missing credentials, a missing database
   file or one not at head, a refused claim, or a database error before the claim returns (the busy
   timeout included). `--repo` and the `--allow-deletions` repository must each name a configured
   repository, ignoring case; a single-valued option given twice (click keeps the last value
   silently) and `--dry-run` with `--break-lock` are usage errors. A report that cannot be written
   leaves the exit code the run's. When stderr cannot take what the process wrote to it (a closed
   pipe or a full disk: a never-started run's error, or `-v` debug logs), the interpreter fails its
   flush at exit and exits 120 instead; 120 is outside the codes D25 publishes on.

### 5.4 Verification

`ghactivity verify [--repo owner/name]... [--exact] [--max-minutes N] [--report PATH]` (online)
checks the database against a fresh index, as of the sync it follows: for each repository, "that
run" is `repo_sync_state.last_run_id` and the boundary is its `last_index_started_at`. verify uses
the 5.3 step 4 failure classes and retries. It covers the configured repositories and, without
`--repo`, every stored untracked repository, which it reports as not synced without a request: a
stored repository that no configured entry resolves to without failing 5.3 step 1 is untracked when
its `is_tracked` is false, and, when every configured entry resolved without failing 5.3 step 1
(to its stored `github_id` or to a new repository), also when it is true, as a sync marks it (5.3
step 1). `--repo` limits verify to the repositories it names, each a configured
repository, ignoring case, as it limits a sync (D27). Like
`sync`, it exits 1 before any request unless the database is at the code's migration head and
`user_version` equals `schema_version` (gap 74), and it uses no server-side cursor (gap 77); it
also exits 1, naming the run, while a sync run holds the lock: before any request it reads
`sync_runs` and stops when a row is `running` with a null `deadline_at` or one less than 10 minutes
in the past, a deadline not yet reached included, the claim's rule (5.3). verify writes nothing to
the database and claims no run, so a stale row stays for the next claim to reclaim, and a crashed
run with a null `deadline_at` stops verify until a `sync --break-lock` run marks it `aborted` (D36).
A sync can still claim after that check (gap 94), so for each repository, before the first request
of its index, verify reads its `repo_sync_state` row with its `sync_pending` rows in one statement,
then its stored items with their closing links and `sync_failures` rows in one statement. Each
statement reads one consistent state (gap 77), and the stored side is never newer than the fresh
index.
It re-indexes and applies the coverage rule, then
compares by `github_id` the stored set with the result (missing and extra items) and, for items in
both, the stored `github_updated_at`, `state`, `is_draft`, `author_association`, and closing links
with the fresh values
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
  for differing items; an `author_association` difference is drift with its own count. A
  connected or disconnected event matches a link difference when its `subject` is the differing
  issue (its repository and number); a link made by a closing keyword produces no such event, so
  its difference is drift only through `createdAt` or `updatedAt` (section 7, "Closing-link
  timeline events"). The index names at most 10 links, so a PR with more can differ in its totals
  alone; that difference is drift when the connected events at or after the boundary number at
  least the total's increase, or the disconnected events at least its decrease, every such event
  counted, those that match a differing issue included. For a link difference with an earlier
  `updatedAt`, the difference is drift only when each part of it is: each differing issue (indexed
  and not stored, or stored and not indexed when the index lists every link) by an event whose
  `subject` is that issue, and a difference in the totals by that count;
- removed since sync: a stored item missing from the fresh index whose lookup by number returns
  `NOT_FOUND`, listed by number (the lookup is a hydration batch: `Gone` is removed since sync, a
  hydrated PR an error, not retried: an index that omits a live PR is the read the sync's step 7
  deletes by, so verify reports it);
- unchecked: a differing item whose timeline lookup (5.8) or lookup by number still fails after the
  retries on a request GitHub did not answer (a transport, server, or timeout failure; a rate limit waits:
  a wait the deadline refuses ends verify as a timeout, and without `--max-minutes` a wait longer
  than the named constant is fatal, 5.3 step 4),
  or that a lookup had not answered when a repository error stopped it, those of the batch the error
  ended included, listed by number;
- error: anything else, a lookup whose answer verify cannot accept included (a timeline lookup
  whose entries still differ from `filteredCount` after its retry, a value the typed models reject).

An error, an unchecked item, and an unverified repository fail verify in both modes: what verify
could not check is a failure, never a pass (D37). `verify --exact` also fails on not synced,
pending, halted deletion, removed since sync, and known failures no `[sync].accepted_failures`
entry accepts as 5.5 matches it (repository, number, error class, and cause); it is the mode the
Step 3-5 acceptance uses, and drift is reported in both modes. A repository that verify cannot
resolve, re-index, or look up is reported as unverified: every repository error the source raises at
resolve or index (not found, no default branch, a fetch that still fails after its retries, a
response or GitHub error the resolve or index rejects, a label list that fails its count or names a
label twice, paging without progress, a number listed twice or out of creation order, a second
count mismatch), step 1's own checks (a resolve to a `github_id` other than its stored row's, a later
entry resolving to an earlier entry's `github_id`, gap 92, and another `is_tracked` stored repository
holding the resolved `full_name`, ignoring case, checked as a dry run checks it, against the stored
names and the names earlier entries resolved to, 5.1), a repository error during a lookup by
number or a timeline lookup (5.3 step 4), and, from Step 4b, verify's own activity or history read
that still fails after its retries, ends before the floor and the log's first event, or whose
count still differs after one rescan; each keeps the classes the repository's comparisons already
gave. An unverified repository fails verify and is reported with its reason; a verify in
which every repository it attempts is unverified, and the deadline stopped none, is fatal (exit 1),
as a sync is when no configured repository resolves (5.3 step 1). A verify that reaches
`--max-minutes` stops with a timeout instead of waiting past it. Exit 0 when no failing class
occurred and no repository is unverified, 2 otherwise, 1 on a fatal error or a timeout, and 130 on an interrupt, the CLI's code
(5.3). A verify that never starts exits 1 before any request, writes no report, and leaves PATH as
it was: a usage error (`--repo` naming no configured repository, a single-valued option given
twice), an invalid `tracking.toml`, missing credentials, the head check, a held lock, a database
error before its first request (the busy timeout included), or a report path that cannot be opened
for writing, which verify opens last before its first request. Once it starts, `--report PATH`
writes a versioned JSON report on every exit its own code handles, 1 and 130 included (not SIGKILL,
SIGTERM, or a second interrupt, as for sync, 5.3), marked incomplete with the reason when verify
stops before its comparisons finish. A report it cannot write at exit turns an exit 0 or 2 into 1,
because Step 6a reads it (5.6, D36); an interrupt exits 130 either way. The report holds the exit
code and whether `--exact` was given;
per class, the count of items and their numbers (an item whose differences take two classes counts
in each); per repository, its `last_run_id`, boundary, per-class counts, per-state counts, and per
child-count check the rows it compared (D39);
for each closing-link difference, the issues it names and the stored and fresh totals; from
Step 4b, per repository the compares stored with `files_at_cap`, those GitHub refused, and those
that still failed, which do not fail verify (GitHub's limit, and answers the sync requests again
on each run, Step 4b) but are never reported as complete; the unverified repositories with their reasons; and verify's GraphQL points and REST
calls, counted by its source as a run's are (5.3 step 4), and the seconds from its start to the
report's write, since verify writes no `sync_runs` row. A report verify prints that stdout cannot
take leaves the exit code verify's, and stderr follows 5.3 step 8, 120 included.

The enumerated count must equal the last page's unfiltered `totalCount` (rescanned once, as in
5.3); per-state counts are
derived from the enumeration and reported, never asserted, because a state change during a scan
moves an item between states. Stored child rows must equal stored totals: `pr_files` rows equal
`files_total`, `pr_commits` rows `commits_total`, `pr_reviews` rows `reviews_total`, `pr_labels` rows
`labels_total`, `pr_closing_issues` rows `closing_issues_total` and those with `user_linked`
`user_linked_closing_issues_total`, `pr_comments` rows `comments_total`, `pr_review_requests` rows
`review_requests_total`, `pr_assignees` rows `assignees_total`, and `repo_labels` rows
`repositories.labels_total`; from Step 4a, `issue_labels` rows equal `issues.labels_total`, and the
issue comment and assignee rows their totals (5.1); from Step 4b,
`repository_activity_compare_commits` rows equal `total_commits` for each compare
with a 2xx `http_status`, and `default_branch_commits` rows equal
`repo_sync_state.default_branch_commits_total`. A child-count mismatch is an error. Each check is
one SQL statement, so a verify that reads beside a sync's item commits compares one consistent
state per check.

`ghactivity verify --db-only` (Step 6a, beside the `snapshot` command that writes its inputs; it takes
the snapshot gz, its manifest, the asset digest, and `--out PATH` as explicit arguments, and a halt
exits 1) decompresses to PATH, prints that file's SHA-256 and its highest `sync_runs` id (0 when
it has none) on its last line once every check passes,
and checks a downloaded snapshot before anything touches it: the gz SHA-256
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
`reason` are non-empty; automation account ids and emails are unique, emails printable ASCII
(space to tilde, the column's CHECK), stored lowercase, and compared lowercase (5.1);
`unsettled_minutes` is an integer of at least 1 and at most 1,439,999,999,999 (the most minutes a
Python `timedelta` holds, so the planner can build the window), `delete_guard_min_rows` an integer
of at least 0,
and `delete_guard_percent` a number above 0 and at most 100; an `accepted_failures` entry names
an item (a configured `repository` and a `number` of at least 1), an account (`github_id`, for a
refresh failure, or `node_id` for an account stored with a null `github_id`), or from Step 4b a
compare (a configured `repository` and the event's `event_id`), with an `error` and a `message`.
The failure's text (`sync_failures.last_error`, the refresh failure's, or the compare's `error`)
is split at its first `": "`: `error` must equal the class, and `message` the first line of the
rest, the failure's cause. Every failure the sync records puts its cause, in the code's own words,
on that first line, and what can change between runs (GitHub's message and request id, a count, a
typed model's rejection text) on the lines after it, so an entry keeps matching a permanent failure
while a failure with another cause is not accepted; several classes carry more than one cause. The
loader refuses an entry without `message`, and a class whose name is not in the list of recorded
failure classes `tracking.py` keeps, which imports none of the modules that define them; a test
holds that one list, which serves every entry form, equal to the concrete subclasses of
`ItemError` and `PlannerFailure`; `ItemConflict`, `AccountMissing`, `LookupConflict`, and
`UserLookupFailed` (an item whose commit-link lookup failed, or a refresh); the engine's
`HydratedIdMismatch` and `IndexContradiction`; and the classes Step 4b's compare reader records. Entries are unique by item, account, or compare, an item or
compare entry matches by its configured repository, so a rename does not unhook it, and a new
cause is not accepted (gap 101); an entry whose item, account, or compare did not fail is reported
(5.3 step 8).
`[automation]` is required, its lists may be empty, so a file without it is refused instead of
emptying the automation tables (gap 105). The file holds domain policy only. Step 1 added
`[[repositories]]`; Step 3b-2a-1 adds `[coverage]` and `[sync]`'s `unsettled_minutes`,
`delete_guard_min_rows`, and `delete_guard_percent`, which the planner reads; Step 3b-2a-2a adds
`[automation]` and `[sync].accepted_failures`, which the run claim and the exit code read; Step
3b-2a-3 adds an entry's `error`, `message`, and account form, and makes `[automation]` required;
Step 4b adds the compare form.

```toml
[coverage]
floor = 2023-01-01T00:00:00Z   # required; a TOML offset date-time; converted to UTC

[[repositories]]
owner = "prebid"
name = "Prebid.js"
group = "Client"
# one entry per tracked repository

[automation]
# Accounts and commit emails to list; the views show each entry's reason beside GitHub's account type.
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
# Items or accounts whose failure is understood and does not count toward exit 2 (5.3 step 8).
accepted_failures = [
  # { repository = "prebid/Prebid.js", number = 12345, error = "ItemConflict",
  #   message = "#12345: ...", reason = "..." },
  # { github_id = 1234567, error = "UserLookupFailed", message = "...", reason = "..." },
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
  `ghactivity sync`, 5.3 step 7), `repo` (repositories separated by spaces, each validated as a
  configured repository and passed as `--repo` to sync and verify; it requires `bootstrap`, the
  manifest records it, and a run with it never publishes to release `db`, since `publish`'s `if:` requires it to be
  empty for `db` (D25): Step 6b's comparison is its use), and `release` (default `db`; the Step 6 acceptance runs against `db-test`). A `schedule` run has no inputs, so every step reads `${{ inputs.release || 'db' }}` and
  treats the other inputs as unset.
- `concurrency: { group: db-sync, cancel-in-progress: false }`. A new run replaces a pending one and
  never cancels a running one.
- Top-level `permissions: {}`; each job is granted only what it lists. Three jobs, so the job that
  runs project code and PyPI dependencies holds no write token:
  - `sync` (`permissions: contents: read`; `timeout-minutes: 355`, under GitHub's 6-hour job limit)
    holds the App credentials. Without `bootstrap`, it downloads the latest snapshot and runs
    `verify --db-only`; a missing or failing snapshot halts the run. With `bootstrap`, it downloads
    nothing and starts from an empty file, and the manifest records `bootstrap: true`. One job-level
    `DATABASE_URL` names the file every step uses, the `--out` path of `verify --db-only`; without
    `bootstrap`, before `alembic upgrade head` the job records the highest `sync_runs` id that
    `verify --db-only` printed (N; 0 for a bootstrap) and fails unless the file's SHA-256 equals the
    one it printed (gap 107). It then runs `alembic upgrade head`, asserts the revision is head, runs
    `ghactivity sync --max-minutes 300` and `ghactivity verify --max-minutes 40 --report
    verify.json`, runs `ghactivity snapshot --out DIR --run-id ${{ github.run_id }} --after-run N
    --sync-exit C --verify-report verify.json`
    with C the sync's exit code, and hands the snapshot and manifest to `publish` as a workflow
    artifact, with the sync and verify exit codes as job outputs. The job fails when
    `verify --db-only` halts, the file's SHA-256 differs, sync exits 1, or `snapshot` refuses.
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
  integer or a 64-character hex digest, and never interpolated into a `run:` script. An `if:`
  compares an exit-code output as a quoted string (`== '0'`) and requires it to be non-empty, since
  GitHub's expressions read an empty string as 0 (gap 107).
- Authentication (D10): `ghactivity sync` and `ghactivity verify` authenticate in-process with
  githubkit's `AppInstallationAuthStrategy` (app ID, private key, and installation ID from secrets;
  contents, pull requests, issues, and metadata read), which mints a new installation token before
  the 1-hour expiry. A token minted once by `actions/create-github-app-token` would expire during the
  bootstrap or any long run. When App credentials are set, the CLI uses them and ignores
  `GITHUB_TOKEN`. This needs the `githubkit[auth-app]` extra (PyJWT, cryptography) in the locked
  dependencies (Step 6a). The App key is readable by the sync job's dependencies, so the App stays
  read-only and its key rotatable. The workflow's own `GITHUB_TOKEN` (`contents: write`) is used
  only in `publish`.
- Snapshots: `ghactivity snapshot --out DIR [--run-id ID]` with either `--after-run N --sync-exit C`
  or `--sync-run ID` (exactly one of the two; another combination is a usage error, exit 1), and
  `[--verify-report PATH]`, writes the copy with `VACUUM INTO`, checks its header for
  rollback-journal mode (the check `scripts/check_db_journal.py` makes), gzips it, and writes the
  manifest, in the workflow and for 6b's first snapshot (`run_id` null). Names are
  `github_activity-<T>.db.gz` and `github_activity-<T>.manifest.json`, with `T` the UTC time
  `snapshot` writes the copy, as `YYYYMMDDTHHMMSSZ`, because GitHub renames asset names that contain special characters. The latest
  snapshot is the newest `T` whose two assets are both in state `uploaded`; `verify --db-only` on
  that pair decides, and a mismatch halts, with no fallback to an older pair. The manifest records
  `actions_run_id`, `sync_run_id`, `sync_finished_at`, the code SHA, the `tracking.toml` digest
  (`sync_runs.config_sha256`), migration head,
  `schema_version`, coverage floor, `bootstrap`, per-repository PR and issue counts, per kind the
  number of rows whose `hydration_version` differs from the code's, the sync exit code, the sync
  run's `repo_filter`, `link_lookups`, and `links_not_found` and from Step 4b its `events_deleted` and
  `history_commits_deleted`, verify's exit code, counts per class, unverified repositories, and from
  Step 4b the compares at the file cap, those refused, and those that still failed (from
  `--verify-report`; null without it), and the SHA-256. `snapshot` exits 1 when the report `--verify-report` names is missing or
  unreadable, or names a `last_run_id` other than the file's `repo_sync_state`, so a verify that
  never started, which leaves no report or one from an earlier sync (5.4), fails the job. In the
  workflow, `snapshot --after-run N --sync-exit C` takes the highest `sync_runs` id the job recorded
  before the sync (0 for a bootstrap) and the sync's exit code, and exits 1 unless exactly one
  `sync_runs` row is newer than N, that row is closed with exit code C, and it is the
  `last_run_id` of the stored row of every `github_id` that run's `sync_runs.entries` records (an
  entry that never resolved records none, 5.1; a recorded `github_id` with no stored row has
  nothing to check), so a job whose sync did not run
  cannot republish the old file (gap 107). For 6b's first snapshot, `--sync-run ID` names Step 5's
  last run instead, which must exist and be closed with exit 0 or 2, and supplies the manifest's
  sync exit code. The manifest records that run as `sync_run_id` and `sync_finished_at`, and
  `--run-id` as `actions_run_id`.
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
- Consumer access: a consumer finds the latest pair through the Releases API, reads the manifest,
  downloads the `.db.gz`, checks its SHA-256 against the manifest, decompresses it once, and
  replaces its local copy by writing a temporary file and renaming it over the old one, so no query
  reads a half-written file. It does this once per new snapshot (about every 6 hours, D11), not per
  read, and queries its copy with SQLite. It treats a pair whose manifest `sync_finished_at` is older
  than 13 hours (D11's 6-hour interval, a job of up to 355 minutes, and an hour of schedule delay)
  as stale and says so wherever it shows the data, with the manifest's sync exit code and verify
  counts beside it: a stopped schedule runs nothing, so only a check outside this repository can
  see it, and the dashboard's integration builds this one (section 9, gap 107). The download runs
  outside a browser, on a server or in a build step: the asset host
  (`release-assets.githubusercontent.com`) answered without `access-control-allow-origin`, so a page
  on another origin is not expected to read the file (inferred from the response headers, no browser
  was run; section 7, "Release downloads"). How a page reads its copy is the frontend's design,
  outside this plan.

A `workflow_dispatch` trigger runs only when the workflow file exists on the default branch, so the
workflow merges first with the manual trigger only, is proven by manual runs, and the schedule is
enabled afterwards.

### 5.7 Code layout

`source/` is the only package that imports githubkit; from Step 3, when `github/` is gone, a test
fails when another module does. It scans `src/` outside `source/`, `scripts/`, and `alembic/` for
imports at any scope, `TYPE_CHECKING` blocks included, and for `importlib.import_module` and
`__import__` called with a string literal, and fails when it scans no file; from Step 3b-2a-2d its test
asserts the exact count of the files and imports it scanned (D39). Tests are exempt, since they build githubkit's errors. A
second test fails when `source/` imports SQLAlchemy or `db/`, so fetchers cannot touch the database.

| Disposition | Modules |
|-------------|---------|
| Keep | `config.py` (without `RateLimitConfig`, `PacingConfig`, `SyncConfig`, and the `rate_limit`, `pacing`, and `sync` settings; `env_nested_delimiter="__"`, gap 64; `github_token` an optional `SecretStr`, which one CLI helper requires for the commands that call GitHub), `tracking.py` and `tracking.toml`, `logging.py` (loguru `diagnose` off, gap 75), `db/engine.py` (without `create_tables` and `drop_tables`; databases are built by `alembic upgrade head`, `tests/db/test_application_engine.py` builds its database that way and keeps both gap 46 checks, and `tests/test_db_engine.py` drops `test_create_tables`), `cli/app.py` (without the `search` and `tags` stubs, D17; tracebacks without local variables, gap 75; a run that never starts exits 1, gap 69), `cli/common.py` (gains `parse_repo_string`; `OutputFormat` goes with the removed subcommands, since no new command takes `--format`), the package and `cli/` `__init__.py` files, `py.typed` |
| New in Step 2 | `source/`: the client factory, failure classification, budget per pool, query documents with their per-kind `hydration_version`, private typed response models, typed invariant errors, resolve, index, hydration with overflow and compare, user lookup, and the typed records that cross into `sync/` (a PR record with its children; per-item outcomes hydrated, gone, or failed) |
| New in Step 3 | `sync/`: plan, writer, engine, verify, run records. The writer's SQL lives in `sync/` (`INSERT ... ON CONFLICT ... DO UPDATE`, with `COALESCE` keeping a reference D28 keeps when GitHub returns null), so one module owns each item's transaction. `db/` gains the timestamp type, `SCHEMA_VERSION`, and the packaged tuple of migration revision ids (gap 74). `source/` gains the rate-limit reading for `ghactivity github rate-limit` (Step 3a), the fields and connections 5.8 adds in Step 3b-1, and verify's timeline lookup (Step 3b-2b-2) |
| New in Step 4b | `source/`: the repository activity reader (REST `GET /repos/{owner}/{repo}/activity?ref=refs/heads/<default branch>`), the compare of each event, and the default-branch history reader (5.8); `sync/`: their planner and writer steps |
| Replace in Step 3 | `db/models.py`, `db/__init__.py` (its re-exports follow the new models), and the shared test fixtures that import them (`tests/conftest.py`; Step 3a removed `tests/factories.py`, and `tests/db/seed.py` writes rows through the models' tables); `cli/sync.py` becomes one `ghactivity sync [--repo owner/name]... [--max-minutes N] [--dry-run] [--allow-deletions owner/name=N] [--prune-untracked] [--break-lock]` command (the `pr`, `repo`, `all`, and `retry` subcommands go), plus `ghactivity verify [--repo owner/name]... [--exact] [--max-minutes N] [--report PATH]` and, in Step 6a, `--db-only` and `ghactivity snapshot`; `cli/github.py` keeps `rate-limit` over the `source/` factory (GraphQL `rateLimit` and REST `/rate_limit`; `--all` stays and `--verbose` goes), and `test [--repo owner/name] --pr N` resolves one repository (by default the first configured) and hydrates that PR |
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

Resolve (step 1; labels past the first 100 continue with the next document; cost 1 with the
Step 3b-1 fields, `rateLimit(dryRun: true)`, 2026-10-18):

```graphql
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id databaseId nameWithOwner name owner { login } isPrivate isArchived isSecurityPolicyEnabled
    visibility description homepageUrl createdAt updatedAt pushedAt archivedAt stargazerCount
    forkCount diskUsage forkingAllowed allowUpdateBranch webCommitSignoffRequired hasWikiEnabled
    hasDiscussionsEnabled hasProjectsEnabled hasVulnerabilityAlertsEnabled usesCustomOpenGraphImage
    mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed deleteBranchOnMerge mergeCommitTitle
    mergeCommitMessage squashMergeCommitTitle squashMergeCommitMessage primaryLanguage { name }
    licenseInfo { spdxId } defaultBranchRef { name }
    planFeatures { codeowners teamReviewRequests maximumManualReviewRequests }
    labels(first: 100) { totalCount pageInfo { hasNextPage endCursor }
      nodes { id name color description isDefault createdAt updatedAt } }
  }
  rateLimit { cost remaining resetAt }
}
```

Repository labels after the first page (cost 1):

```graphql
query RepositoryLabels($id: ID!, $after: String!) {
  node(id: $id) {
    ... on Repository { id labels(first: 100, after: $after) { totalCount pageInfo { hasNextPage endCursor } nodes { id name color description isDefault createdAt updatedAt } } }
  }
  rateLimit { cost remaining resetAt }
}
```

PR index (cost 1 point per 100 items, measured with and without the closing-link selection, and
with `closedAt` by `rateLimit(dryRun: true)`, 2026-10-18; `closedAt` is new in Step 3b-1; the
repository's `databaseId`, which Step 3b-2a-3 adds for 5.3 step 2's check, keeps it at 1 point,
dry run on Prebid.js, 2026-10-21):

```graphql
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    databaseId
    pullRequests(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number fullDatabaseId state isDraft authorAssociation createdAt updatedAt mergedAt closedAt
        closingIssuesReferences(first: 10) { totalCount nodes { number repository { databaseId nameWithOwner } } }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
```

Issue index (Step 4), same shape with
`issues(...) { ... nodes { number fullDatabaseId state createdAt updatedAt closedAt } }`.

Pull request fields per alias, from Step 3b-1 (25 aliases cost 2 points, the cost of the Step 2
document: `rateLimit(dryRun: true)` and the repository's formula agree, 2026-10-18; cost depends
only on the query shape). The added connections are three unnested ones (`userLinkedClosingIssues`,
`reviewRequests`, `assignedActors`); `comments { totalCount }` inside a review selects no nodes and takes
no `first`, so it adds no requests. By the formula, a fourth added connection would make the batch
3 points (section 7, "Collected fields"):

```graphql
id number fullDatabaseId state isDraft title body createdAt updatedAt mergedAt closedAt lastEditedAt
baseRefName baseRefOid headRefName headRefOid isCrossRepository authorAssociation
headRepository { databaseId nameWithOwner } mergeCommit { oid } milestone { number title }
reviewDecision totalCommentsCount publishedAt includesCreatedEdit maintainerCanModify
headRepositoryOwner { id login }
mergeable mergeStateStatus canBeRebased potentialMergeCommit { oid }
stack { id number size baseRefName } stackEntry { id position }
additions deletions changedFiles
author { ...Actor } mergedBy { ...Actor } editor { ...Actor }
labels(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { id name } }
closingIssuesReferences(first: 100) { totalCount pageInfo { hasNextPage endCursor }
  nodes { fullDatabaseId number repository { databaseId nameWithOwner } } }
userLinkedClosingIssues: closingIssuesReferences(first: 100, userLinkedOnly: true) { totalCount
  pageInfo { hasNextPage endCursor } nodes { number repository { databaseId } } }
reviews(first: 100, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) { totalCount
  pageInfo { hasNextPage endCursor } nodes { id fullDatabaseId state createdAt submittedAt
    authorAssociation authorCanPushToRepository updatedAt lastEditedAt publishedAt
    includesCreatedEdit commit { oid } comments { totalCount }
    author { ...Actor } editor { ...Actor } } }
comments(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { id fullDatabaseId
  createdAt updatedAt lastEditedAt authorAssociation publishedAt createdViaEmail includesCreatedEdit
  author { ...Actor } editor { ...Actor } } }
commits(first: 100) { totalCount pageInfo { hasNextPage endCursor }
  nodes { commit { oid authoredDate author { name email user { id databaseId login } } } } }
files(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { path changeType additions deletions } }
reviewRequests(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { id databaseId asCodeOwner
  requestedReviewer { __typename ... on User { id databaseId login } ... on Bot { id databaseId login }
    ... on Mannequin { id databaseId login } ... on Team { id databaseId slug }
    ... on EnterpriseTeam { id fullDatabaseId slug } } } }
assignedActors(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { __typename
  ... on User { id databaseId login } ... on Bot { id databaseId login }
  ... on Mannequin { id databaseId login } ... on Organization { id databaseId login } } }
```

Issue fields per alias as Step 3 planned them (100 aliases cost 1 point with labels the only
connection); Step 4a completes this document under D29 and measures it, since more connections
raise the cost (5.1, Step 4a):

```graphql
id number fullDatabaseId state stateReason(enableDuplicate: true) title body createdAt updatedAt closedAt
author { ...Actor }
labels(first: 100) { totalCount pageInfo { hasNextPage endCursor } nodes { name } }
```

Overflow continuation, one alias per (item, connection), several per request; each continuation
selects the same nodes as its connection's first page above (the example shows the Step 2 review
nodes):

```graphql
o0: node(id: $id0) { ... on PullRequest { headRefOid updatedAt
  reviews(first: 100, after: $after0, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) {
    totalCount pageInfo { hasNextPage endCursor } nodes { fullDatabaseId state author { ...Actor } } } } }
```

User lookup (5.2; 100 ids per request; cost 1 with the Step 3b-1 fields and D31's profiles, dry
run 2026-10-18):

```graphql
nodes(ids: $ids) { __typename id
  ... on User { databaseId login name createdAt updatedAt company location websiteUrl }
  ... on Bot { databaseId login createdAt updatedAt }
  ... on Organization { databaseId login name createdAt updatedAt description email location websiteUrl
    twitterUsername isVerified archivedAt hasSponsorsListing pinnedItemsRemaining }
  ... on Mannequin { databaseId login name createdAt updatedAt claimant { databaseId } }
  ... on EnterpriseUserAccount { login name createdAt updatedAt user { databaseId } } }
```

Rate-limit reading for `ghactivity github rate-limit` (Step 3a; REST `/rate_limit` supplies each
pool's limit and use):

```graphql
query RateLimit { rateLimit { cost remaining resetAt } }
```

Verify's timeline lookup (Step 3b-2b-2) reads, for each differing PR, its ready-for-review,
convert-to-draft, connected, and disconnected events at or after the repository's boundary. It
passes `since` one second before the boundary and counts an entry only when its `createdAt` is at or
after the boundary: `since` keeps only items created after it, the event times are whole seconds
(section 7, "Timeline `since`"), and so is the boundary (5.3 step 2). It reads each PR through
`repository(owner:, name:)` and `pullRequest(number:)`, so a null `repository` is the repository
error 5.4 names. It pages by `pageInfo.hasNextPage` and checks the entries it read against the
first page's `filteredCount`, which applies `itemTypes` and `since` (measured; GitHub's schema
describes it as the count after applying `before` and `after`, and names neither); a later page's
`filteredCount` counts only the items after its cursor, and `totalCount` is neither filtered nor
the number of entries GitHub returns (section 7, "Closing-link timeline events"). A lookup whose
entries differ from that count is retried once alone, as an item error is (5.3 step 4), and a
difference that remains makes its item an error, as a value the typed models reject does (5.4,
D37). Its document, formula cost, and recordings are added here when it
is written; the recorder adds them without changing the existing recordings, and its default mode
and `--only documents` record them too, so a re-recording keeps them (gap 96). The document takes
no `tests/query_versions.json` entry: verify stores nothing, so no `hydration_version` depends on
it, and once it changes the replay fails with "re-record" on its recordings, whose stamp is the old
query hash (Step 2).

Default-branch history (Step 4b; 100 commits per page cost 1, dry run on Prebid.js, 2026-10-18):

```graphql
repository(owner: $owner, name: $name) { defaultBranchRef { name target { ... on Commit {
  history(first: 100, since: $floor, after: $after) { totalCount pageInfo { hasNextPage endCursor }
    nodes { oid authoredDate committedDate additions deletions changedFilesIfAvailable
      parents { totalCount } author { name email user { id databaseId login } } } } } } } }
```

Repository activity (Step 4b; REST `GET /repos/{owner}/{repo}/activity?ref=refs/heads/<default
branch>&per_page=100`, newest first, paged by the cursor in each response's `Link` header) and,
for each event whose `before` and `after` are both non-zero, REST
`GET /repos/{owner}/{repo}/compare/{before}...{after}`, paged like the commit-list compare (5.3
step 4). Their REST cost is measured in Step 4b.

`Actor` selects `__typename login` plus `id databaseId` on `User`, `Bot`, `Organization`, and
`Mannequin`, and from Step 3b-1 `id user { databaseId }` on `EnterpriseUserAccount`, which has no
`databaseId` (5.2). `Actor` is spread only in fields GitHub types as `Actor`: spread in a field typed
`User`, it made GitHub demand the `read:enterprise` scope for `EnterpriseUserAccount.id` (dry run,
section 7, "Collected fields"). A continuation
includes the `Actor` fragment only when its connection uses it: GitHub rejects a document that
defines a fragment it does not use. Do not nest a
connection inside `commits` (for example `authors(first: N)`); GraphQL charges for every possible
nested node.

## 6. Execution steps

Each step is one pull request from a `feature/<slug>` branch, except Step 3 (pull requests 3a, 3b-1,
3b-2a-1, 3b-2a-2a, 3b-2a-2b-1, 3b-2a-2b-2, 3b-2a-2c, 3b-2a-2d, 3b-2a-3, 3b-2b-1, and 3b-2b-2), Step
4 (pull
requests 4a and 4b),
Step 5 (pull request 5, and 5b when its measurements call for the 5.3 step 3 rule or the retry
delay Step 5 names), and Step 6 (pull requests 6a and 6c, with the 6b manual runs between them). A step, or a pull
request within one, starts after the previous one merges.

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
real all-skipped pytest run; and the suppression counter.

Acceptance: CI passes on the exact PR head with a non-empty job list.

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
invariant check without a test that targets it; the lock check runs on the exact PR head; CI passes
on the exact PR head.

### Step 3: New schema, PR sync engine, cutover

Eleven pull requests. 3a lands the schema and removes the old pipeline, which imports the old models
and so cannot outlive them; 3b-1 adds what the sync collects under D29 (the query documents, typed
records, schema, and row-level views of D3-D6, D8, D14, and D28); 3b-2a-1 adds the planner and the
configuration it reads; 3b-2a-2a adds the writer and the run records on that schema, so the writer
is built against the final tables; 3b-2a-2b-1 adds the scenario model the engine's tests run
against; 3b-2a-2b-2 adds the sync engine over the planner and the writer, `--dry-run`
included; 3b-2a-2c adds `ghactivity sync`, the seeded runs, and the live acceptance; 3b-2a-2d
replaces the tests that planted inputs (D39); 3b-2a-3 applies D37 to the sync (D38); 3b-2b-1 adds
the REST sampler that checks what the sync wrote; and 3b-2b-2 adds verify, whose live acceptance
runs the sampler. "Step 3b-2" elsewhere in this plan means 3b-2a-1, the parts of 3b-2a-2, 3b-2a-3,
and the two parts of 3b-2b together; "Step 3b-2a" means 3b-2a-1, 3b-2a-2, and 3b-2a-3; "Step
3b-2a-2" means 3b-2a-2a, the two parts of 3b-2a-2b, 3b-2a-2c, and 3b-2a-2d; "Step 3b-2a-2b" means 3b-2a-2b-1
and 3b-2a-2b-2; and "Step 3b-2b" means 3b-2b-1 and 3b-2b-2. The REST sampler and its recorder mode
are 3b-2b-1's; verify (5.4), its timeline lookup, and the scenario model's timeline responses are
3b-2b-2's; the planner, its snapshot reader, step 7's candidate, guard, and `--allow-deletions N`
functions, and the `tracking.toml` keys the planner reads (`[coverage]` and `[sync]`'s
`unsettled_minutes`, `delete_guard_min_rows`, and `delete_guard_percent`) with their loader and the
file's two comment changes are 3b-2a-1's; `[automation]` and `[sync].accepted_failures` with their
loading, every write the sync makes, run records, the run lock, the head check, and the busy-timeout
comment are 3b-2a-2a's; the scenario model and its round-trip test are 3b-2a-2b-1's; the command
with its usage errors and the runs that never start, the seeded runs, the go-gdpr recordings, and
the live acceptance are 3b-2a-2c's; the D39 test replacements are 3b-2a-2d's; the changes D38
makes to the sync, its exit code included,
are 3b-2a-3's; and the rest, `--dry-run` and the exit code of a run that starts included, is
3b-2a-2b-2's. Until 3b-2a-2c `main` has no `sync` command and the tracked database
stays frozen (D26). No sync has written the new schema before 3b-2a-2, so 3b-1's changes touch no
stored data: the tracked `github_activity.db` holds the first schema's data, which D18 rebuilds from
GitHub rather than migrating (Step 5). Prerequisites for 3a: the gap 68 counter follow-up and the
httpx declaration (gap 76) have merged.

Every Step 3 test of a GitHub interaction runs through the real `GitHubSource` over
`httpx.MockTransport`. Databases come from one template per test session, built by
`alembic upgrade head` and copied for each test; `metadata.create_all` is used only by the 5.1
schema comparison. Evidence is direct, with inputs at and on each side of every boundary the design
states (the coverage floor, the 10-minute unsettled window, 10 minutes past `deadline_at` for the
lock, the 24-hour refresh, the delete guard's max(25, 2%)) and regression tests that fail against
the parent's code. D39 governs every test. Nothing is planted or tampered, no flaw is seeded, and
there is no mutation testing, under any name. No test, review, or acceptance changes a file or a
copy of one (other than a stub or configuration the inputs below allow), or writes a file, a row, a revision, a call
site, or a decoy holding a flaw, to see whether something catches it, and none measures how many
such flaws are found. A test's or an acceptance's inputs are GitHub's recorded responses; stubs of a
response GitHub or the environment can send (a status, a body, a timeout, an interrupt, a failing
`git`, a clock), which a test may build in memory from a recorded unit changed into that shape;
calls the code under test must refuse, and configuration it must refuse or that names a state GitHub
can produce (a repository that does not resolve, one no longer listed); valid rows a test writes
(through the models with `tests/db/seed.py`, or in SQL); and databases and files this project's own
code wrote, at any revision. Each test asserts the exact result. A check over stored data, files, or
source that only a defect of this code could fail is tested on its passing side, with the exact
count of the rows, files, or values it compared, so it cannot pass on nothing; a check that GitHub,
the environment, or a caller can fail is tested on both sides. A fix comes with a regression test
that fails against the parent commit's code. Tests merged before D39 that change a file or a copy of
one, or write a file, a revision, a call, a connection, or a decoy holding a flaw, to see whether a
check catches it are replaced in Step 3b-2a-2d. Each wait on a thread or subprocess has an explicit
timeout.

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
`b3c4d5e6f7g8` and on a scratch copy of the tracked file; the migration connection reporting
`foreign_keys=0`; a batch alter of `pull_requests` keeping its child
rows, through env.py with a test-only revision that drops and recreates the views; env.py refusing
the tracked file; `downgrade()` raising; `PRAGMA user_version` holding `schema_version`, the
`schema_version` lock and the CI lock step; the revision
tuple equal to the chain; the schema comparison (5.1), CHECK
names included; every foreign-key column leading an index; the timestamp type rejecting a naive
value and converting an aware one to UTC; view rows, on seeded data, for a null-author PR, a null
reviewer, `final_verdict` (a later `DISMISSED`, a later `COMMENTED`, order by review id), and
`v_pr_commenters` excluding reviewers; the dictionary in both directions; only `source/` importing
githubkit, and `source/` importing neither SQLAlchemy nor `db/`; `scripts/check_frozen_db.py` on
the pinned file; `LOGGING__ROTATION` overriding `Settings().logging.rotation`;
a usage error, an invalid `tracking.toml` (printing no traceback), and a missing token exiting 1
through the installed entry point; an unhandled exception through the installed entry point printing
no local variable values, and every loguru handler configured with `diagnose` off (gap 75);
`github rate-limit` and `github test`; the placeholder check;
the default `DATABASE_URL` and `.env.example` naming `./github_activity.local.db`; and the anchored
`.gitignore` exception (`github_activity.local.db` and a `github_activity.db` in a subdirectory
ignored).

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no
invariant check without a test that targets it; the parent's and the head's test counts are
reported with the removed modules listed; `alembic upgrade head` succeeds on a scratch copy of the
tracked file and is refused on the file itself; CI passes on the exact PR head.

#### Step 3b-1: Collection schema

Scope: the 5.8 pull request, PR index, resolve, and user-lookup documents with the fields and
connections Step 3b-1 adds (the index gains `closedAt`, which the coverage rule reads for a closed
PR, and the lookup gains D31's profile fields); the `account_profiles` table; their typed response models and the typed records out (a closed PR with
`closed_at`, the head and merge commits, milestone, review decision, review requests,
assignees, label ids, user-linked closing links, each review's and comment's added fields, the
inline-comment count, the commit link's account id, and an `EnterpriseUserAccount` actor with its
node id, editors, the merge state and stack, and the repository's added fields); the pull-request `hydration_version`
2 and its query-lock entry; the recordings of the changed documents (index units included)
re-recorded from public repositories, leaving the other units unchanged. A migration for the 5.1
changes, which refuses a database holding rows in any table it adds a column to (5.1): the added columns and the `repo_labels`, `pr_review_requests`,
`pr_assignees`, and `account_profiles` tables; `state` admitting `CLOSED`; `github_users` admitting `EnterpriseUserAccount`
with a null `github_id`; `sync_runs.halt_reason`, which nothing defined, dropped and
`config_source` and `config_sha256` added; `run_id` on the automation tables; `deleted_at` renamed
`not_found_at` (gap 89); the row-level views
in place of `v_pr_reviewers` and `v_pr_commenters` (gaps 81-83), `v_account_profiles`, `v_github_users` and
`v_commit_authors` recreated as 5.1 defines them, and `schema_version` 2, with its
lock entry and view digests (gap 87). The data dictionary for every new and changed table, view,
and column, with gap 86's consumer columns and gap 89's wording; `cli/github.py` printing that GitHub
returned no author instead of "(deleted account)", and the `source/` docstrings in `convert.py`,
`users.py`, and `records.py` describing a null actor or `NOT_FOUND` as GitHub reports it;
`docs/database.md`'s query example rewritten for the new views. No sync code: nothing writes the
new columns until 3b-2a-2.

Tests: each document's formula cost pinned (2 for the batch) and each changed document's recordings
round-tripping through the replay; every added field parsed from a recording, and a value of the
wrong type rejected by the typed models; an `EnterpriseUserAccount` actor kept as an account, where
`6fedb05` failed the item; a `CLOSED` PR returned with its `closed_at`, and the index returning
`closedAt`; overflow and a short page on each of the three added connections, a duplicate assignee,
and a user-linked closing issue absent from the full list, each failing the item; the migration
refusing a database at `33f8570f6540` with a `pull_requests` row, a `repositories` row, a `github_users` row, and a `sync_runs` row, and upgrading one without; the 5.1
schema comparison, CHECK names included; each
view on seeded rows that include a PR author's own review, a `Bot` reviewer, an account listed in
`[automation]`, a null reviewer and a null commenter, and a commenter who reviewed, every one present
with its columns, and an `EnterpriseUserAccount` identified by its `node_id`; `v_pr_relationships`
holding one row per source row, a team review request and a request with no reviewer told apart,
and a commit linked to no account told apart from one whose lookup returned `NOT_FOUND`, an edit
(`last_edited_at` set) with no editor kept as an editor row, a request row carrying its request ids, a
reference D28 kept carrying `*_reported` false, and a commit row an `[automation].emails` entry
matches carrying its `email_automation_reason`; the lock
with view digests; the dictionary in both directions.

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no
invariant check without a test that targets it, no consumer surface that does what D29 forbids,
and no computed column outside D29's list; the batch's `rateLimit(dryRun: true)` cost is 2 on the
exact head; CI passes on the exact PR head.

#### Step 3b-2a-1: Planner and configuration

Scope: `tracking.toml` gains `[coverage]` and `[sync]`'s `unsettled_minutes`,
`delete_guard_min_rows`, and `delete_guard_percent` (5.5), the keys the planner reads, with their
loader; its "Order is the sync order" comment changes to the resolve and index order (5.3 steps
1-2), and
its group comment calls a group a maintainer-assigned name stored as `repositories.repo_group`
(gap 89). In `sync/`: the snapshot reader, which reads the stored rows the planner needs without a
server-side cursor (gap 77), and the planner of 5.3 step 3, a pure function over those snapshots,
step 1's resolved repositories (each one's `github_id` and current labels by id and name, which a
dry run does not write), the fresh index, and the configuration. It applies the coverage rule
(section 3) and the hydration conditions, `hydration_version` compared by inequality, unsettled
re-hydration, the label-set check, the link conditions, and a `sync_failures` row among them. It
returns the item failures it detects itself (an in-coverage index entry whose number is stored
under another `github_id`, and one whose state its dates contradict), which 3b-2a-2b's engine
records at once, and derives the in-place updates, the queue order with `sync_pending`, and the
batches. Step 7's deletion candidates, the guard (max(25, 2%) of the
pre-run snapshot), and `--allow-deletions N` are functions 3b-2a-2b's engine calls. Nothing in this
pull request writes the database or sends a request.

Tests: direct tests of the planner over snapshots read from SQLite files built by `alembic upgrade
head` and seeded rows, with a fresh index built from recorded or synthetic index units. Cases:
- each hydration condition alone, and its absence: an item not stored; `updatedAt` differing;
  state differing; `hydration_version` below and above the code's (a revert, 5.1); unsettled, on
  each side of the 10-minute window; a label its repository no longer has, deleted and renamed
  (same label id), read from step 1's resolve while the stored `repo_labels` still lists it; a link
  `totalCount` differing, an indexed link not stored,
  a stored link not indexed when the index lists all of them, and fewer distinct links than the
  smaller of `totalCount` and 10; a `sync_failures` row;
- the coverage rule on each side of the floor for each state, a `CLOSED` PR's `closedAt` included;
- an index entry whose state its dates contradict returned as a failure, not planned even when
  another condition selects it, and never a deletion candidate;
- an in-coverage index entry whose number is stored under another `github_id` returned as a
  failure and not planned, the stored row of that number a step 7 candidate, and such an entry
  outside coverage no failure;
- a repository whose index is incomplete (a count mismatch after the rescan, or the deadline during
  its index) given no hydrations, in-place updates, or deletion candidates while a completely
  indexed repository's are planned, and a repository failed during hydration given no candidates;
- the in-place updates (`is_draft`, `author_association`) only for items not planned;
- a pending item none of the conditions selects left unplanned;
- the queue order (pending, then open, then `updatedAt` descending) across repositories, and each
  batch the first unbatched item and further unbatched items of its repository and kind, in queue
  order, up to the kind's batch size;
- step 7's candidates and the guard on each side of max(25, 2%), counted on the pre-run snapshot,
  with `--allow-deletions N` under, at, and over the candidate count, and the `sync_failures` rows
  of items absent from the in-coverage set listed for removal, under a halt too;
- the snapshot reader leaving no read half-consumed: after each read, a separate `sqlite3`
  connection commits without waiting (gap 77);
- config loading for the new keys (defaults, invalid values, unknown keys, a missing floor, a
  string floor, an integer floor (1672531200), a local date-time floor without an offset, and a
  floor with a non-UTC offset converted to UTC).

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no
invariant check without a test that targets it, and no decision that the planner or step 7's
candidate, guard, and `--allow-deletions N` functions make (5.3 steps 3 and 7) without a direct test
in this pull request; CI passes on the exact PR head.

#### Step 3b-2a-2a: Writer, run records, and their configuration

Prerequisite: Step 3b-2a-1 has merged.

Scope: `tracking.toml` gains `[automation]` and `[sync].accepted_failures` (5.5) with their
loading. The writer, applying 3b-2a-1's plan: the in-place updates, `sync_pending`, the item
failures the planner returns, recorded at once, and the deletions that 3b-2a-1's guard and
`--allow-deletions N` functions allow; the writes for untracked repositories and
`--prune-untracked` (D27); accepted failures recorded like any other; the automation tables written
from `tracking.toml`; the writes of user lookup and refresh results; run records, the run lock, and
the sync's head check (gap 74). `sync_failures.last_run_id` (migration `2d2ba76b177c`), by which
`attempts` counts runs (5.1). The busy-timeout comment in `db/engine.py` (gap 77). Writing what
3b-1 collects: closed PRs, review requests, assignees, repository labels, the `*_reported` columns
and `linked_github_id` (D28), `config_source` and `config_sha256` (gap 88), and `account_profiles`
from each lookup and refresh (D31). Each write is a function 3b-2a-2b's engine calls on the
writer's one connection; nothing in this pull request sends a request, and no command calls the
writer.

Tests: the writer gets direct tests, over SQLite files built by `alembic upgrade head` and seeded
rows, with records built from recorded or synthetic units; the planner's are 3b-2a-1's. Cases: each
write rule of 5.1 and 5.3 step 6 on both sides (D28's kept references for each kind of reference,
GitHub's value replacing the others, child rows replaced, and the validated totals), a
`pr_user_tags` row surviving re-hydration and removed when its PR is deleted (D17), every item
write on the writer's one connection, a re-hydration that returns a null editor clearing the stored
one while a null author is kept, the merge state and stack written from a hydration, review
requests (a user, a team, a null requested reviewer) and assignees (a user and a bot) written and
replaced, an `EnterpriseUserAccount` actor stored, a default-branch change recomputing
`targets_default_branch`, an account seen in two shapes keeping its login until a lookup, a review
or comment id stored under another PR refused before anything is written, run lock and
`--break-lock` (the lock held by a separate `sqlite3` connection; a stale `running` row reclaimed
as `aborted`; a refused claim inserting no row and leaving the stale rows it found; the close of a
run whose row another process aborted leaving `aborted` and writing no outcome), the head check
refusing an older revision and an empty file and a missing path without creating a file,
`config_sha256` equal to the read file's digest with and without `TRACKING_CONFIG_PATH`, a `User`
profile and an `Organization` profile written from a lookup, a refresh replacing a changed profile
field with null, no profile row for a `Bot`, `Mannequin`, or `EnterpriseUserAccount`, a `NOT_FOUND`
lookup leaving the profile unchanged, an automation-list change with no hydration changing the
views, config loading for `[automation]` and `accepted_failures` (an accepted failure naming no
configured repository, a non-ASCII automation email), step 1 keeping two `is_tracked` repositories
from sharing a `full_name`, ignoring case, which 5.1 states and no constraint holds (a unique index
would make a rename swap a database error).

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no
invariant check without a test that targets it, and no write rule of 5.1, 5.2, or 5.3 steps 6-8
without a direct test in this pull request; CI passes on the exact PR head.

#### Step 3b-2a-2b-1: Scenario model

Prerequisite: Step 3b-2a-2a has merged.

Scope: tests and `docs/testing.md` only. A scenario model of repositories, PRs, and accounts that
renders resolve, repository-label, index, batch, overflow, compare, and user-lookup responses
through the Step 2 replay, each response's `Date` header the test clock's reading when the request
arrives, and that a test changes between renderings: the changes 3b-2a-2b-2's cases make, a null
repository and a `NOT_FOUND` item or account included. It renders only GraphQL documents it was
checked against, so the replay's stamp check still applies. The engine's tests share the replay's
pytest fixtures and a test clock they can advance; `docs/testing.md` describes the model. 3b-2a-2c
and 3b-2b-2 extend it as their tests need (expected states over several runs, timeline responses).

Tests: every recorded pull-request, index, user-lookup, overflow, resolve, and repository-label unit
parses into the model and re-renders to the recording's data and errors exactly, compared as
serialized JSON (headers differ by construction: the model's `Date` comes from the test clock); the
three recorded compare pages (1.47 MB) are compared on the fields the source reads, since exact
equality would make the model hold REST fields the source never reads. What the model renders,
before and after a change, parses through the real `GitHubSource` into records equal to the model's
state.

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no
invariant check without a test that targets it; CI passes on the exact PR head.

#### Step 3b-2a-2b-2: Sync engine

Prerequisite: Step 3b-2a-2b-1 has merged.

Scope: the engine, steps 1-8 of 5.3 over `GitHubSource`, 3b-2a-1's planner, and 3b-2a-2a's writer,
and `--dry-run` (5.3 step 8): untracked repositories and `--prune-untracked` (D27); accepted
failures (5.3 step 8, 5.5); user lookup and refresh, gap 90's lookups included; the engine checks
of 5.3 step 5 and gap 78; the failure classes of 5.3 step 4; and the deadline. The engine is a
function `ghactivity sync` (3b-2a-2c) calls with the run's options. The `DeadlineReached` docstring
in `source/errors.py`, which says the run drains, follows 5.3 step 4.

Tests through the sync service with a real SQLite file built by `alembic upgrade head`
and 3b-2a-2b-1's scenario model. Each case runs the engine and checks the stored rows, the
`sync_runs` row, each `repo_sync_state` row, and the exit code. The planner's and the writer's own
rules are tested in 3b-2a-1 and 3b-2a-2a; these cases show the engine joins them. Cases:
- lifecycle: first sync, update, merge, close (stored as `CLOSED`), reopen, a renamed repository
  followed, a renamed label (same label id), a version bump re-hydrating all, unsettled
  re-hydration, queue order (pending, then open, then `updatedAt` descending), ready-for-review and
  author-association changes from the index (never over a hydrated write), a closing link added
  after merge, a closing link listed twice in the index, a PR with more than 10 links, an item
  leaving coverage between index and hydration skipped without a failure, the `sync_runs` counters,
  and view rows after a sync;
- tracking: an unresolvable repository; a stored repository whose resolve failed and one the
  deadline kept from resolving; two entries resolving to one `github_id`; `repositories.resolved_at`
  from the resolve's `Date` header, unchanged by a failed resolve; a `--repo` run writing no
  `repo_sync_state` for another repository; untracked repositories and the prune (no marking under
  `--repo`, a re-added repository re-tracked and not pruned, a refused prune deleting nothing with
  exit 2, a prune over two runs);
- deletions: a mass-delete halt and its override; `--allow-deletions` naming a repository by its
  configured name after a rename while another repository halts (`deletions_approved` names only the
  approved one, with its `github_id`; the halting one keeps its candidates and records its halt);
  the incomplete-index halt; the guard counted on the pre-run snapshot; `sync_failures` rows leaving
  with their items, under a halt too; a `CLOSED` PR on each side of the floor, in the planner and
  step 7;
- failures: each failure the engine detects itself (5.3 steps 3 and 5) recorded at once without a
  hydration and cleared once the item hydrates; a failed user lookup whose solo lookup succeeds, and
  one whose solo lookup fails; identity conflicts (a lookup `databaseId` other than the link's, a
  review id stored under another PR, a PR number stored under another `github_id`); a duplicate
  repository label id; a repository gone during hydration; a database error in the writer and any
  other exception after the claim, each closing the run `failed`; an `AccountMissing` refusal
  recorded as an item failure, and no other engine
  test leaving one in `sync_failures`;
- accounts (D28): a bot seen first through a commit; an account deleted after it was stored
  (`not_found_at` set; its references kept through a re-hydration that returns it as null, each
  `*_reported` false and the commit's `linked_github_id` null), then seen again; an account deleted
  before it was stored; an object first written after its author's deletion; a commit link replaced;
  the 24-hour refresh on each side of its boundary, `refreshed_at` from the lookup's `Date` header,
  skipping an account with `not_found_at` set; a commit link to such an account looking it up
  (gap 90);
- stopping: the deadline before any resolve, during an index (a middle repository's included),
  during hydration (a time-limited run, then a run that finishes its `sync_pending` items), and
  during the refresh; a pending item whose cause clears; an in-process interrupt during a request
  closing the run `aborted` with 130 and each attempted repository's `last_outcome` `failed`, then a
  resume with committed items intact; a fatal error in the middle of a batch, whose unwritten items
  the next run plans again; a fatal and an interrupted run after step 7 keeping step 7's
  `halt_reason`; a run whose row another process aborted exiting 1 without overwriting `aborted`;
  the head check before the claim and before any request (a missing path: no request, no file
  created, no row);
- exit codes: one case per exit condition of 5.3 step 8, with the status, each `last_outcome`, and
  `last_index_started_at` (written for a time-limited repository, not for a failed, incompletely
  indexed, or gone one); an accepted failure leaving exit 0; an `accepted_failures` entry whose item
  did not fail reported, and not reported when its repository's outcome is another or none, or its
  item is pending;
- `--dry-run`: writing nothing (the file's bytes unchanged); for each step-8 condition steps 1-3 can
  show, the run's exit code on the same scenario, and without a halt the delete candidates the run
  deletes; its report; no lock taken while a run is `running`, and its reads beside a sync's commits
  not blocking them (gap 77); the head check before step 1; the deadline during its index.

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no rule
of 5.2 or 5.3 this pull request implements without a direct test, and no invariant check without a
test that targets it; CI passes on the exact PR head.

#### Step 3b-2a-2c: `ghactivity sync`, seeded runs, and live acceptance

Prerequisite: Step 3b-2a-2b-2 has merged.

Scope: `ghactivity sync` (5.7) over 3b-2a-2b's engine, `--dry-run` included: its options, the usage
errors and the runs that never start of 5.3 step 8, the process exiting with the code the engine
returns, and the report a dry run prints. The seeded scenario runs below, and recordings made for go-gdpr's first sync.

Tests: `ghactivity sync` exiting 2 through a child interpreter that runs the installed package
(`python -I -c`, the Step 3a pattern) with the replay patched in, while a usage error still exits
1, as a refused claim does with no `sync_runs` row; crash mid-run then resume with committed
items intact, by a child process that sends itself SIGKILL at a set point under
`subprocess.run(timeout=...)`, which leaves its row `running`. One test runs a first sync of go-gdpr
from recordings made for it, without the model, and checks exit 0 and each stored item's child rows
against the recording's totals and node lists. A few fixed seeds each drive several runs over random
GitHub changes, and after each run the database equals an expected state computed separately from
the scenario model, compared on every column except row ids, the times the sync stamps from `Date`
headers, and the run counters; the expected state applies D28's kept references and the refresh from
the model's own history, not from the writer's code.

Acceptance: `sync --repo` on Prebid.js, salesagent, and prebid-server, from a fresh database at an
explicit path, with `GITHUB_TOKEN="$(gh auth token)"` and the token never printed; it reads
`ghactivity github rate-limit` before and after, does not start with fewer than 1,500 GraphQL
points remaining, runs a second, incremental sync, and records the points, REST calls, and time it
spent. Each run exits 0; a live item failure that cannot pass is listed in
`[sync].accepted_failures` with REST evidence of why, as in Step 5, which leaves the exit 0 (5.3
step 8). On the resulting file, SQL queries recorded in the pull request show every stored pull
request's child rows equal to its stored totals (reviews, comments, commits, files, labels,
closing issues, user-linked closing issues, review requests, assignees), its `v_pr_relationships`
rows equal to the rows 5.1 lists for it, and every stored repository's `repo_labels` rows equal to
its `labels_total`. If a merge to an open PR's base branch by someone else falls between the two
syncs, the pull request records that PR's `updatedAt` and `mergeStateStatus` from both (section
8). An adversarial review of the change, by reviewers who did not write it, finds no invariant
check without a test that targets it; CI passes on the exact PR head.

#### Step 3b-2a-2d: Tests without planted inputs (D39)

Prerequisite: Step 3b-2a-2c has merged, and D39 is on `main`.

Scope: tests and `docs/testing.md`; nothing under `src/`, `scripts/`, or `alembic/` changes, and no
rule loses its test. Every test merged before D39 that changes a file or a copy of one, or writes a
file, a revision, a call, a connection, or a decoy holding a flaw, to see whether a check catches it,
is replaced under D39: a check that GitHub, the environment, or a caller can fail keeps a refusing
side built from D39's inputs, and a check that only a defect of this code could fail is tested on
its passing side, with the exact count of what it compared. Among them, by file:
`tests/db/test_schema.py` (changed copies of a migrated database); `tests/db/test_migrations.py` (revisions holding a failure, a foreign-key violation, a `COMMIT` or
`END`, a statement the authorizer refuses, an `ATTACH`, or a batch alter that leaves a view in
place; the revision-rule checker's planted files; the connection put inside a transaction; the
copies changed for the definition reading); `tests/db/test_tracked_database.py` (the frozen file's
changed copy); `tests/db/test_views.py` (indexes dropped on a copy, and a row id projected under a consumer name);
`tests/sync/test_snapshot.py` (a server-side cursor left half-read); `tests/sync/test_engine.py` (a
writer patched to raise `AccountMissing` or a `RuntimeError`, a prune patched to raise a `RuntimeError`, and
`alembic_version` set to a revision that does not exist); `tests/sync/test_runs.py` (`user_version`
set on a migrated copy); `tests/sync/test_writer.py` (a wrong `targets_default_branch` written into
a row); `tests/test_cli_sync.py` (`alembic_version` set to `'older'`); `tests/test_cli_app.py` (the
command written to raise `boom`); the schema-version lock, the query lock, and the CI lock step on
edited or removed entries (`tests/test_data_dictionary.py`, `tests/test_check_query_lock.py`, and
`tests/source/test_recordings.py`'s edited query documents); and the planted calls, spellings,
decoys, alias files, subprocess calls, and package copies of the checkers' tests
(`tests/test_check_log_placeholders.py`, `tests/test_count_suppressions.py`,
`tests/test_check_any_aliases.py`, `tests/test_suite_rules.py`, `tests/test_import_boundaries.py`,
`tests/test_server_side_cursors.py`, `tests/test_declared_dependencies.py`, and
`tests/test_wheel_smoke.py`). A file at an older schema comes from a real earlier revision, as
`tests/sync/test_runs.py` builds one at `33f8570f6540`. env.py's refusal of a file that already
holds a foreign-key violation is tested on its passing side: a command over a file of seeded rows
succeeds, and the test counts the rows of that file the check read. These are D39 inputs and keep
their tests, renamed where a name says planted: a recorded unit `replay.modify` changes into a shape
GitHub can send (`FAIL_ONE_ITEM`), a kill or an interrupt at a set point, an exception a caller
raises in a transaction body (`tests/sync/test_connection.py`), the CLI's exit-code test commands,
valid tables in a scratch database (`tests/test_data_dictionary.py`'s `planted.db`), the tracked-file
check's WAL, non-database, symlink, and sidecar inputs. `tests/source/test_recorder.py`'s file-size
limit patched to a small value is replaced by a recorded unit given, in memory, a body over
`MAX_FILE_BYTES`.
`docs/testing.md` drops its descriptions of the replaced tests.

Tests: each rule a replaced test held has a test D39 allows, on both sides where GitHub, the
environment, or a caller can fail it, or on its passing side with the exact count it compared; the
pull request lists each test it replaces or removes, with the rule it held and the test that now
holds it.

Acceptance: the pull request lists every test under `tests/**/*.py` that writes a source file,
a revision, a row, a database, or a package copy, changes a recorded unit, patches a call or a
constant, or runs a call or a query holding a flaw, with the D39 input it uses or the test that
replaced it; a search of `tests/**/*.py`
(recorded GitHub text under `tests/fixtures/` is outside it) for `plant`, `tamper`, `decoy`,
`corrupt`, and `mutat` finds none; an adversarial review of the change, by reviewers who did not
write it, finds no rule that lost its test and no test that holds a flaw; CI
passes on the exact PR head.

#### Step 3b-2a-3: D37 in the sync

Prerequisite: Step 3b-2a-2d has merged.

Scope: the sync rules D38 changes, with a migration that adds `sync_runs.refresh_failures`,
`link_lookups`, `links_not_found`, `repo_filter`, and `entries`, `unknown` to the `trigger` CHECK, and the `account_node_ids`
table, and refuses a file whose `sync_runs` or `github_users` holds rows (5.1, gap 110; a
development file is rebuilt by a sync), with their data dictionary entries and the current
schema-version lock entry's consumer pairs:
- a failed 24-hour refresh is counted (accounts), listed in the report, and makes the exit 2
  unless an account entry accepts it (5.3 step 4, gap 98);
- a run that reaches its deadline exits 2, and the failures a batch made final before the deadline
  are recorded, the hydrator handing them over with the deadline (5.3 steps 6 and 8, gap 99);
- a hydration that contradicts the run's own index (an alias `NOT_FOUND`, an item outside coverage,
  an `updatedAt` earlier than the index entry's) is requested once more alone by the engine and
  then is an item failure (5.3 steps 3, 4, and 5, gap 100);
- an `accepted_failures` entry names the error class and the cause it accepts, and may name an
  account, by `github_id` or `node_id`, for a refresh failure; every failure the sync records puts
  its cause on its first line and what changes between runs on the lines after it; `tracking.py`
  keeps the list of recorded failure classes, and the index contradictions take the class
  `IndexContradiction` (5.3 step 5, 5.5, gap 101);
- a PR whose link `totalCount` exceeds the 10 links the index carries is hydrated every run (5.3
  step 3, gap 102);
- every node id a sighting, a lookup, or a commit link reports for a stored account is stored in
  `account_node_ids`, and a commit-link lookup answered `NOT_FOUND` is counted in `links_not_found`
  beside `link_lookups` (5.2, gap 103);
- the source checks of gap 104: a 2xx REST response whose body is JSON and whose `Date` is readable
  but without readable rate-limit headers is not understood, a GraphQL `errors` whose value is not a list, `null` included, is a server error, a
  compare above 250 commits holds the commit connection's first page at positions
  `total_commits - 250` to `total_commits - 151`, in order, and every index page selects the
  repository's `databaseId` and is checked against the resolved `github_id` (no `hydration_version`
  changes; the index document changes, so the GraphQL recordings are re-recorded with `--only
  documents` and the first-sync directory with `--only first-sync`, the recording splitter and the
  replay keep the repository's `databaseId` beside `pullRequests` in an index unit, and the tests
  that assert recorded values follow the new recordings);
- before the commit-page rule is fixed, the GraphQL first page and the compare of every in-coverage
  PR above 250 commits in the three Step 3b-2a-2c acceptance repositories are read live and the
  rule checked on each; section 7 records how many hold merge commits, whether each page is
  compare's positions `total_commits - 250` to `total_commits - 151` in order, and the points and
  REST calls spent, and if any is not, the rule is chosen from that measurement before the step
  merges;
- the run-record and setup checks of gap 105: `code_version` as 5.1 defines it, with `_git`
  returning the exit status; a close given an outcome it cannot write raises before writing;
  `[automation]` is required; the connect listener reads `foreign_keys` back and refuses a
  connection where it is off or where the read-back returns no row (SQLite built without
  foreign-key support); `alembic/env.py` refuses a `stamp` that would change the revision;
  the wheel check compares the packaged `tracking.toml` bytes with the source file's; the log-file
  handler receives every record at its level; `sync_runs.repo_filter` records `--repo`; and
  `trigger` records `unknown` for a `GITHUB_EVENT_NAME` other than `schedule` and
  `workflow_dispatch`;
- the data dictionary describes the head-repository, milestone, and team names as of
  `hydrated_at` (gap 108);
- `docs/development.md`'s list of sync's exit-2 conditions gains the deadline, a failed refresh,
  and an index contradiction;
- the fixtures these rules break: the scenario model renders `x-ratelimit-*` headers on REST units
  and the repository `databaseId` on index pages, and renders a pull request it made above 250
  commits as GitHub does (section 7, "Commit cap"): its `commits` connection keeps `totalCount`
  and pages over the newest 250, so its first page is compare's positions `total_commits - 250` to
  `total_commits - 151`, while a recorded pull request keeps its recorded page; `docs/testing.md`
  and the model's refusal of a push to Prebid.js #8858 (`tests/source/scenario.py`,
  `tests/source/test_scenario.py`) say that its GraphQL page is compare's positions 4-103, in
  order, recorded separately, so a push cannot keep both; `tests/sync/expected_state.py` computes
  `account_node_ids` from the model's history, adds `refresh_failures`, `link_lookups`, and
  `links_not_found` to its run counters, and computes each run row's `repo_filter` and `entries`;
  every test configuration gains `[automation]`;
  every `accepted_failures` entry in the tests and in the packaged file's comment gains `error` and
  `message`; `tests/test_cli_sync_report.py` follows the account and message forms.

Tests, each rule on both sides with inputs D39 allows: a refresh request that fails and one that
succeeds, a refresh answer with another `databaseId`, an accepted account entry (exit 0) by
`github_id` and by `node_id`, and an account entry the refresh did not reach, not reported as not
having failed; `refresh_failures` counting each failed refresh, accepted ones included; a run that
finishes before its deadline (exit 0) and one whose deadline stops hydration, the refresh, or both
(exit 2); a deadline inside a batch after one item's failure became final, which is recorded while
the rest wait in `sync_pending`; a `NOT_FOUND` alias for a number the index listed that the retry
clears and one it does not, then a later run whose index omits the number and whose step 7 removes
the item and its failure; a hydration outside coverage that the retry clears and one it does
not; a deadline that refuses the retry, leaving the item in `sync_pending`; a hydrated `updatedAt`
earlier than, equal to, and later than the index entry's, the earlier one cleared by the retry and
not; an accepted entry whose class and cause match, one whose class differs, one whose cause
differs, and one that still matches a failure whose request id, counts, and typed-model text
differ between two runs; a loader refusing an unknown class and an entry without `message`, and
the recorded-class list equal to the classes the engine records; PRs with 10 and with 11 links; a
second node id from a sighting, a lookup, and a REST compare link stored and seen again in a later
run, keeping `first_run_id` and advancing `last_run_id`, and a node id in `account_node_ids`
reported under another `databaseId` failing the item; a commit-link lookup answered `NOT_FOUND`
counted in `link_lookups` and `links_not_found`, and one for an account an earlier run's lookup
answered `NOT_FOUND` counted in neither; the report listing each index contradiction with its kind
and whether the retry cleared it; `entries` recording each configured entry's `github_id`, a
renamed repository's included, null for one that never resolved, and the stored row's
`github_id` for an entry that resolves to another; each source check accepting the shapes GitHub's recordings hold and refusing each shape
its rule names (a 2xx REST response whose body is JSON and whose `Date` is readable but without
rate-limit headers; an `errors` of `{}`, `""`, and
`null`; an index page whose repository `databaseId` differs; a commit page from other positions; each built in memory from a recorded unit with
`replay.modify` or by the scenario model's `set`),
the commit page at positions 4-103 of the recording of Prebid.js #8858 accepted, and a 502, a 200
whose body is not JSON, and a 200 without `Date`, none with rate-limit headers, still retried; `code_version` outside any `.git` (the package
version), in a checkout with the package untracked (the package version), in a clean checkout,
one holding only an untracked file, and a dirty one (the commit, the commit, and `-dirty`), and
with `git status`, `ls-files`, and `rev-parse` failing or timing out inside a checkout
(`-unknown`); `trigger` for `schedule`, `workflow_dispatch`, another event name, and none; a close
given an outcome with no stored row leaving the row `running`; a `stamp` to another revision and a
`stamp --purge` at head refused with nothing committed, and a `stamp` at head accepted; the
migration refusing a populated file; a connection whose `foreign_keys` read-back returns no row refused, through a fake DBAPI connection
that answers `PRAGMA foreign_keys` with no row, as SQLite built without foreign-key support does
(an environment stub), and one that reads back 1 accepted; the
wheel check reporting the SHA-256 of the packaged `tracking.toml` it compared, equal to the source
file's; a record at the log file's level reaching it and one below not; `repo_filter` written for
a `--repo` run, sorted, with each value as given, and null without one; a file without
`[automation]` refused; and the report listing failed refreshes and commit links answered
`NOT_FOUND`. Each rule's refusing side is a regression test that fails against the parent's code;
its accepting side passes on both. The tests that pinned the old behaviour change with the rule
they pinned: in `tests/sync/test_engine.py` every test that asserts a `time_limited` run exits 0,
the deadline inside a batch among them, whose failures are now recorded, and the failed-refresh,
gone-item, leaving-coverage, and 11-link tests; in `tests/sync/test_runs.py` the skipped-close,
`trigger`, and `code_version` tests; and in `tests/test_tracking_config.py` the defaults without `[automation]`
and the accepted-failure entries.

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no rule of
this step without a test that targets it; CI passes on the exact PR head.

#### Step 3b-2b-1: REST sampler

Prerequisite: Step 3b-2a-3 has merged.

Scope: tests, `scripts/`, and `docs/testing.md`; `Transport.rest_get` already takes any path and
query, so nothing under `src/` changes. The REST sampler 3b-2b-2's acceptance uses, laid
out like the recorder: comparison logic in a
module under `tests/source/`, tested offline on REST and GraphQL units recorded from the same PRs,
each compared field matching, with the number of fields compared per PR asserted so a comparison
that reads nothing fails (D39), REST's `ghost` matching a null reference or a stored reference to an
account a lookup now returns `NOT_FOUND` for (the sampler looks it up), REST's 250 commits of a PR
above 250 compared as a subset of the stored commits with equal attribution, a PR whose REST
`updated_at` differs from the stored `github_updated_at` reported as changed and not compared, and
the per-file sum rule; the fields it compares listed in the module, the Step 3b-1 fields REST
returns among them (`closed_at`, the head and base commits, the merge commit, the milestone, review
times and commits, comment times, review requests, assignees) and those REST does not return named,
the stack fields among them; REST's `mergeable`, `rebaseable`, and `mergeable_state` are returned
but not compared, since the stored merge state is as of `hydrated_at` (5.1);
each REST list read 100 entries per page until a page holds fewer, since `rest_get` returns no
`Link` header (gap 96); the offline tests compare REST bodies with rows written from GraphQL units
recorded in the same recorder run as the REST units of the same PRs; REST bodies recorded as their own unit kind, each stamped as the other kinds are
(Step 2), by a recorder mode of their own that writes every unit of its run, the sampled PRs'
GraphQL units included, into its own directory beside `recorded/`, replacing that directory alone,
as the first-sync mode does, and that a document change re-runs; the offline tests replay that
directory alone, and the default mode and `--only documents`, which rebuild `recorded/`, leave it
as it is (gap 96); the mode refuses a repository the recorder's `PRIVATE` list names
before any request and resolves each other repository through the recorder's public-only source
before its first REST request (D33); the sampler takes its repositories with `--repo` and refuses
`prebid-agent-skills` before any request (D33); run from `scripts/`, reaching GitHub through
`GitHubSource.transport`; Steps 3b-2b-2, 4, and 5 reuse it.

Tests: the offline tests the scope lists; a stub REST body with the recorded `updated_at` and one
compared value GitHub changes without moving `updatedAt` (`author_association`, a label name)
reported as a mismatch with its PR, field, stored value, and REST value; a list read across pages, one of them ending on a full
page followed by an empty one; the recorder mode sending no request for a repository `PRIVATE`
lists and refusing a private repository before any REST request; a re-recording in the default mode
and with `--only documents` leaving the mode's directory, REST and GraphQL units, as it is; and the
sampler reading only the
repositories `--repo` names and refusing `prebid-agent-skills` before any request.

Acceptance: an adversarial review of the change, by reviewers who did not write it, finds no rule
of the sampler without a test that targets it; CI passes on the exact PR head.

#### Step 3b-2b-2: Verify

Prerequisite: Step 3b-2b-1 has merged.

Scope: `ghactivity verify` (5.4; `--db-only` is Step 6a) and its head check (gap 74), with D36's
lock check, repository set, report, and unverified repositories. Verify's timeline lookup in
`source/` with its recordings, which the recorder's default mode and `--only documents` also record
(5.8, gap 96). Any change 3b-2b-1's sampler needs on live REST bodies.

Tests through 3b-2a-2b-1's scenario model, extended to render the timeline lookup's responses from
each PR's draft and link event history, which the model gains here, rendering `since` and
`filteredCount` as section 7 measured them (entries created after `since`; each page's
`filteredCount` counting the matching entries after its cursor), with the replay matching a timeline
unit by its `since` (the timeline recordings join the model's round-trip test), and
fake GitHub: each verify class, each overlapping pair of classes taking the first in the 5.4
order, a known failure whose class an entry accepts but whose cause differs, reported as a known
failure and failing `--exact`, and one whose cause matches, passing it, a stale item, verify's repository set with and without `--repo` (a stored untracked
repository, a stored row no configured entry names, a renamed repository, and a renamed repository
whose configured entry fails to resolve, reported unverified and not untracked, and an entry that
resolves to a `github_id` other than its stored row's, with no stored `is_tracked` row reported
untracked), its child-count checks, each reporting the rows it compared, equal to the counts the
scenario model gives (D39), an unverified repository for
each failure 5.4 lists, one made unverified by a lookup by number and one by a timeline lookup, each
keeping the classes it already had and leaving unchecked the items no lookup had answered, those of
the batch the error ended included, an unchecked item and an unverified repository each making a
verify without `--exact` exit 2, a verify with every repository unverified exiting 1, a
repository's `sync_pending` rows replaced by a sync's close between verify's two reads and the item
still reported pending, a
`CLOSED` PR whose `closedAt` falls on each side of the
floor, verify's head check refusing an older revision, an empty file, and a missing path before
any request and without creating a file, verify exiting 1 and naming the run while a sync run
holds the lock (5.4), with a `running` row whose `deadline_at` is null, one in the future, one less
than 10 minutes past, and one 10 minutes past, which does not stop it, and the database file
unchanged by verify, a
verify started before a sync's claim reading beside the sync's item commits without blocking them
(gap 77), with the classes it reports when that sync writes a repository's items while verify
indexes it, a verify reaching `--max-minutes` exiting 1, and its report written on exits 0, 2, 1,
and 130 (the 130 from a real SIGINT to a child process, as for sync), marked incomplete where 5.4
says, with the source's points and REST calls and the clock's seconds; each cause 5.4 lists for a
verify that never starts exiting 1 before any request, with no report and PATH unchanged; a report
that cannot be written at exit turning 0 or 2 into 1 and leaving an interrupt's 130; and a closed
stdout leaving the exit code verify's. The
timeline lookup through the replay: a connected event naming the differing issue, one naming
another issue, a link difference made by a closing keyword with no event, a difference in the
link totals alone with enough connected events, with only a disconnected event, and with too few, a
synthetic
disconnected event, an event at the boundary counted and one a second before it not (under the measured `since`
and whole-second times, verify's own `createdAt` filter drops no entry the lookup returns, so it
is tested on its passing side, every returned entry counted), a stored link no longer indexed on a PR whose index lists every link matched only by an
event naming it, a filtered page whose `totalCount` differs from its entries, counted by its
entries, a lookup whose entries differ from its `filteredCount` after its retry and one whose
response the typed models reject, each an error, one that differs once and matches on its retry, classed by its events, and a
lookup
spanning two pages whose second page's `filteredCount` counts only the entries after its cursor
(5.8). A re-recording in the default mode and with `--only documents` keeping the timeline units.

Acceptance: `sync --repo` and `verify --exact --repo` on Prebid.js, salesagent, and prebid-server
(without `--repo`, verify would report the other configured repositories as not synced); a REST
sample of PRs that includes a null author, a null merger, a closed-unmerged PR, more than 250
commits, overflow, a duplicate path, and a dismissed review matches field by field under
3b-2b-1's sampler rules, with REST's `ghost` account (id 10137) matching a null reference or a
reference to an account a lookup now returns `NOT_FOUND` for,
per-file line counts compared only where REST's per-file sums equal the PR totals, and comment ids
and authors compared as D24 stores them (section 7, "Source vs REST"). The live run starts from a
fresh database at an explicit path, with `GITHUB_TOKEN="$(gh auth token)"` and the token never
printed; reads `ghactivity github rate-limit` before and after, and does not start with fewer than
2,500 GraphQL points remaining (the first sync of these repositories spent 1,687, section 7,
"Step 3b-2a-2c acceptance"); runs a second,
incremental sync before verify; and records the points, REST calls, and time it spent, verify's
from its report. The sample's cases are chosen by SQL queries recorded in the pull request; all
three repositories hold in-coverage dismissed reviews (section 7, "Step 3b-2a-2c acceptance"). The
pull request lists each
closing-link error verify reports, with its PR and link (for a difference in the totals alone,
found by reading the PR's full closing list; section 8). `verify --exact` exits 0, or
exits 2 with no failing difference but such closing-link errors, each with REST evidence that
GitHub changed the link without changing the PR's `updatedAt`. A live item failure that
cannot pass is listed in `[sync].accepted_failures` with REST evidence of why, as in Step 5. An
adversarial review of the change, by reviewers who did not write it, finds no invariant check
without a test that targets it; CI passes on the exact PR head.

### Step 4: Issues, closing-issue links, and default-branch activity

Two pull requests: 4a for issues and closing-issue links, 4b for default-branch activity (D30).

#### Step 4a: Issues and closing-issue links

Scope: a migration for the issue tables, completing the 5.1 issue rows as their Step 4a row lists;
issue index and hydration (5.8) with `stateReason` including `DUPLICATE`, collected under D29 the
way Step 3b-1 collects pull requests: issue comments one row per comment with their author and
times (no text, D24), assignees, milestone, issue type, the parent (an issue's sub-issues are the issues naming it) and `duplicateOf` issues by
repository id and number, and the free fields section 7's field rules admit, the document's cost
confirmed by `rateLimit(dryRun: true)` before it is fixed (by the formula, comments and assignees
raise a 100-issue batch from 1 point to 3, which may change the batch size); transfers (a transferred issue arrives with a new `github_id` and number,
step 7 removes the source row, and the old number returns `NOT_FOUND`); a view joining closing
references to stored issues, one row per link, a link to an issue the database does not hold
included; the data dictionary for issues.

Tests: issue first sync, update, close, and reopen; unsettled re-hydration and version bump for
issues; an issue's stored author kept when GitHub returns it as null (D28); issue comments and
assignees written and replaced; a transfer arriving with a new id; an old number the run's index
listed that hydration answers `NOT_FOUND`, an item failure until an index that omits it removes it
(5.3 step 4); a snapshot one migration behind head upgrades and syncs; verify's
child-count checks for `issue_labels`, `issue_comments`, and `issue_assignees`, each reporting the
rows it compared, equal to the stored totals (D39).

Acceptance: `verify --exact` for issues on the same repositories; a REST sample of issues matches
field by field.

#### Step 4b: Default-branch activity

Scope (D30): the repository activity reader, its compares, and the default-branch history reader
(5.8); the `repository_activities`, `repository_activity_compares`,
`repository_activity_compare_files`, `repository_activity_compare_commits`, and
`default_branch_commits` tables (5.1) and the `sync_runs` counters `events_deleted` and
`history_commits_deleted` in a migration, with a stored `totalCount` for the history
(`repo_sync_state.default_branch_commits_total`), the as-of times of the log and history reads
(`activity_read_at`, `history_read_at`), the log's oldest listed event (`activity_first_event_at`),
and the activity read's outcome and halt reason
(`activity_last_outcome`: `complete`, `time_limited`, `failed`, or `halted`; `activity_halt_reason`;
5.1); the compare form of `accepted_failures` (5.5); the planner and writer steps, which run after
5.3 step 6's writes and the refresh (5.2) and before step 7. Each run reads the activity log since the floor in full,
following GitHub's `Link` cursors, which the source then returns with a REST page; a read is
complete only when its oldest listed event is before the floor or is the first event a read of the
log in ascending order returns, made in the same run (one REST call), so a listing that ends early
without a next link is not taken for the log's start, and a read that lists no event is complete
when the ascending read also returns none, `activity_first_event_at` staying null; a deadline gives `time_limited` (exit 2, 5.3
step 8), and any other end fails the repository's activity for the run (exit 2, gap 106). A
complete read upserts the events from the floor on by `github_id` and records in
`activity_first_event_at` the `timestamp` of the oldest event it listed, one before the floor
included, keeping each stored event's compare, then deletes the stored events the log no longer
lists (D7), counting them in `sync_runs.events_deleted`, unless they exceed max(25, 2%) of the
repository's stored events, which halts that deletion as step 7 halts deletions. It compares each
event whose `before` and `after` are both non-zero and that has no compare row, or one holding an
`error` or a refusal, storing compare's commits in full (counted against `total_commits`) and the
files of its first page, the only page GitHub lists files on, with `files_at_cap` set when that
page returned 300 files, GitHub's documented cap for one comparison (gap 106). Each compare is
written in its own transaction when it completes, so a deadline keeps the compares already
written. A compare whose first page GitHub refuses (HTTP 404 or 422) is stored with its
`http_status`; one that still fails under 5.3 step 4's transient classes, whose 2xx first page has
no `files` list, whose fetched commits differ from `total_commits`, or whose later page answers 404
or 422 is stored with its `error` and makes the repository's activity `failed` (exit 2) unless an
`accepted_failures` entry for the repository and the event's `github_id` accepts it (5.5); both are
requested again on each later run until GitHub answers them, and verify's report and the manifest
count the capped, refused, and failed compares (5.4, 5.6). It reads the default branch's history
since the floor in full and replaces that repository's `default_branch_commits` rows only when the
read count equals `totalCount` and the commits it would remove are within the same guard, counting
them in `sync_runs.history_commits_deleted`, so a commit a force push removed leaves as GitHub no
longer lists it; a count that differs is rescanned once, and a second mismatch fails the
repository's activity for the run (exit 2, gap 106). A log or history page that still fails under
5.3 step 4's transient classes makes the repository's activity `failed` for the run: it writes
none of that read, and the run exits 2. The deadline stops the readers as it stops hydration, a
stopped log or history read writes nothing (`time_limited`), and a halt is `halted` (exit 2, 5.3
step 8) with its reason in `repo_sync_state.activity_halt_reason`; `--allow-deletions` applies its N to the events
and to the history commits separately (5.3 step 7). Event actors and commit authors are linked
like commit links (5.2), and a history replace keeps a commit's stored link when GitHub returns
none (D28). verify reads the repository's stored events and default-branch commits in one statement
each before the first request of its log and history reads, and reads the log and the history by
the sync's completeness rules; a read that
still fails after its retries, ends before the floor and the log's first event, or whose history
count still differs after one rescan makes the repository unverified (5.4). It compares the stored
event ids with the log since the floor and the stored commits with the history, taking the 5.4
classes, with `activity_read_at` the boundary for events and `history_read_at` for commits: not
synced when the repository's `activity_last_outcome` is `failed` or `time_limited` or the boundary
is null; halted deletion for a stored event or commit the fresh read no longer lists when it is
`halted`; drift for an event the fresh log lists at or after its boundary, and for a commit the
fresh history lists and the stored rows lack when the fresh log holds an event at or after
`history_read_at` (a merge brings commits with their earlier dates); removed since sync for a
stored event or commit the fresh read no longer lists; and error otherwise. The data dictionary
for the new tables; recordings from public repositories.

Tests: an event of each `activity_type` the recordings hold; an event with an all-zero SHA and no
compare row; a refused compare stored with its status and requested again on the next run; a
compare of exactly 300 files marked `files_at_cap` and one of 299 not; a compare whose 2xx first
page has no `files` list, one whose fetched commits differ from `total_commits`, one whose later
page answers 404, and one that still fails under the transient classes, each stored with its
`error`, making the activity `failed`, requested again on the next run, and accepted by a compare
entry, which a later run that GitHub answers reports as not having failed; a log read that lists no
event, with an ascending read that returns none, complete; an activity or history halt making the
exit 2; the compares written before a deadline kept; a two-page activity read through a
`Link` cursor that completes and sets `activity_first_event_at`, one that stops at the floor, one
the deadline stops (`time_limited`, `activity_first_event_at` unchanged), and one that ends without
a next link before the first event of the ascending read, failing the activity; a history count
mismatch rescanned once and, on a second mismatch, failing the activity; a second run sending no
compare for an event already compared; an event deletion past the guard halting; deleted events
and history commits counted in `sync_runs.events_deleted` and `history_commits_deleted`; a
diverged push whose compare runs from the merge base; a push of several commits whose compare
commits join each commit to its event; a force push removing commits from
`default_branch_commits`; a history read past the guard halting; a null actor; an event GitHub
stops listing removed on the next run; an event the log stops listing after the stored read
classified as removed since sync; a merge that brings commits dated before `history_read_at`,
classed as drift by its event; a halted repository's unlisted events classed as halted deletion;
verify's own log read failing and ending early, and its history count differing after a rescan,
each making the repository unverified; `activity_halt_reason` set by a halt and cleared by the
next run that does not halt, `halt_reason` unchanged; `--allow-deletions` applying N to events and
history commits separately; a history replace keeping a commit's stored author link when GitHub
returns none (D28); and each verify check and each stored-row check on the recordings, each
stored-row check reporting the rows it compared (D39).

Acceptance: on the Step 3b-2 acceptance repositories and on `prebid/github-activity-db`, the stored
events equal the log and the stored commits equal the history; each repository's first logged event
is recorded (section 8), with the file count of its largest push's compare and, for Prebid.js,
the page count of its full log read; the run records its
REST calls, points, and time.

### Step 5: Full bootstrap and verification

Scope: sync all tracked repositories into a new database. An agent runs it with `--repo` for every
tracked repository except `prebid-agent-skills`, whose rows the first scheduled run adds (D33, Step
6b); the maintainer may run it for all of them. Projected cost about 1,900 GraphQL points
for the open and merged PRs and the issues; closed-unmerged PRs (D3), default-branch activity
(D30), and the 11 repositories added on 2026-10-18 (D13) add to it, and Steps 3b-2 and 4b record what they cost.
The database is not committed (D26); it becomes the first snapshot of release `db` in Step 6b.

Acceptance: `verify --exact` for every repository the run synced, and REST checks only on those
repositories (D33); zero invariant failures other than accepted
failures, each listed in the PR with REST evidence of why it cannot pass; a 50-item random sample
matches REST; a difference report against the tracked file classifies every differing item
(closed-unmerged PR, file-status rebuild, commit list beyond 250, login rename, changed on GitHub after the old
file's sync, in-rule item absent from the old file, schema-only) with per-category counts, zero
unclassified items, and five REST-checked examples per category; a GraphQL timeline read of a
sample measures whether a body edit (`userContentEdits`), a base-ref change
(`BaseRefChangedEvent`), a review dismissal (`ReviewDismissedEvent`), and (from Step 4a) an issue
label event (`LabeledEvent`, `UnlabeledEvent`) change `updatedAt`, at least 20 counted cases of
each where the repositories hold them: a case counts only when no other timeline item or edit lies
between the event and the PR's or issue's `updatedAt`, and an event later than `updatedAt` is a
case that left it unchanged; a kind with such a case, or with fewer than 20 counted cases, gets an
index-side check or a periodic re-hydration in pull request 5b before Step 6a, which adds the 5.3
step 3 rule and its tests (gap 109); for each index contradiction (an alias `NOT_FOUND`, an item
outside coverage, an earlier `updatedAt`), how many occurred and how many the retry cleared are
recorded, and a kind that recurs gets a delay before its retry, measured, and added with its tests in pull
request 5b before Step 6a; `link_lookups` and `links_not_found` are recorded, and Step 6a's share
and minimum are set from them (5.2); and the run's time, cost, file size, and secondary-limit responses at one request in
flight are recorded (and at any wider setting proposed), with the time the workflow's bootstrap is
projected to take, Step 4b's compares included.

### Step 6: Scheduled sync and publication

Prerequisite: a GitHub App with read access to Contents, Pull requests, Issues, and Metadata,
installed on the tracked repositories, with its app ID, private key, and installation ID stored as
repository secrets.

Scope: 6a merges `sync.yml` with `workflow_dispatch` only, adds `ghactivity snapshot`,
`ghactivity verify --db-only` (5.4), and `scripts/compare_reads.py` for 6b, adds the commit-link
`NOT_FOUND` limit with its share and minimum from Step 5's measurement (5.2, 5.3 step 8), and adds the
`githubkit[auth-app]` extra to `pyproject.toml` and `uv.lock`. Its tests: `verify --db-only`
accepting the gz and manifest `ghactivity snapshot` wrote, and reporting the SHA-256 it computed,
the per-repository counts it compared, the revision it read, and the answers of `integrity_check`
and `foreign_key_check`, each equal to what the file holds; halting with exit 1 and naming its check
on an asset digest that names another file (a stub of GitHub's release-asset response) and on one
snapshot's gz given with another snapshot's manifest, both written by `snapshot`, its
decompression, `integrity_check`, and `foreign_key_check` refusals, which once the digest matches
only a defect of `snapshot` could reach, tested on their passing side (D39), and its unknown-revision
refusal on a file `snapshot` wrote after an upgrade through a test-only revision past head that
holds no flaw, as newer code writes one, halting with exit 1 and naming the revision check; a release with no complete snapshot pair halting with exit 1; `snapshot
--verify-report` exiting 1 on a missing report, on a file that is not a verify report (the gz
`snapshot` wrote), and on one from a verify of an earlier sync, whose `last_run_id` differs from the file's, and the manifest
listing the report's unverified repositories (5.6); through `httpx.MockTransport`, a mocked
installation-token response whose `expires_at` lies just over a minute ahead, the wall clock
githubkit's token cache reads (`datetime.now` in `githubkit.auth.app` and
`githubkit.cache.mem_cache`) stubbed past it mid-run, and a second token request before the next API request (githubkit keeps a token until
one minute before `expires_at`); and a 401 on an API request still exiting 1 with the run closed
(5.3 step 4); `verify --db-only --out` writing the file and printing its SHA-256 and highest
`sync_runs` id; `snapshot --after-run --sync-exit` refusing no newer row, two newer rows, a row
closed with another exit code, a configured entry's stored row whose `last_run_id` is older, and a renamed repository's row so
older, found through `entries`,
and accepting a stored repository the run neither attempted nor marked and, after a bootstrap with
`--repo`, the entries it did not name (5.6); `snapshot --sync-run` refusing a missing or unclosed
row, and neither form or both a usage error; the manifest carrying `sync_run_id`,
`sync_finished_at`, `actions_run_id`, `repo_filter`, `link_lookups`, `links_not_found`, and the Step
4b compare and deletion counts; the commit-link limit making the exit 2 above its share, not at it,
and not below its minimum number of lookups;
`scripts/compare_reads.py` on two files the sync wrote from the same recordings, reporting no
difference and the rows and columns it compared per table, equal to the files' counts, and on files
it wrote from recordings of one repository taken at two times, reporting each difference the
recordings hold (D39), and exiting 1 when a file is not at the code's migration head or a compared
table is empty in both; and a test that reads `sync.yml`, lists each exit-code output the `sync`
job declares, and fails unless `publish`'s and `report`'s conditions compare each as a quoted
string and require it to be non-empty, the path parsed from the job-level `DATABASE_URL`, resolved
against the step's working directory, equals `verify --db-only`'s `--out`, the SHA-256 comparison
and the recording of N come before `alembic upgrade head`, the `repo` input is validated and
requires `bootstrap`, `publish`'s condition is exactly the sync output `'0'` or `'2'` and the
verify output `'0'` with `inputs.repo` empty when the release is `db`, `report`'s requires both
outputs `'0'` and `publish`'s success, and `snapshot` is given `--out`, `--run-id`, `--after-run`,
and `--sync-exit`. 6b creates release
`db` and uploads the Step 5 database with its manifest (`ghactivity snapshot --sync-run` naming
Step 5's last run, and `--verify-report` from Step 5's `verify --exact --report`) as the first
snapshot. It creates release `db-test` the same way, holding snapshots this project's code wrote:
one built at the revision before the last migration, and one synced with a `tracking.toml` that
listed a repository the current one does not (a sync exit 2). Runs against `db-test` go in this
order, each prepared snapshot written and uploaded after the previous `db-test` publication so
that it is the latest pair: the snapshot one migration behind head and a run from it; the snapshot
with an untracked repository and a run from it; a run from a branch whose `tracking.toml` names a
repository that does not resolve; and the App comparison below. Before any App run writes to release
`db`, 6b compares the App's reads with the maintainer token's: a `workflow_dispatch` run with
`bootstrap`, `release: db-test`, and `repo` naming a sample of the repositories syncs them with the
App into an empty file, and at once a local run syncs the same repositories with the token into a
fresh file. The sample holds a PR with a `Team` review request, one with an `Organization`
assignee, and a private repository other than `prebid-agent-skills` (D33); a case the tracked
repositories do not hold is reported as unmeasured. `scripts/compare_reads.py` compares the two
files over every table the sync writes from GitHub (`repositories`, `repo_labels`,
`github_users`, `account_profiles`, `account_node_ids`, `pull_requests` and its child tables, and
from Step 4 the issue and activity tables), every column and child row except those stamped from
`Date` headers, the run ids, and row ids, each foreign key compared as the natural key of the row
it names, as `tests/sync/expected_state.py` does, and lists as a difference any value the token read that the App
read as null. Any difference or unmeasured case blocks App runs that publish to `db`, and 6c, until
it is explained or the permission is granted (gap 107). Once the comparison shows no unexplained
difference, it runs one normal manual run and one bootstrap run with `release: db`. 6c enables the schedule and stops
tracking `github_activity.db`: it removes the file from the index, the `!/github_activity.db`
exception from `.gitignore`, the `check-added-large-files` exclude, the
`tracked-db-rollback-journal` and `tracked-db-no-sidecar` hooks, the D26 freeze check, and
`alembic/env.py`'s refusal of the repository-root `github_activity.db` and of any file of that name
(gap 73); replaces the tracked-file journal test with one on the snapshot `VACUUM INTO` writes;
records the D20 WAL decision for the working file; and documents how to download the latest snapshot
and the staleness rule 5.6 gives consumers.

Acceptance: two consecutive scheduled runs publish, and their manifests list drift, pending, and
failure counts; the bootstrap run publishes, and the next scheduled run starts from its snapshot; a bootstrap or
catch-up run that reaches its deadline publishes with the workflow red (D38), and the pull request
records whether the bootstrap run reached it and how many runs it took to complete; a
run makes requests more than 60 minutes after its first installation-token mint, and its log
records at least two mints; against release `db-test`, the snapshot one migration behind head
upgrades and publishes, the snapshot with an untracked repository publishes and the workflow is
marked failed, and a re-run of `publish` after an interrupted upload completes; the run from a branch
whose `tracking.toml` names a repository that does not resolve publishes nothing, verify reporting
that repository unverified, and is marked failed; `verify --db-only`'s digest, manifest, and revision refusals
are covered by the 6a tests.

### Step 7: Documentation rewrite and code audit

Scope: README, `CONTRIBUTING.md`, `CLAUDE.md`, and `docs/` rewritten from the code; the data
dictionary from Steps 3-4 completed with the coverage rule and column definitions; no hardcoded
counts. A final audit of the whole repository, made once Steps 1-6 have landed, lists:
- code that no command, workflow, or test reaches;
- dependencies in `pyproject.toml` that nothing imports;
- scripts, hooks, and CI steps for mechanisms a step removed;
- tests and fixtures of removed paths, and fixtures no test reads;
- every note that defers something until a step that has landed (in code, docs, `.gitignore`,
  and hook comments).

Each entry is removed in Step 7 or given an owner (a later step or an issue).

Acceptance: the pull request lists the audit's entries, each removed or owned; the documents
describe the code at Step 7's head, and the pages kept as history until Step 7
(`docs/architecture.md`, `docs/data-model.md`, `docs/rate-limit-handling.md`,
`docs/github-api-quirks.md`) are removed or rewritten.

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
| Source vs REST | 43 PRs (seeded sample of the indexes of Prebid.js, prebid-server, salesagent, prebid-mobile-android, prebid.github.io, go-gdpr, plus the recorded fixture PRs) hydrated through `source/` and read again through REST: state, ids, draft, title, body, author, merger, base ref, additions, deletions, changed files, commit count, author association, labels, reviews (id, state, reviewer), files (path, additions, deletions), commit SHAs and linked authors (up to 250), and commenters equal on 40. The 3 differences are REST's: it reports `ghost` (id 10137) where GraphQL returns a null actor (`Prebid.js#12975`), and it reports 0 additions and deletions for some files of large PRs (`salesagent#1501`: 132 of 1,504 files; `#1721`: 93 of 1,473), where GraphQL's per-file counts sum exactly to the PR totals | live cross-check, 2026-10-17 |
| Partial errors | A batch with a missing number returns data for the other aliases plus `errors[{type: NOT_FOUND, path}]`; `issue(number:)` on a PR's number also returns `NOT_FOUND` | live API |
| githubkit 0.14.2 | `graphql.arequest` raises `GraphQLFailed` on any error (its `.response` is the parsed body, without HTTP status or headers) and `PrimaryRateLimitExceeded` on `RATE_LIMITED`; `GitHub.arequest` returns data and errors together; its retry covers 403/429 rate-limit responses and 5xx only; defaults `auto_retry` on (rate limit once, 5xx three times), `http_cache=True`, `follow_redirects=True`, no timeout; accepts `async_transport` | installed source; probe |
| Item ids | `PullRequest.databaseId` and `PullRequestReview.databaseId` are deprecated: "`databaseId` will be removed because it does not support 64-bit signed integer identifiers. Use `fullDatabaseId` instead. Removal on 2024-07-01 UTC." Both are still served. `fullDatabaseId` is a BigInt returned as a JSON string. Prebid.js#15779 returns 4,891,210,785 (above 2^31) from both, equal to its REST `id`. `databaseId` on `User`, `Bot`, `Organization`, and `Mannequin` is not deprecated | live introspection; live query and REST |
| Actor types | `Actor` has five implementers: `Bot`, `EnterpriseUserAccount`, `Mannequin`, `Organization`, `User`; `EnterpriseUserAccount` has no `databaseId` (it has `user`) | live introspection |
| One account, two shapes | dependabot (49699333) is `Bot`/`dependabot` as the author of `Prebid.js#15724` and `dependabot[bot]` as its commit's `GitActor.user` (declared type `User`); REST `pulls/15724/commits` gives `dependabot[bot]`, type `Bot`; `nodes(ids:)` on the commit user's node id returns `Bot`/`dependabot` | live GraphQL and REST |
| Duplicate paths | `Prebid.js#13481`: `changedFiles` 2, `files.totalCount` 3 (`CLAUDE.md` deleted, `CLAUDE.md` added, `package-lock.json` modified), so `changedFiles` is not the file-list length | live query |
| Null-author comments | `Prebid.js#12975`: PR author null; 1 of its 2 conversation comments has a null author | live query |
| Dismissed verdicts | `prebid-server-java#4637`: Lightwood13 submitted CHANGES_REQUESTED (5276426920), then a review now DISMISSED (5289072517); `latestOpinionatedReviews` lists only the two APPROVED reviewers. A rule taking the latest APPROVED or CHANGES_REQUESTED review would report CHANGES_REQUESTED | live query |
| Late closing links | `prebid-server-java#4380`, `#4350`, `#4397`: a `ConnectedEvent` on 2026-08-14 linked an issue months after merge, and the PR's `updatedAt` did not change. Adding `fullDatabaseId`, `authorAssociation`, and `closingIssuesReferences(first: 10)` with `nameWithOwner` keeps an index page at 1 point | timeline scan; live index page and `rateLimit(dryRun: true)` |
| Closing-link timeline events | Over the 100 most recent PRs each of Prebid.js, prebid-server-java, and prebid-server (2026-10-19), the `ConnectedEvent` subjects of each of the 48 PRs with closing links equal its user-linked issues (`userLinkedOnly: true`); each event's `source` is the PR and its `subject` the issue. None of the 30 links made only by a closing keyword has an event, nor does `Prebid.js#12975`'s ("Closes #12914"), read separately; no `DisconnectedEvent` occurs. `filteredCount` equals the filtered entries on all 300, each a single page. On a later page `filteredCount` counts only the items after its cursor (`Prebid.js#15694`, `ISSUE_COMMENT`, two entries per page: 11 on the first page, 9 on the second). `totalCount` is the same with and without `itemTypes` and is not the number of entries returned (56 on `prebid-server-java#4380`, whose unfiltered timeline returns 66 entries and one connected event) | `timelineItems(itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT])` beside both closing lists, and `ISSUE_COMMENT` pages, live |
| Timeline `since` | On eight events, five ready-for-review or convert-to-draft events (`Prebid.js#15581`, `#15806`, `#15807`, `#15821`, and `#15823`) and three connected events (`prebid-server#4805`, `#4840`, and `#5007`), `timelineItems(since:)` returned the event for a `since` 1 ms before its `createdAt` (on the five, also 0.5 s and 1 s before) and not for a `since` equal to it (on the five, nor 1 ms, 0.5 s, 0.999 s, or 1 s after), so `since` keeps the items created after it; every `createdAt` read was a whole second. `filteredCount` applies `since`: on `Prebid.js#15807` it was 5 without `since`, 3 with `since` one second before the third event, and 2 at it, each equal to the entries returned, although GitHub's schema describes it as the count "after applying `before` and `after` filters". Paged one entry at a time with `since` set, `filteredCount` read 3, 2, and 1. Each read cost 1 point | live GraphQL `timelineItems(itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, CONNECTED_EVENT, DISCONNECTED_EVENT], since:)` and schema introspection, 2026-10-21 |
| Copilot review requests | In 3 of the last 60 Prebid.js PRs, and none of the last 60 of prebid-server or prebid-server-java (2026-10-19), each `ReviewRequestedEvent` for Copilot names the bot `copilot-pull-request-reviewer` (`BOT_kgDOCnlnWA`, `databaseId` 175728472), the account that writes its review; no other Copilot account appears. A pending request was not caught | `reviews`, `reviewRequests`, and `timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT, REVIEW_REQUEST_REMOVED_EVENT])`, live |
| Label renames | The prebid-mobile-ios label "4.0 In-App Platfrom" became "4.0 In-App Platform"; issue #1030, which carries it, kept its `updatedAt` | REST events; live query |
| Author association | GraphQL and REST agree on 100 of 100 PRs; on Prebid.js, the earliest merged PR of each of 60 recent first-time authors reads CONTRIBUTOR (59) or MEMBER (1), not FIRST_TIME_CONTRIBUTOR, so the value is computed when read, not stored at creation. `Prebid.js#15715`, merged, whose commits link to no account, still reads FIRST_TIME_CONTRIBUTOR (GitHub: "Author has not previously committed to the repository.") | live queries; introspection |
| Issue transfer | `prebid.github.io#6053`, transferred from Prebid.js on 2025-05-23, has `databaseId` 3087307383, in the range of issues created that day rather than of its July 2024 creation; GraphQL returns `NOT_FOUND` for `Prebid.js#13150`, while REST follows that number to `prebid.github.io#6052` | live query; REST |
| Activity during a run | Over 3 days, at least one in-coverage item was updated in 13% of 5-minute windows and 31% of 15-minute windows (a lower bound: only each item's latest update is visible) | 100 most recently updated PRs and issues per public repository |
| Rate-limit windows | One personal token's GraphQL `rateLimit` readings alternated between two windows with different `remaining` and `resetAt` within seconds; REST `/rate_limit` reported one of them. GitHub: "Because GitHub processes API requests in multiple regions, rate limit values can vary from one response to the next based on the location handling the request." | consecutive `rateLimit(dryRun: true)` readings; GitHub docs |
| Installation tokens | "The installation access token will expire after 1 hour." githubkit's `AppInstallationAuthStrategy` mints a new token before expiry and needs PyJWT and cryptography, which the current environment lacks | GitHub docs; installed githubkit source; `importlib.util.find_spec` |
| Secondary limits | GraphQL may use no more than 60 s of CPU time per 60 s of real time; GitHub advises avoiding concurrent requests; a request taking more than 10 s is terminated. A GraphQL secondary limit returns HTTP 200 or 403, a REST one 403 or 429, each with an error message; GitHub's handling: `retry-after` if present, else the reset time when `x-ratelimit-remaining` is 0, else at least one minute | GitHub docs |
| Nested settings | Before Step 3a: `SYNC__COMMIT_BATCH_SIZE=50`, `PACING__BURST_ALLOWANCE=99`, and `LOGGING__ROTATION` leave `Settings` at its defaults (25, 10, `10 MB`); `Settings` sets no `env_nested_delimiter` | probe |
| Logging placeholders | Before Step 3a: 51 of 56 `logger.<level>(...)` calls in `src/` pass `%`-style placeholders, all under `github/` | AST count over `src/` |
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
| Release downloads | For a public release asset (`cli/cli` v2.102.0), with an `Origin` header: `github.com/.../releases/download/...` answers 302 to `release-assets.githubusercontent.com` with no `access-control-allow-origin`; that host answers a range request with 206 (`accept-ranges: bytes`) and no `access-control-allow-origin`; the API asset endpoint (`api.github.com/repos/.../releases/assets/{id}`, `Accept: application/octet-stream`) answers 302 to the same host with `access-control-allow-origin: *`. So a browser page on another origin cannot read the file (inferred from the headers; no browser was run), and a server or build step can | `curl -D -`, 2026-10-18 |
| Release assets | Each file under 2 GiB; at most 1,000 assets per release; an upload that ends in a 502 "may leave an empty asset with a state of starter"; the asset `digest` field is "string or null"; GitHub renames asset names containing special characters; `gh release upload --clobber` deletes before upload ("If the upload fails, the original assets will be lost.") | GitHub docs; gh source |
| Large files | Git warns above 50 MiB; GitHub blocks files above 100 MiB | GitHub docs |
| Schedules | Public-repo schedules disable after 60 days without repository activity; start of hour is high load; queued jobs may be dropped; `workflow_dispatch` requires the workflow on the default branch | GitHub docs |
| Concurrency | A new run replaces a pending run in the group; running runs are cancelled only with `cancel-in-progress: true` | GitHub docs |
| App token action | `actions/create-github-app-token` v3.2.0 (2026-05-12) and v3.3.0 (2026-10-12); the sync no longer uses it (5.6) | GitHub API |
| SQLite pragmas | The `connect` listener on `engine.sync_engine` sets per connection through aiosqlite: `foreign_keys=1` and `busy_timeout=30000`. Without the listener a connection reports `foreign_keys=0` and `busy_timeout=5000` (Python `sqlite3` default `timeout=5.0`). A file once set to WAL stays WAL (header bytes 18-19 `0202`; rollback `0101`). A foreign-key violation is rejected and `ON DELETE CASCADE` removes children; the same values hold on every session from `get_session()` | `tests/test_db_engine.py` (`TestSqliteConnectionSettings`), `tests/db/test_application_engine.py` |
| Connect-listener failure | When a statement in the `connect` listener raises, SQLAlchemy 2.0.45 does not close the DBAPI connection and the aiosqlite 0.22.1 worker thread keeps the process alive. With `PRAGMA journal_mode=DELETE` in the listener, `ghactivity sync retry --dry-run` against a database held with `BEGIN EXCLUSIVE` printed "database is locked" and was still running at 60 s; without that statement, and separately with the connection closed on failure, it exited 1 after 31.6-31.8 s (the 30 s busy timeout). With the close in place and the statement restored, a WAL database held open by another process fails every connection with "database is locked" (exit 1 after 0.5 s), where without the statement the CLI uses the file | CLI runs against a locked file |
| Build constraints | `uv build --build-constraints build-constraints.txt --require-hashes` builds the wheel; with both listed hatchling hashes altered it exits 2 (hash mismatch); with only the wheel's hash altered it succeeds, because uv rejects the wheel and builds hatchling from its sdist, whose hash still matches | local builds with uv 0.7.10, no cache |
| Packaged config | The wheel built from the Step 1 branch (merged as `8c5b16b`) contains `github_activity_db/tracking.toml`; installed with the locked dependencies and run outside the checkout, `Settings().tracked_repos` returns the 18 repositories. A wheel built from `7555a6d` raises `TrackingConfigError` for `<env>/lib/python3.13/config/tracking.toml`; a wheel excluding the file fails the same check | `uv build`, `scripts/wheel_smoke.py` in a new environment |
| All-skipped runs | pytest exits 0 when every collected test is skipped; the previous inline gate exited 0 on that report and `scripts/check_junit.py` exits 1 | report written by a real pytest run |
| Suppression spellings | ruff 0.14.11 honors `# noqa` in any letter case, `#noqa`, `# noqa:F401`, a directive after other comment text, and file-level `# ruff: noqa`, `# ruff: noqa: F401`, `#ruff:noqa`, `# flake8: noqa[: codes]`, but not `# RUFF: NOQA`; `PGH004` flags only the blanket forms. mypy 1.19.1 honors `# type: ignore`, `# type:ignore[...]`, `#type:ignore`, a first-line `# type: ignore`, `# mypy: ignore-errors`, and `# mypy: disable-error-code=...`, but not `# TYPE: IGNORE`, `#mypy:ignore-errors`, or `# type: ignore` after other comment text. The counter also counts `# RUFF: NOQA`, `#mypy:ignore-errors`, and the trailing `# type: ignore`, and does not count `# TYPE: IGNORE`. ruff also stops I001 for `# isort: skip_file`, `# isort: off`, and their `# ruff: isort:` forms, and for `# isort: skip` on the first of two out-of-order imports (0 findings; on the second, 1 finding remains); mypy reports no error inside a `@no_type_check` function, also when the decorator is imported under another name or decorates an `async def` | probe files with `ruff check --isolated` and `mypy --strict` |
| Range suppressions | ruff 0.14.11 with `--preview` honors `# ruff: disable[F401]` on its own line or indented, with or without spaces around `:` and before `[` (any whitespace, tab and U+3000 included), with several codes (`#ruff:disable[F401, E501]`), spaces inside the brackets, a trailing comma, and text or a comment after `]`; `# ruff: enable[F401]` ends the range. It does not honor it without `--preview`, in uppercase or mixed case, with no or empty codes, at the end of a code line, or after other comment text. The counter counts every `# ruff: disable` in any letter case and no `# ruff: enable` | probe files with `ruff check --isolated --select F401`, with and without `--preview`, 2026-10-17 |
| Counter file forms | ruff 0.14.11 ignores a coding declaration: in a `# coding: latin-1` file, a UTF-8 `# ruff:<U+3000>disable[F401]` suppresses F401 with `--preview`. It also ends a line at a lone CR: `# ruff: noqa: F401` after one suppresses F401 without `--preview`. tokenize follows the declaration and does not split lines at a lone CR, so a counter that counts `# ruff: disable` but reads these files with tokenize counted 0 in both, against 1 for the same directive in a UTF-8 file without a declaration (`212a3f4`, on PR #9). pre-commit's file tagging (identify 2.6.16) marks `module.PY`, `app.wsgi`, and an executable with a `#!/usr/bin/env python3` or `nix-shell -i python3` shebang as python, `stub.PYI` as pyi, and `#!/bin/env python3` as neither (it strips only `/usr/bin/env`), and marks an executable `tool.sh` with a python shebang as shell and a non-executable shebang file as text | probe files with `ruff check --isolated --select F401`, with and without `--preview`; `identify.identify.tags_from_path`, 2026-10-17 |
| Existing file | 0 foreign-key violations; integrity check `ok` | local |
| WAL snapshots | `VACUUM INTO` from a WAL database writes a rollback-journal file (header bytes 18-19 `0101` vs `0202`) that can be queried read-only from a read-only directory | sqlite3 |
| Test-suite flake | pytest-asyncio 1.3.0 on Python 3.13 leaves an unclosed event loop after an async test followed by a test calling `asyncio.run()`; with `filterwarnings = error` the next test fails. Fixed in pytest-asyncio 1.4.0 (#724) | repeated runs on `22551d3` (3 of 3 failed) and with 1.4.0 (5 of 5 passed); release notes |
| WAL with a tracked file | SQLite: any `*-wal` file must be copied together with the database, and swapping or moving journal files between databases is likely to corrupt them | sqlite.org/howtocorrupt.html |
| CI color forcing | typer sets `FORCE_TERMINAL` when `GITHUB_ACTIONS`, `FORCE_COLOR`, or `PY_COLORS` is set; two help-text tests failed only on GitHub Actions and reproduced locally with `GITHUB_ACTIONS=true` | installed typer `rich_utils.py`; CI run on `c7e0146` |
| Alembic on SQLite | alembic 1.18.1, SQLAlchemy 2.0.45, SQLite 3.45.3; two revisions, the second dropping the first's table, creating another, and inserting a row. Default handling, second revision failing: the first table dropped and the second created empty, with the first revision stamped. A statement executed before `context.configure()`: `upgrade` exits 0 with no revision stamped and no row inserted. Transactional DDL (the driver's transaction handling off, `BEGIN` emitted on `begin`, `transactional_ddl=True`), second revision failing: nothing applied, not even the version table | probe through a real script directory and `env.py`, 2026-10-17 |
| Isolation level | Assigning `isolation_level = None` to a Python `sqlite3` connection inside a transaction commits it, after which `PRAGMA foreign_keys=OFF` takes effect; set before the assignment, inside the transaction, the pragma leaves `foreign_keys` at 1 | probe, Python 3.13, SQLite 3.45.3 |
| Connect listener order | SQLAlchemy 2.0.45's first-connect handling rolls a new connection back before connect listeners added after `create_engine` run: such a listener, given a connection the DBAPI returned inside a transaction with `foreign_keys=ON`, sees `in_transaction` false, and `PRAGMA foreign_keys=OFF` then takes effect. Registered with `insert=True`, it sees the open transaction and the pragma leaves `foreign_keys` at 1; SQLAlchemy's initialization and a `BEGIN IMMEDIATE` transaction still work after it | probe through a patched `sqlite3.dbapi2.connect`, 2026-10-17; `tests/db/test_migrations.py` |
| URI filenames | Python 3.13's `sqlite3` with SQLite 3.45.3 (no `SQLITE_USE_URI` compile option) opens `file:target.db`, without `uri=True`, as a new file of that name, not as `target.db`. Other builds, CI's included, are not checked; `alembic/env.py` refuses `uri=true` URLs and checks the file SQLite opened, so either reading is covered | probe |
| Path from the URL | SQLAlchemy 2.0.45's pysqlite dialect opens `os.path.abspath(database)`, which drops `..` as text, while `Path.resolve()` follows symlinks first: `/tmp/..` before the tracked path (on macOS `/tmp` is a symlink) and `<symlinked dir>/../github_activity.db` name the tracked file to the driver and a missing file to `resolve()`. An env.py that checked the resolved URL path migrated a stand-in of the tracked file through the second spelling; `PRAGMA database_list` on the opened connection names the file SQLite opened | review probe at `f90eee9`, on PR #11, 2026-10-17; `tests/db/test_migrations.py` |
| Commit inside a revision | Under `transactional_ddl=True`, a revision's `op.execute("COMMIT")`, `op.execute("END")`, or `autocommit_block()` committed its writes before a later failure, leaving the file at head with a foreign-key violation. An SQLite authorizer that denies `SQLITE_TRANSACTION` statements other than the upgrade's own refuses each, and `connection.commit()` too | review probe at `f90eee9`, on PR #11; `tests/db/test_migrations.py` |
| Prepared statements | Python's `sqlite3` reuses a prepared statement by its SQL text (128 by default), and SQLite runs the authorizer only when it prepares one. SQLite's own rollback (`INSERT OR ROLLBACK`, `RAISE(ROLLBACK)`) expires prepared statements only when the transaction changed the schema, so in a data-only upgrade a statement the revision repeated after a swallowed rollback ran in autocommit: its row was committed with a foreign-key violation, and a repeated `SAVEPOINT` reopened a transaction env.py then committed, the earlier writes lost. With `cached_statements=0` every execute is prepared and refused | review probes at `4af764b`, on PR #11, 2026-10-17, SQLite 3.45.3 and 3.47.1; `tests/db/test_migrations.py` |
| Pragmas in a revision | Inside the upgrade's `BEGIN IMMEDIATE` and before its first write, `PRAGMA journal_mode=OFF` takes effect: pages a large revision then spilled reached the file unjournaled, and a failure after them left it unreadable ("malformed database schema"). `legacy_alter_table=ON` let a batch alter under the views pass, and `ignore_check_constraints=ON` committed a row a named CHECK refuses | review probes at `4af764b`, on PR #11, 2026-10-17, SQLite 3.45.3 and 3.47.1; `tests/db/test_migrations.py` |
| Revision module code | alembic loads each revision module inside env.py's transaction, so `op.execute` at a module's top level runs in `current`, `check`, and `stamp`, which run no revision. Such code's `DELETE` was refused only when the check ran on rows changed (`total_changes`); its `DROP TABLE github_users` changes no row, left one `pull_requests.author_id` orphaned, and committed. The check now runs before every commit | review probes at `459393f`, on PR #11, 2026-10-17, SQLite 3.45.3 and 3.47.1; `tests/db/test_migrations.py` |
| View lookups | With only single-column indexes on `pr_reviews` and `pr_comments`, `EXPLAIN QUERY PLAN` for `v_pr_reviewers` and `v_pr_commenters` searched the verdict, comment-count, and reviewer lookups by the account alone (`reviewer_id=?`, `author_id=?`); with the PR index created first, the planner took the account index, created later. With one account reviewing and commenting on every PR, four times the PRs cost about 15 times the SQLite virtual-machine instructions for each view, against about 4 times with `(pr_id, reviewer_id, github_id)` and `(pr_id, author_id)`, with the same rows | review probe at `459393f`, on PR #11, and local probes, 2026-10-17, SQLite 3.45.3 and 3.47.1; `tests/db/test_views.py` regenerates the ratio |
| Batch alter and views | `batch_alter_table(..., recreate="always")` on a table a view reads raises "error in view v: no such table: main.p"; with `PRAGMA legacy_alter_table=ON` it succeeds | probe, same versions |
| CHECK constraints | Batch recreate keeps `CONSTRAINT ck_k_b CHECK (b < 10)` and drops the unnamed column-level `CHECK (a > 0)` and table-level `CHECK (n < 5)`. SQLAlchemy renders `Boolean` as `BOOLEAN` and `Enum` as `VARCHAR(6)`, without a CHECK, on SQLite. Under a `ck` naming convention an explicit `name="ck_prs_state"` becomes `ck_prs_ck_prs_state`, and `op.f("ck_prs2_state")` stays as given. `compare_metadata` reports nothing against a table with an unnamed `UNIQUE (name)` under an ix/ck/fk convention, and `add_constraint` under a `uq` convention | probe, same versions |
| SQLite timestamps | SQLAlchemy's SQLite `DateTime` stores `2024-01-01 10:00:00.000000` (26 characters; microseconds always written) and stores 12:00+02:00 as `2024-01-01 12:00:00.000000`, dropping the offset | probe, same versions |
| Usage-error exit code | `ghactivity sync --no-such-option` and an unknown command exit 2 (click 8.3.1, typer 0.21.1) | CLI at `c1f474b` |
| Lax datetimes | pydantic 2.12.5 `AwareDatetime` in lax mode accepts a TOML `floor` given as the string `"2023-01-01T00:00:00Z"` or the integer 1672531200; strict mode rejects both and accepts a TOML offset date-time | probe |
| Locals in tracebacks | typer 0.21.1 defaults `pretty_exceptions_show_locals` to true, and `cli/app.py` does not override it; `logging.py` sets `diagnose=True` on both stderr handlers, and its optional file handler passes none, so it takes loguru's default (`LOGURU_DIAGNOSE`, true) | installed typer signature and loguru `_defaults.py`; source at `c1f474b` |
| Half-read queries | With a cursor that has fetched 1 of 1,000 rows on one connection, a commit on a second connection (busy timeout 0.5 s) raises "database is locked". SQLAlchemy's aiosqlite dialect fetches every row and closes the cursor inside `execute` unless the cursor is server-side | probe, Python `sqlite3`, rollback-journal mode; installed `sqlalchemy/dialects/sqlite/aiosqlite.py` |
| Run claim | While one connection holds `BEGIN IMMEDIATE` with an uncommitted `running` row, a second claimant using `BEGIN IMMEDIATE` waits, then refuses. With a deferred `BEGIN`, the claimant reads no `running` row and its insert fails at once with "database is locked"; the holder's commit then fails too only while the claimant keeps its transaction open, and succeeds once the claimant rolls back or closes | probe, Python `sqlite3` |
| Cancelled writer transaction | On databases built by `alembic upgrade head`, a task awaiting a statement inside `transaction()` on the writer's connection was cancelled, unshielded. During a slow recursive `SELECT` (cancelled at 20, 50, and 200 ms) the connection was invalidated, another connection's `BEGIN IMMEDIATE` was still refused, `close_run` failed after the busy timeout (2 s here) with "database is locked", the row stayed `running`, and the lock was still held after the writer's engine was disposed: 3 of 3. During a slow `INSERT ... SELECT` the lock was released, nothing was inserted, and the close committed `aborted` at once: 3 of 3. In a transaction alternating short `SELECT`s and `INSERT`s, cancelled at 24 points from 13 to 404 ms, the lock was kept and the close failed 3 times. With the slow `SELECT` behind `asyncio.shield` and awaited before the close, the close committed at once (`aborted`, 130) | probe, 2026-10-20: Python 3.13.0, SQLite 3.45.3, SQLAlchemy 2.0.45, aiosqlite 0.22.1 |
| Loguru placeholders | ruff 0.14.11 PLE1205 flags `logger.warning("brace {}", value)` and passes `logger.warning("percent %s", value)` for `logger = get_logger(__name__)` | `ruff check --isolated --select PLE1205,PLE1206,G` on a probe file |
| Tracked database | `github_activity.db` at `c1f474b`: SHA-256 `c18b2bc50a7e60f822cff185723cabc2846da31bcd8814aa9df46dca0d1b4751`, revision `b3c4d5e6f7g8`, 14,669 pull requests, 0 `user_tags` and 0 `pr_user_tags` rows, `user_version` 0, journal mode `delete` | `shasum -a 256`; `sqlite3` on a copy |
| Quality gates | At `43fcf06`, before Step 1: no CI; pre-commit hooks not installed; ruff 0.14.11 (lock) 10 findings plus 4 format diffs vs ruff 0.8.6 (pre-commit pin) 0 findings plus 1 format diff; mypy clean on 91 files; 608 tests, 0 failures | local |
| Private data | `prebid-agent-skills` is private; the committed file contains its PRs since commit `bf40280` | `gh repo view`; history |
| PR states | GitHub's `PullRequestState` is `OPEN`, `CLOSED`, `MERGED` | live introspection, 2026-10-18 |
| Review order | On `prebid-server#4710`, review 3886287882 was created at 01:54:50Z and submitted at 15:59:52Z, after review 3889751813, created and submitted at 14:23:49Z: review ids do not follow submission order | live GraphQL, 2026-10-18 |
| Collected fields | The 25-PR batch costs 2 points with the Step 2 document and still 2 with every field and connection 5.8 adds in Step 3b-1; `rateLimit(dryRun: true)`, which also validates every field name, and `tests/source/graphql_cost.py` agree on each variant. A variant that also added `latestReviews`, `latestOpinionatedReviews`, and `participants`, together with commit fields D19 excludes, cost 3; by the formula the three connections make the difference, and a fourth added connection alone makes it 3. Inline review comments selected as nodes inside reviews make it 27; each review's `comments { totalCount }` alone adds nothing. `assignedActors` (users, bots, mannequins, and organizations) in place of `assignees` (users only), and node ids on reviews, comments, and review requests, keep it at 2. The earlier resolve with the repository fields then selected returned in one live read at 1 point, with the token used here. Spreading the `Actor` fragment inside `pinnedBy` (typed `User`) made the dry run fail with GitHub demanding the `read:enterprise` scope for `EnterpriseUserAccount.id`; selecting the user's own fields there passes. The resolve with the added repository and label fields, the user lookup with `name`, `createdAt`, `updatedAt`, `claimant`, and D31's profile fields, and the PR index with `closedAt` each cost 1 | dry runs on prebid/Prebid.js, 2026-10-18 |
| Inline review comments | Of the 50 most recently updated merged PRs: Prebid.js has 140 reviews, 90 with inline comments, 130 inline comments; prebid-server 445 reviews, 290 with inline comments, 393 inline comments | live GraphQL, each review's `comments { totalCount }`, 8 points, 2026-10-18 |
| First-schema requests and assignees | Of the tracked file's 14,669 PRs, `reviewers` (pending review requests, gap 7) is non-empty on 2,752 and `assignees` on 6,444; the Step 3a schema stores neither | `sqlite3` on a copy of `github_activity.db`, 2026-10-18 |
| Repository activity | `GET /repos/{owner}/{repo}/activity?ref=refs/heads/<branch>` returns events with `id`, `node_id`, `activity_type`, `ref`, `before`, `after`, `timestamp`, and `actor`; githubkit 0.14.2's `Activity` model types `activity_type` as `push`, `force_push`, `branch_deletion`, `branch_creation`, `pr_merge`, or `merge_queue_merge`, and `actor` as nullable. `prebid/github-activity-db` `main`: 40 events since the branch was created on 2026-01-15 (11 `pr_merge`, PRs #1-#11; 27 `push`; 1 `force_push`; 1 `branch_creation`). The oldest event on the default branches of `prebid/Prebid.js` and `prebid/prebid-server` is from 2023-03-07; Prebid.js's newest 100 `push` events span 2025-04-03 to 2026-10-16, and prebid-server has none. `Commit.pushedDate` is deprecated ("`pushedDate` is no longer supported. Removal on 2023-07-01 UTC.") and returned null on the three newest Prebid.js `master` commits. Each of the 11 `pr_merge` events' `after` equals its PR's `mergeCommit.oid`: PRs #1-#6 merged with a merge commit (2 parents), #7-#11 squashed (1 parent); no rebase merge exists to measure | live REST and GraphQL, introspection; installed githubkit `versions/v2022_11_28/models/group_0267.py`, 2026-10-18 |
| Merge state and stacks | Adding `mergeable`, `mergeStateStatus`, `canBeRebased`, `potentialMergeCommit`, `stack`, and `stackEntry` keeps the 25-PR batch at 2 points (dry run, with the auto-merge and merge-queue fields also added; formula). Of the 17 public tracked repositories, none allowed auto-merge (`autoMergeAllowed`) or had a merge queue on its default branch, and no open PR had an auto-merge request or a queue entry; none of the 28 repositories read after the additions (D13) allows auto-merge. A real read of the 3 most recently updated open Prebid.js PRs returned them without error (each `MERGEABLE`, `BLOCKED`, in no stack). GitHub's descriptions: `stack` is "The stack this Pull Request belongs to, or null if it is not part of a stack"; a stack entry's `position` is "This entry's position in the stack, where 1 is the closest to the base branch"; `potentialMergeCommit` "will not return a value if the pull request is merged, or if the test merge commit is still being generated". The 17 public tracked repositories held 440 open PRs (prebid-server 137, salesagent 86, prebid.github.io 77, Prebid.js 35, 36 at a later read), 25 batches when batched per repository as the PR batch requires; the 28 tracked repositories other than `prebid-agent-skills` held 463 in 30 per-repository batches after the additions. `prebid` has 11 project boards; GitHub refused their `title` without the `read:project` scope | dry run and live GraphQL, introspection, 2026-10-18 |
| Merge state after a base merge | Three PRs were merged into Prebid.js `master` (#15678 at 00:58:52, #15814 at 01:13:09, and #15825 at 01:19:19 UTC on 2026-10-21) between Step 3b-2a-2c's second and third acceptance runs. Of the 41 PRs open on `master` after the third run, 38 were stored before the merges and 37 of those kept their `updatedAt`, so the third run (01:36 to 01:38 UTC) did not hydrate them; it hydrated four: #15581, stored before the merges with a new `updatedAt` (01:22:15), and #15823, #15824, and #15826, opened between 01:05 and 01:09. Read live after the third run, all 41 had the stored `updatedAt`, and 8 of them a different `mergeStateStatus` than stored (6 `UNKNOWN` to `BLOCKED`, 2 `BLOCKED` to `UNKNOWN`); 38 of the 41 stored values were `UNKNOWN`. GitHub changed the merge state without changing `updatedAt`, so the stored merge state is as of `hydrated_at` (5.1, D32); the reading cannot tell whether the merges or GitHub's own recomputation changed it | the acceptance file and live GraphQL, 2026-10-21 |
| Step 3b-2a-2c acceptance | The acceptance file of Prebid.js, salesagent, and prebid-server (three runs, 2026-10-20 23:45 to 2026-10-21 01:38 UTC): the first sync spent 1,687 GraphQL points, 563 REST calls, and 3,872 s, and the two incremental syncs 179 and 180 points, no REST call, and 115 and 131 s. The runs ran `9270c64`, `c1e70d4` with uncommitted changes, and `d08d98d`. No run counted an item failure (`sync_runs.failures` is 0 on each, and `sync_failures` is empty). 59 stored PRs have more than 250 commits, read through compare, and 247 accounts only their commits link (225 `MDQ6...` and 20 `U_kgDO...` users, 2 `MDEy...` organizations) were stored by lookups that answered with the requested node id. The one compare link a lookup answered `NOT_FOUND` (`github_id` 1708813, 21 commit rows on 7 Prebid.js PRs) has no account in GraphQL either (its commit's `author.user` is null), and is stored as 5.2 stores such a link, which no run reports, so exit 2 would not show a node id form GraphQL answers `NOT_FOUND`. None of the 2,580 review-request rows names Copilot; its 224 reviews are stored under `BOT_kgDOCnlnWA`. Every stored PR is in coverage, and `DISMISSED` reviews are stored on Prebid.js (25), prebid-server (622, on 383 PRs), and salesagent (3) | the runs' `sync_runs` rows and `sqlite3` on the file, 2026-10-21 |
| Account profiles | The lookup of 100 ids with every D31 field, and with `organizations(first: 100)` added, costs 1 point by `rateLimit(dryRun: true)`. A real read of `patmmccann`'s 15 `User` profile fields and `prebid`'s 6 `Organization` fields returned with no error at 1 point. A real read of a `User`'s `email` failed: "The 'email' field requires one of the following scopes: ['user:email', 'read:user']" (the token here holds `gist`, `read:org`, `repo`, `workflow`); the dry run did not report it. GraphQL `organizations` listed 3 for `ChrisHuie` and `prebid` for `patmmccann`, while REST `/users/{username}/orgs` (public memberships) listed none for either. An organization's 8 public fields D31 names read on `prebid` and on `github` (not a member) at 1 point; on `github`, `webCommitSignoffRequired`, `membersCanForkPrivateRepositories`, `ipAllowListEnabledSetting`, `ipAllowListForInstalledAppsEnabledSetting`, and `notificationDeliveryRestrictionEnabledSetting` each returned FORBIDDEN ("does not have the right permission") and nulled the organization in its alias, `announcementBanner` and `samlIdentityProvider` (shown only to enterprise members and to owners) returned null, and `requiresTwoFactorAuthentication` and `organizationBillingEmail` need `admin:org`. On 20 organizations (`prebid`, `github`, and 18 others, 12 with a sponsors listing, `Homebrew` with 643 sponsors), `estimatedNextSponsorsPayoutInCents` and `monthlyEstimatedSponsorsIncomeInCents` were 0, `anyPinnableItems` true, `interactionAbility` null, and `pinnedItemsRemaining` from 0 to 6; `totalSponsorshipAmountAsSponsorInCents` was set only on `prebid` | live GraphQL and REST, 2026-10-18 |
| Empty fields | The plan's 5.8 documents run on 934 PRs (up to 100 numbers drawn per public tracked repository, all of them where it has fewer; seed 20261018), 400 node ids from those PRs (the first 400 by id: 328 users, 11 bots, and 61 review-request ids; no organization, mannequin, or enterprise account), and all 17 public tracked repositories, 148 points; then, after the additions (D13), on 206 PRs of the 11 added repositories (the same draw) and on the 28 tracked repositories other than `prebid-agent-skills`, 68 points. These fields were empty or identical everywhere and are not collected (D32): a PR's `createdViaEmail`, `locked`, and `activeLockReason`; a review's `createdViaEmail`, `isMinimized`, and `minimizedReason`; a comment's `isMinimized`, `minimizedReason`, `isPinned`, `pinnedAt`, and `pinnedBy`; a repository's `isFork`, `parent`, `isMirror`, `mirrorUrl`, `isTemplate`, `templateRepository`, `isUserConfigurationRepository`, `isEmpty`, `isDisabled`, `isLocked`, `lockReason`, `hasSponsorshipsEnabled`, `autoMergeAllowed`, `hasIssuesEnabled`, `hasPullRequestsEnabled`, `isBlankIssuesEnabled`, `isInOrganization`, `issueCreationPolicy`, `pullRequestCreationPolicy`, `codeOfConduct`, `fundingLinks`, `contactLinks`, `interactionAbility` (`NO_LIMIT` from `REPOSITORY` with no expiry where GitHub returned it), and `planFeatures`' `draftPullRequests` and `maximumAssignees`; and an organization's `anyPinnableItems` and `interactionAbility` on the 20 organizations read ("Account profiles"). Collected because they vary: a comment's `createdViaEmail` (true on 12 comments of the first sample), and once the added repositories were read, a repository's `forkingAllowed` and `hasVulnerabilityAlertsEnabled` (each constant across the 17), `usesCustomOpenGraphImage`, and `planFeatures`' `codeowners`, `teamReviewRequests`, and `maximumManualReviewRequests`. Kept though empty or constant: a repository's `isArchived` and `archivedAt` (the maintainer's exception for archive and visibility status; `isPrivate` and `visibility` vary across the 28); `owner`; the merge-method settings, `squashMergeAllowed`, `mergeCommitTitle`, and `mergeCommitMessage` among them; and `stack` and `stackEntry`, which the maintainer wants | the 5.8 documents over live GraphQL, 2026-10-18 |
| Added repositories | The 11 repositories D13 adds: `prebid-cache` (147 PRs; 119 default-branch commits, 27 since the floor), `user-sync` (3; 16, 16), `shared-id` (34; 90, 5), `Shared-id-v2` (7; 144, 26), `prebid-mobile-ios-adapters` (0; 11, 11), `prebid-js-build-generator` (26; 42, 6), `code-scanner` (0; 29, 29), `currency-file` (0; 3,002, 1,424), `currency-file-generator` (45; 97, 24), and the private `prebidjs-releaser` (21; 401, 291) and `automation-testing` (6; 73, 73); 289 PRs in all; each pushed to since 2025-04-18 | live GraphQL, 2026-10-18 |
| Field dispositions | Every field of the types the documents request was listed by introspection (`PullRequest`, `PullRequestReview`, `IssueComment`, `Repository`, `Label`, `User`, `Bot`, `Organization`, `Mannequin`, `EnterpriseUserAccount`, `Team`, `Milestone`, `ReviewRequest`, `PullRequestChangedFile`, `PullRequestStack`, `PullRequestStackEntry`, `Commit`, `GitActor`). 5.8 collects every free field except those a decision or a mechanical rule excludes. Decisions: the text D19 and D24 exclude (comment and review bodies, commit messages); every `Commit` field D19 leaves off a pull request's commits; CI status (`statusCheckRollup`, D13) and releases (`latestRelease`, D13); the profile fields D31 leaves out; and D32's: auto-merge and merge-queue fields (`autoMergeRequest`, `isInMergeQueue`, `isMergeQueueEnabled`, `mergeQueueEntry`, a repository's `mergeQueue`), projects (a PR's `projectItems` is a connection, which makes the batch 3 points, and GitHub refused a project's `title` to this token, which needs the `read:project` scope; section 9), `suggestedReviewers`, a referenced milestone's or team's own fields, and the fields "Empty fields" lists. Mechanical rules: connections are collected only as D29 admits them (section 9 lists those not admitted, among them `reviewThreads` and `onBehalfOf`; reactions are deferred, section 9); a field that takes an argument to look up another object (`issue`, `pullRequest`, `label`, `ref`, `object`, `release`, `milestone`, `discussion`, `environment`, `ruleset`, and the like) is a lookup, not a fact of the object; viewer-relative fields (`viewer*`, `isReadByViewer`, `isSponsoringViewer`, `sponsorshipForViewer*`) and fields GitHub returns only to members, admins, or maintainers (an organization's, D31; a repository's `pullRequestCreationCapConfig`, "Only visible to repository maintainers and administrators") describe the token, not the item; `tempCloneToken`, a credential, is never stored; the repository's file contents (`contributingGuidelines`, `issueTemplates`, `pullRequestTemplates`, and `codeowners`, which reports `CODEOWNERS` parse errors) are not stored, as `pr_files` stores no file content; an object of its own reached from a requested one (a milestone, a team, an organization's `sponsorsListing` and `itemShowcase`) is stored by the reference the documents select, if any, and its own fields are not; links and images built from stored keys (`url`, `resourcePath`, `permalink`, the `*Url` and `*ResourcePath` paths GitHub builds, `avatarUrl`, `sshUrl`, `openGraphImageUrl`) are not stored, while a link an owner sets (`homepageUrl`) is; GitHub's renderings of stored text (`bodyHTML`, `bodyText`, `titleHTML`, `descriptionHTML`, `shortDescriptionHTML`) and fields that restate a stored one (`closed`, `merged`, `abbreviatedOid`, a repository's `fullDatabaseId`) are not; and `hovercard` is GitHub's page context, not a fact about the item. A `User`'s `email` needs a token scope and organization memberships are token-dependent (D31). An `EnterpriseUserAccount`'s `enterprise` and a `Mannequin`'s `email` are not selected (section 8). Step 4b lists the fields of its REST responses (activity event, compare, compare file and commit) and of default-branch `Commit` the same way before its documents are fixed | live introspection and GraphQL, 2026-10-18 |
| Compare | REST compare reports no total additions or deletions (its keys: `status`, `ahead_by`, `behind_by`, `total_commits`, `base_commit`, `merge_base_commit`, `commits`, `files`, and URLs), and its files run from the merge base: for `5110f3d...fccb1f5` on `prebid/github-activity-db`, a diverged push, it returned status `diverged`, merge base `e0cbb98`, and 22 files summing to +458/-36, which is `git diff` from the merge base; from `5110f3d` itself the diff is 21 files, +455/-36 | live REST; local `git diff --shortstat`, 2026-10-18 |
| Default-branch history | `prebid/Prebid.js` `master` has 4,560 commits since 2023-01-01 (`history(since:)` `totalCount`); a page of 100 with the 5.8 fields costs 1 point | live GraphQL and dry run, 2026-10-18 |

## 8. Open items

| Item | Resolution point |
|------|------------------|
| Intermittent GraphQL failure rate at full scale (prototype needed 0 retries) | Step 5 measures it |
| Secondary-limit responses with more than one request in flight | Step 5 measures before any wider setting |
| Whether a heartbeat commit counts as repository activity for schedule disabling (undocumented); a disabled schedule fails no run | Not provable inside Step 6; the heartbeat is best effort. A schedule that stops starts no run, so this repository cannot see it; 5.6 states the consumer's staleness rule, and the dashboard's integration builds it (section 9, gap 107) |
| Issues read permission for the App | Step 6b's first manual run exercises it with a real token |
| GraphQL `rateLimit` readings alternating between two windows (seen with a personal token; GitHub documents that values vary by the region handling a request, but not differing `resetAt` values) | Step 2's guard handles it; Step 6 records App-token readings |
| Whether body edits, base-ref changes, review dismissals, and issue label events always change `updatedAt` (not measured; a state change is caught by the index) | Step 5's GraphQL timeline sample measures each kind, issue label events from Step 4a, counting only cases no later change follows; a kind not shown to change `updatedAt` gets an index-side check or a periodic re-hydration in pull request 5b, before Step 6a (gap 109) |
| The error body of a GraphQL secondary limit returned as HTTP 200 (not observed; documented only as "an error message") | Step 2 classifies by type and message; a message it does not recognize is "not understood" (5.3 step 4) and stops the run with exit 1, naming the message; Step 5 records secondary-limit responses |
| A PR file list above 3,000 entries (no tracked PR found) | Validation fails the item if `files` stops short of `totalCount`; Step 5 reports any |
| Compare fallback on a PR whose head fork was deleted (only a live fork tested) | A count mismatch fails the item; Step 5 reports any |
| Full bootstrap time, cost, and size are projections from two repositories | Step 5 measures them |
| Overflow continuations per request (25), the server retry delays (2, 4, 8 s), the reset margin (1 s), retry jitter (25%), the index boundary margin (60 s), the budget reserves (100 points, 100 calls), the longest accepted rate-limit wait (65 min), and the HTTP timeouts (read 30 s, connect 10 s) are chosen, not measured | Step 5 records secondary-limit responses, budget readings, and request times at these values |
| The cost of one commit per item in rollback-journal mode (not separated; whole-run times on three repositories are in section 7, "Step 3b-2a-2c acceptance") | Step 5 measures the full run |
| Whether the activity log's 2023-03-07 start is fixed or a rolling window (seen on two repositories) | Step 4b stores in `activity_first_event_at` the oldest event each complete read lists, one before the floor included; it reads the whole log every run, so an event that ages out leaves the database as GitHub stops listing it (D7) |
| Whether `history(since:)` filters on the committed or the authored date (only its `totalCount` was read) | Step 4b's recordings settle it before the coverage rule's wording for commits is final |
| Whether REST compare caps the `files` it returns for a large push (GitHub documents a cap of 300 files per comparison; not measured live) | Step 4b marks a list of 300 `files_at_cap` (gap 106) and records its largest push's file count |
| The REST cost of comparing every activity event on a first sync, and of reading every log in full each run (about one call per event compared, and one per 100 events read; Prebid.js's default branch has 4,560 commits since the floor) | Step 4b records it; Step 5 measures the full run |
| How a rebase merge appears in the activity log: whether its `pr_merge` event's `after` is the PR's `mergeCommit.oid` and its other commits come through the compare (the measured repositories hold merge-commit and squash merges only) | Step 4b records one on a tracked repository that allows rebase merges, before D30's join wording is final |
| Whether GitHub returns `EnterpriseUserAccount.id` to a token without the `read:enterprise` scope (the dry run validated the document; no tracked repository has such an actor yet) | Step 3b-1 tests the synthetic actor. A live occurrence decides between two outcomes (a prediction): an error at the alias's path is an item failure (exit 2), and an error without a path is fatal (5.3 step 4, exit 1), which Step 5's bootstrap would surface |
| Whether the scheduled App token can read every resolve field the token used here read (`diskUsage`, `webCommitSignoffRequired`, `hasVulnerabilityAlertsEnabled`, and `planFeatures` included) | Step 6b's App-and-token comparison, before any App run publishes to `db`; a field it cannot read is an error 5.3 step 4 classifies, and a difference blocks App publication (Step 6, gap 107) |
| A `DisconnectedEvent`'s `source` and `subject` (none in the 300 PRs read for section 7, "Closing-link timeline events"; GitHub's schema gives it `ConnectedEvent`'s fields) | Step 3b-2b-2 tests a synthetic one; a live one is recorded when seen |
| Whether a closing-keyword link can change without the PR's `updatedAt` changing (an issue transfer or deletion, a body edit, a base-ref or default-branch change); 5.4 then reports the difference as an error, unless it is a difference in the totals alone that the connected or disconnected events since the boundary cover (5.4) | Step 3b-2b-2's acceptance lists each such error with its PR and link; Step 5 reports any |
| Whether a pending `reviewRequests` entry for Copilot carries `BOT_kgDOCnlnWA` (the review-request events and the reviews do; no pending request was seen, section 7, "Copilot review requests" and "Step 3b-2a-2c acceptance") | Step 5 reports any Copilot request row; for a `databaseId` reported under a second node id, the writer keeps the stored node id, the run reports the second, and `account_node_ids` stores it (5.2, gap 103) |
| Whether to collect a `Mannequin`'s `email` ("The mannequin's email on the source instance"; the maintainer's profile decision named users and organizations only, and no mannequin was available to read, so whether GitHub requires a scope for it is unmeasured) | The maintainer decides; a live read on a mannequin comes first |
| Whether GitHub returns an `EnterpriseUserAccount`'s `enterprise` to the tokens used (an `Enterprise` object; no tracked repository has such an actor) | Not selected until a live occurrence is read |
| Whether GitHub returns an `EnterpriseTeam` requested reviewer's `id`, `fullDatabaseId`, and `slug` to the tokens used (the batch with them dry-runs at 2 points; no tracked repository has such a request) | Step 3b-1 tests a synthetic one; a live occurrence decides, as for `EnterpriseUserAccount.id`: an error at the alias's path fails the item (exit 2) |
| Whether the scheduled App token reads a `Team` requested reviewer and an `Organization` assignee as the token used here does (null or FORBIDDEN not measured) | Step 6b's App-and-token comparison, before any App run publishes to `db`; a FORBIDDEN fails the item, which 5.3 step 4 classifies, and any difference blocks App publication (Step 6, gap 107) |
| `--help` anywhere on a command line, or `--version` before the subcommand, exits 0 without doing any work (click's eager options), the code D25 publishes on, so a workflow input that expands to either would publish the unchanged database | Step 6's workflow passes inputs as validated option values, never as raw arguments |

## 9. Later work

Dashboard integration and replacement of its data layer (it reads the snapshot as 5.6 "Consumer
access" describes), a query API, AI enrichment (D12),
company affiliation, CI check status, releases, and additional repositories. The dashboard's
integration includes 5.6's staleness rule, the only check that can see a schedule that stopped
(gap 107). File classification
and dependency tagging are not planned; excluding paths from line counts is the consumer's:
`pr_files` keeps every path with its counts (D29).

Data the mirror does not keep (D7), each a further cost or table: earlier values GitHub overwrote
(logins, repository names, titles, edits, the state a review held before a dismissal), child rows
a re-hydration no longer returns (5.3 step 6), items GitHub no longer lists, a `NOT_FOUND` that a
later sighting clears, timeline events, and, across a bootstrap, the rows of untracked repositories,
accounts no stored row references, and D28's kept attribution.

Not collected by decision: auto-merge and merge-queue fields, projects, suggested reviewers, a referenced milestone's or team's own fields, and the fields empty or identical across the measured sample (D32; section 7, "Merge state and stacks" and "Empty fields"), the user profile fields beyond company, location, and website, a user's profile email, and organization memberships (D31), inline review comment rows (D5: a count per review), commit co-authors
(D15), commit messages, committers, committed dates, parents, and per-commit counts on pull requests (D19), and CI
status (D13). Not collected because they add cost no decision admits (D29): GitHub's
`latestReviews`, `latestOpinionatedReviews`, and `participants` (with them the batch costs 3
points, section 7), a review's `onBehalfOf` teams (by the formula, about 25 more points per
batch, nested in reviews). Projects need the `read:project` scope (the scheduled App a
Projects permission) and a connection that makes the batch 3 points; the maintainer accepts them
not being collected (2026-10-18).

Reactions, deferred by the maintainer (2026-10-18): GitHub's eight reaction counts on each PR,
review, and comment (`reactionGroups { content reactors { totalCount } }`), without who reacted or
when. Measured on 2026-10-18: the selection keeps the 25-PR batch at 2 points (dry run); GitHub
returns all eight groups (`THUMBS_UP`, `THUMBS_DOWN`, `LAUGH`, `HOORAY`, `CONFUSED`, `HEART`,
`ROCKET`, `EYES`), zeros included; of 311 recently updated PRs in Prebid.js, prebid-server,
salesagent, and github-activity-db, 23 had reactions on the PR (22 `THUMBS_UP`, 1 `EYES`), comments
held 28 and reviews none, and `chatgpt-codex-connector` gave 24 of the 51; 3 reactions came after
their PR's `updatedAt`, so a reaction does not always change it. Adding them bumps
`hydration_version`, one full re-hydration (about 950 points).

Data that cannot be re-fetched from GitHub and is stored in the published file (user tags; AI enrichment, if D12's planning puts it there) must be keyed by GitHub
identity (PR or issue `github_id`, user `github_id`), must not cascade from mirror rows, must be
produced where the published file is built, and must be copied from the last good snapshot across a
bootstrap. D28's attribution to accounts GitHub no longer returns cannot be re-fetched either, but
it is kept in the mirror rows and accepted as lost when a row is re-created (D28), so these rules do
not cover it. Nor do they cover the rows of untracked repositories (D27), which a bootstrap does not
resolve, or accounts no stored row references, which it never sights; a bootstrap drops both, and
besides these, D18's "nothing that cannot be re-fetched" holds.
