# Database Schema

## Overview

The database uses SQLite with async access via aiosqlite. Schema migrations are managed by Alembic.

**Connection String:** `sqlite+aiosqlite:///./github_activity.db`

## Entity Relationship Diagram

```
┌─────────────────┐       ┌─────────────────────┐
│  repositories   │       │     user_tags       │
├─────────────────┤       ├─────────────────────┤
│ id (PK)         │       │ id (PK)             │
│ owner           │       │ name (unique)       │
│ name            │       │ description         │
│ full_name (UQ)  │       │ color               │
│ is_active       │       │ created_at          │
│ last_synced_at  │       └──────────┬──────────┘
│ created_at      │                  │
└────────┬────────┘                  │
         │                           │
         │ 1:N                       │ N:M
         │                           │
         ▼                           ▼
┌─────────────────────────────────────────────────┐
│                  pull_requests                   │
├─────────────────────────────────────────────────┤
│ id (PK)                                         │
│ repository_id (FK) ──────────────────────────┐  │
│ number                                       │  │
│ [... 21 data fields ...]                     │  │
│ created_at, updated_at                       │  │
├─────────────────────────────────────────────────┤
│ UNIQUE(repository_id, number)                   │
└─────────────────────────────────────────────────┘
         │
         │ N:M
         ▼
┌─────────────────────┐
│   pr_user_tags      │
├─────────────────────┤
│ pr_id (FK, PK)      │
│ user_tag_id (FK,PK) │
│ created_at          │
└─────────────────────┘
```

## Tables

### 1. repositories

Tracks which GitHub repositories are being monitored.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment ID |
| owner | VARCHAR(100) | NOT NULL | GitHub org/user (e.g., "prebid") |
| name | VARCHAR(100) | NOT NULL | Repo name (e.g., "prebid-server") |
| full_name | VARCHAR(200) | UNIQUE, NOT NULL | Full path (e.g., "prebid/prebid-server") |
| is_active | BOOLEAN | DEFAULT TRUE | Whether to sync this repo |
| last_synced_at | DATETIME | NULLABLE | Last successful sync timestamp |
| created_at | DATETIME | DEFAULT NOW | Record creation time |

**Indexes:** `full_name` (unique)

### 2. pull_requests

Stores all PR data with 26 columns total.

| Column | Type | Constraints | Sync Behavior |
|--------|------|-------------|---------------|
| id | INTEGER | PRIMARY KEY | - |
| repository_id | INTEGER | FK(repositories.id) | - |
| **Immutable Fields** |
| number | INTEGER | NOT NULL | Set once |
| link | VARCHAR(500) | NOT NULL | Set once |
| open_date | DATETIME | NOT NULL | Set once |
| submitter | VARCHAR(100) | NOT NULL | Set once |
| **Synced Fields** |
| title | VARCHAR(500) | NOT NULL | Until merged |
| description | TEXT | NULLABLE | Until merged |
| last_update_date | DATETIME | NOT NULL | Until merged |
| state | VARCHAR(6) | NOT NULL | Until merged |
| files_changed | INTEGER | DEFAULT 0 | Until merged |
| lines_added | INTEGER | DEFAULT 0 | Until merged |
| lines_deleted | INTEGER | DEFAULT 0 | Until merged |
| commits_count | INTEGER | DEFAULT 0 | Until merged |
| github_labels | JSON | DEFAULT [] | Until merged |
| filenames | JSON | DEFAULT [] | Until merged |
| commits_breakdown | JSON | DEFAULT [] | Until merged |
| reviewers | JSON | DEFAULT [] | Until merged |
| assignees | JSON | DEFAULT [] | Until merged |
| participants | JSON | DEFAULT {} | Until merged |
| **Agent Fields** |
| classify_tags | VARCHAR(500) | NULLABLE | Until merged |
| **Merge-Only Fields** |
| close_date | DATETIME | NULLABLE | Set on merge |
| merged_by | VARCHAR(100) | NULLABLE | Set on merge |
| ai_summary | TEXT | NULLABLE | Set on merge |
| **Metadata** |
| created_at | DATETIME | DEFAULT NOW | - |
| updated_at | DATETIME | ON UPDATE NOW | - |

**Indexes:**
- `repository_id` (foreign key)
- `(repository_id, number)` (unique constraint)

### 3. user_tags

User-created tags applied via CLI.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| id | INTEGER | PRIMARY KEY | Auto-increment ID |
| name | VARCHAR(100) | UNIQUE, NOT NULL | Tag name (e.g., "needs-review") |
| description | VARCHAR(500) | NULLABLE | Optional description |
| color | VARCHAR(7) | NULLABLE | Hex color (e.g., "#ff0000") |
| created_at | DATETIME | DEFAULT NOW | Record creation time |

### 4. pr_user_tags (Junction Table)

Many-to-many relationship between PRs and user tags.

| Column | Type | Constraints |
|--------|------|-------------|
| pr_id | INTEGER | PK, FK(pull_requests.id) ON DELETE CASCADE |
| user_tag_id | INTEGER | PK, FK(user_tags.id) ON DELETE CASCADE |
| created_at | DATETIME | DEFAULT NOW |

## JSON Column Schemas

### commits_breakdown

Array of commit metadata:

```json
[
    {
        "date": "2024-01-15T10:30:00Z",
        "author": "username1"
    },
    {
        "date": "2024-01-16T14:20:00Z",
        "author": "username2"
    }
]
```

### participants

Dictionary mapping usernames to their actions:

```json
{
    "username1": ["comment", "approval"],
    "username2": ["comment", "changes_requested", "comment"],
    "username3": ["review"]
}
```

**Possible actions:**
- `comment` - Left a comment
- `approval` - Approved the PR
- `changes_requested` - Requested changes
- `review` - Left a review (without approval/rejection)
- `commit` - Pushed commits

### github_labels, filenames, reviewers, assignees

Simple string arrays:

```json
["label1", "label2", "label3"]
```

## Migrations

Migrations are managed by Alembic with async support.

### Commands

```bash
# Check current migration
uv run alembic current

# Generate new migration
uv run alembic revision --autogenerate -m "description"

# Apply all pending migrations
uv run alembic upgrade head

# Rollback one migration
uv run alembic downgrade -1

# View migration history
uv run alembic history
```

### Migration Files

Located in `alembic/versions/`:

| Migration | Description |
|-----------|-------------|
| `01421d8dfaeb_initial_pr_schema.py` | Creates all 4 tables |

## Usage Examples

### Async Session Context Manager

```python
from github_activity_db.db import get_session, PullRequest
from sqlalchemy import select

async with get_session() as session:
    # Query all open PRs
    result = await session.execute(
        select(PullRequest).where(PullRequest.state == "open")
    )
    prs = result.scalars().all()
```

### Creating Records

```python
from github_activity_db.db import get_session, Repository

async with get_session() as session:
    repo = Repository(
        owner="prebid",
        name="prebid-server",
        full_name="prebid/prebid-server"
    )
    session.add(repo)
    # Commits automatically on context exit
```

### Querying with Relationships

```python
from github_activity_db.db import get_session, PullRequest
from sqlalchemy.orm import selectinload

async with get_session() as session:
    result = await session.execute(
        select(PullRequest)
        .options(selectinload(PullRequest.repository))
        .options(selectinload(PullRequest.user_tags))
        .where(PullRequest.number == 123)
    )
    pr = result.scalar_one()
    print(pr.repository.full_name)
    print([tag.name for tag in pr.user_tags])
```

## Transaction Management

### Default Behavior

By default, `get_session()` auto-commits on successful exit and rolls back on exception:

```python
async with get_session() as session:
    # All operations here...
    pass
# Auto-commits on exit
```

### Batch Commits with CommitManager

For bulk operations, use `CommitManager` to commit in batches and prevent data loss on failure:

```python
import asyncio
from github_activity_db.db import get_session
from github_activity_db.github.sync import CommitManager

async with get_session(auto_commit=False) as session:
    write_lock = asyncio.Lock()
    commit_manager = CommitManager(session, write_lock, batch_size=25)

    for pr in prs_to_process:
        # Process PR...
        await session.flush()
        await commit_manager.record_success()  # Auto-commits at batch_size

    await commit_manager.finalize()  # Commit any remaining
```

**Key parameters:**
- `auto_commit=False`: Disables auto-commit on session exit (CommitManager handles it)
- `write_lock`: Serializes commits with concurrent flush operations
- `batch_size`: Number of items per commit batch (default: 25)

### Configuration

The batch size is configurable via environment variable:

```bash
# Set batch size to 50 PRs per commit
SYNC__COMMIT_BATCH_SIZE=50 uv run ghactivity sync all --since 2024-10-01
```

### Failure Recovery

With CommitManager, only the current uncommitted batch is lost on failure:

```
Processing 100 PRs with batch_size=25:
- PRs 1-25: Committed ✅
- PRs 26-50: Committed ✅
- PRs 51-75: Committed ✅
- PRs 76-85: [FAILURE] → Rolled back ❌
- Result: 75 PRs saved (3 complete batches)
```
