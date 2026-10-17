# PR Data Model

> **History.** This document describes the first schema's pull-request fields, which
> Step 3a of the [redesign plan](redesign-plan.md) replaced. It stays until Step 7
> rewrites the documentation; the current schema is in the
> [data dictionary](data-dictionary.md).

## Overview

Each Pull Request stores 21 data fields plus metadata. Fields are categorized by their sync behavior and data source.

## Tagging Systems

The project uses **three separate tagging mechanisms**:

| System | Storage | Source | Purpose |
|--------|---------|--------|---------|
| **github_labels** | JSON column | GitHub API | Labels from GitHub UI |
| **classify_tags** | String column | Agent | AI-generated classification |
| **user_tags** | Separate table | User via CLI | Custom user-created tags |

## Field Categories

### Immutable Fields (Set Once)

These fields are set when the PR is first synced and never updated.

| Field | Type | GitHub API Source |
|-------|------|-------------------|
| `number` | int | `pull.number` |
| `link` | str | `pull.html_url` |
| `open_date` | datetime | `pull.created_at` |
| `submitter` | str | `pull.user.login` |

### Synced Fields (Updated Until Merged)

These fields are updated on each sync while the PR remains open.

| Field | Type | GitHub API Source |
|-------|------|-------------------|
| `title` | str | `pull.title` |
| `description` | str | `pull.body` |
| `last_update_date` | datetime | `pull.updated_at` |
| `github_labels` | list[str] | `pull.labels[].name` |
| `files_changed` | int | `pull.changed_files` |
| `file_changes` | list[dict] | Files endpoint (filename, status, additions, deletions, changes) |
| `lines_added` | int | `pull.additions` |
| `lines_deleted` | int | `pull.deletions` |
| `commits_count` | int | `pull.commits` |
| `commits_breakdown` | list[dict] | Commits endpoint |
| `reviewers` | list[str] | `pull.requested_reviewers[].login` |
| `assignees` | list[str] | `pull.assignees[].login` |
| `participants` | dict | Reviews + Comments endpoints |

### Agent-Generated Fields

| Field | Type | When Generated |
|-------|------|----------------|
| `classify_tags` | str | On each sync (while open) |
| `ai_summary` | str | Once, when PR is merged |

### Merge-Only Fields

Set once when PR transitions to merged state.

| Field | Type | GitHub API Source |
|-------|------|-------------------|
| `close_date` | datetime | `pull.merged_at` or `pull.closed_at` |
| `merged_by` | str | `pull.merged_by.login` |

## PR State Machine

```
                    ┌──────────────┐
                    │     OPEN     │
                    │              │
                    │ - Sync fields│
                    │ - Update tags│
                    └──────┬───────┘
                           │
           ┌───────────────┼───────────────┐
           │               │               │
           ▼               │               ▼
    ┌──────────────┐       │        ┌──────────────┐
    │    MERGED    │       │        │    CLOSED    │
    │              │       │        │              │
    │ - Set close  │       │        │ - Set close  │
    │   _date      │       │        │   _date      │
    │ - Set merged │       │        │ - No merged  │
    │   _by        │       │        │   _by        │
    │ - Generate   │       │        │              │
    │   ai_summary │       │        │              │
    │ - Freeze all │       │        │ - Freeze all │
    │   fields     │       │        │   fields     │
    └──────────────┘       │        └──────────────┘
                           │
                    (Fields frozen,
                     no more updates)
```

## GitHub API Endpoints Required

To populate all fields, the sync process needs:

| Endpoint | Fields Populated |
|----------|------------------|
| `GET /repos/{owner}/{repo}/pulls` | Basic PR data (number, title, state, etc.) |
| `GET /repos/{owner}/{repo}/pulls/{number}` | Full PR details |
| `GET /repos/{owner}/{repo}/pulls/{number}/files` | `file_changes` |
| `GET /repos/{owner}/{repo}/pulls/{number}/commits` | `commits_breakdown` |
| `GET /repos/{owner}/{repo}/pulls/{number}/reviews` | Part of `participants` |
| `GET /repos/{owner}/{repo}/pulls/{number}/comments` | Part of `participants` |

## Sync Logic

### For Open PRs

```python
if pr.last_update_date > stored_pr.last_update_date:
    # Update all synced fields
    # Re-generate classify_tags
```

### For Newly Merged PRs

```python
if api_pr.state == "closed" and api_pr.merged:
    stored_pr.state = PRState.MERGED
    stored_pr.close_date = api_pr.merged_at
    stored_pr.merged_by = api_pr.merged_by.login
    # Generate ai_summary (async)
```

### For Already Merged PRs

```python
if stored_pr.state in (PRState.MERGED, PRState.CLOSED):
    # Skip - no updates needed
    pass
```

## Target Repositories

Tracked repositories are listed in `src/github_activity_db/tracking.toml`, one `[[repositories]]`
entry per repository with `owner`, `name`, and `group`. The file ships inside the package, so an
installed wheel reads the same list as a checkout. `github_activity_db.tracking` validates the file
at load, and `Settings.tracked_repos` returns the entries in file order. `TRACKING_CONFIG_PATH`
points at a different file.

## Field Validation Rules

### String Fields
- `title`: Max 500 characters
- `link`: Valid URL, max 500 characters
- `submitter`, `merged_by`: GitHub username, max 100 characters
- `classify_tags`: Max 500 characters

### JSON Fields
- `github_labels`: Array of strings
- `file_changes`: Array of `{filename: string, status: string, additions: int, deletions: int, changes: int}`
- `reviewers`, `assignees`: Array of GitHub usernames
- `commits_breakdown`: Array of `{date: ISO8601, author: string}`
- `participants`: Object mapping username → array of action strings

### Datetime Fields
- All datetimes stored in UTC
- ISO 8601 format from GitHub API
