# Testing Guide

How the test suite is built and what it counts as evidence. The plan of record is
[docs/redesign-plan.md](redesign-plan.md); Step 3 states the evidence rules quoted here.

---

## Principles

- **Real components, faked boundaries.** GitHub is the only fake: the real `GitHubSource` runs
  over `httpx.MockTransport`, answering from responses recorded from public repositories.
  Databases are real SQLite files built by `alembic upgrade head`.
- **Direct evidence.** A check is tested with planted inputs on both sides of the boundary it
  states, tampered databases and artifacts, and a positive control showing it sees what it
  checks; a fix comes with a regression test that fails against the parent commit's code.
- **No skipped tests** (D22). CI runs `scripts/check_junit.py --max-skipped 0`, which fails a run
  where any test was skipped (pytest reports `xfail` as skipped) or none ran.
- **Time.** Source tests pass a `FakeClock`, so waits and retries are asserted, never slept.
  Each wait on a thread or subprocess has an explicit timeout; `tests/test_suite_rules.py`
  fails on a `subprocess` wait without one and on any use of that module it does not model.
  The module reached without an import statement (`importlib.import_module`, `__import__`,
  `sys.modules`) and process APIs outside `subprocess` (`os.system`, `asyncio` subprocesses)
  are outside its scan.

---

## Layout

```
tests/
├── conftest.py              # The session template database and its per-test copies
├── db/
│   ├── alembic_support.py   # Running the real migrations; test-only revisions after head
│   ├── schema_support.py    # Structural schema readings for the model comparison
│   ├── seed.py              # Valid rows through the models' tables, foreign keys on
│   ├── test_migrations.py   # The chain and alembic/env.py
│   ├── test_schema.py       # Head against the models and the frozen view SQL; CHECKs
│   ├── test_views.py        # The consumer views on seeded data
│   ├── test_types.py        # UtcDateTime
│   ├── test_application_engine.py, test_tracked_database.py
├── source/                  # GitHub source layer (redesign plan Step 2)
│   ├── recordings.py        # Unit format, shared by the recorder and the replay
│   ├── replay.py            # httpx transport answering from units, with fault injection
│   ├── recorder.py          # Recording scenarios and the recording transport
│   └── test_*.py
├── fixtures/source/
│   ├── recorded/            # Units recorded from public repositories (scripts/record_fixtures.py)
│   └── synthetic/           # Error responses: documented, observed, or constructed; each names which
├── data_dictionary.py       # Reader for docs/data-dictionary.md
├── query_versions.json      # hydration_version -> hash of the query documents
├── schema_versions.json     # schema version -> consumer (table or view, column) pairs
├── test_cli_app.py, test_cli_github.py   # The CLI, in process and through the installed script
├── test_data_dictionary.py  # The dictionary and the schema-version lock
└── test_*.py                # Settings, logging, scripts, import boundaries, build inputs
```

---

## Databases

- **One template per session.** `head_template` (`tests/conftest.py`) is built once by
  `alembic upgrade head` and made read-only; `migrated_database` is a writable copy per test,
  and `test_engine` the application engine (`build_engine`) over it, so foreign keys and the
  busy timeout match production. `metadata.create_all` appears only in `tests/db/test_schema.py`,
  as the other side of the model comparison.
- **The real environment.** Migration tests run the repository's `alembic.ini`, `env.py`, and
  revisions. `write_revision` adds a test-only revision after the head through
  `version_locations`, which is how a planted failure, a foreign-key violation, and a batch alter
  under the views are run through `env.py`. `env.py` is loaded fresh by every command, so its
  `MigrationRefused` is matched by class name.
- **Seeding.** `tests/db/seed.py` writes rows through the models' tables with every column a
  test does not care about filled with a valid default; GitHub ids come from one process-wide
  counter, so separate transactions never collide.
- **Schema comparison.** `schema_of` reads columns (`PRAGMA table_xinfo`), indexes, foreign keys,
  and every item of each table's definition, which is where constraint names and CHECK text
  live; each reading is tested against planted differences.
- **Dictionary and schema version.** `tests/test_data_dictionary.py` compares
  `docs/data-dictionary.md` with a migrated database in both directions.
  `tests/schema_versions.json` maps each schema version (`PRAGMA user_version`) to its consumer
  (table or view, column) pairs and, from version 2, to the SHA-256 of each view's frozen SQL;
  a test fails when the current entry differs from the columns the dictionary marks consumer,
  and on pull requests the CI step that checks the hydration-version lock rejects a removed
  entry, a pair removed from one, and a view digest removed or changed in one, so removing or
  renaming a consumer column, or changing a view, needs a new schema version. Pairs and views
  may be added to the newest entry only.

## GitHub Source Layer: Recorded Responses

`tests/source` runs the real source layer (`GitHubSource`) against `httpx.MockTransport`. The
transport answers from **units**: one file per resolve, label page, index page, pull request
alias, overflow continuation, user, or compare page, plus one GraphQL `rateLimit` reading and
one REST `/rate_limit` body (`rate_limit/graphql.json`, `rate_limit/rest.json`), recorded from
public repositories. Every
query document the source layer sends has a recorded unit except the labels and closing-issue
continuations: no tracked PR has more than 100 labels or closing issues, so their tests use
synthetic pages, and a change to those documents is caught by the hydration-version lock test,
not by "re-record". The only label page comes from `golang/go` (144 labels), because no tracked
repository has more than 100.

- **Assembled, not replayed whole**: a batch, split, or solo retry is rebuilt from per-alias units,
  so a change of batch size needs no new recording.
- **Stamped**: each GraphQL unit carries the SHA-256 of the normalized request that produced it:
  the whole document for resolve, label, index, user, and GraphQL rate-limit units; for a pull
  request alias or an
  overflow continuation, the request reduced to that one alias, so batch size and neighbouring
  aliases do not change it. The replay computes the same stamp from each request and refuses a
  unit with another, so changing a query without re-recording fails the suite with "re-record".
  It also refuses a batch or continuation whose aliases do not each bind their own variables.
  Compare units are stamped with `COMPARE_STAMP` (`recordings.py`), the text of the REST request
  they answer; their key holds the base, head, page, and page size. The REST `/rate_limit` unit
  is stamped with `REST_RATE_LIMIT_STAMP`.
- **Re-recording**: `GITHUB_TOKEN="$(gh auth token)" uv run --no-sync python scripts/record_fixtures.py`
  replaces `tests/fixtures/source/recorded/` only after every scenario recorded without a failed
  item or user lookup. It refuses a private repository and keeps response bodies plus the
  `date`, `retry-after`, and `x-ratelimit-*` headers, never request headers.
  `tests/source/recorder.py` lists the scenarios and why each was chosen. `--only rate-limits`
  records the two rate-limit units alone and keeps every other unit. Readings differ between
  recordings, so re-recording the rate limits (a full run does too) means updating the readings
  `tests/test_cli_github.py` asserts.
- **Synthetic responses** (`tests/fixtures/source/synthetic/`) are error responses. Each file's
  `source` says where its shape comes from: GitHub's documentation (quoted), a live observation
  (dated), or a construction for a case GitHub does not document. A test that needs a variant of
  a recorded item modifies the unit in the test and says which one.
- **Faults**: `replay.fault(respond, when=..., times=...)` answers matching requests with a status
  or exception; `replay.modify(change, ..., headers=...)` edits the recorded body, and optionally
  the headers, of matching requests. The `replay` fixture fails a test whose fault was never
  served, or served fewer times than given, so a predicate that misses cannot pass silently.
- **Round trip**: `tests/source/test_recorder.py` records the replay and gets back every unit, so
  the recorder's split and the replay's assembly agree for every operation.
- **Time**: tests pass a `FakeClock`, so waits and retries are asserted, never slept.
- **Hydration version**: `tests/query_versions.json` maps each `<kind>:<version>` to the hash of
  that kind's documents. A test fails when the current documents differ from the current
  version's entry; on pull requests, CI runs the base commit's copy of
  `scripts/check_query_lock.py` (the pull request's own copy where the base has none), which
  rejects an edited or removed entry, so a document change needs a new version.

---

## CLI

`tests/test_cli_github.py` runs commands with typer's `CliRunner` and an 80-column console, the
width Rich uses when none of stdin, stdout, and stderr is a terminal and `COLUMNS` is unset, as
in CI, and replaces `common.open_source` with a `GitHubSource` over the replay; the fixture fails
a test that leaves a request unanswered or a fault unserved. `tests/conftest.py` unsets
`COLUMNS` and `LINES` before the CLI's consoles are built at import, and every variable
`Settings` reads before each test, so neither a terminal size nor a setting the developer's
shell exports changes a result.
`tests/test_cli_app.py` also runs the installed console script (`.venv/bin/ghactivity`) in a
subprocess, for exit codes and tracebacks outside `CliRunner`.

---

## Running Tests

```bash
uv run pytest                                  # everything
uv run pytest tests/db -v                      # one directory
uv run pytest tests/db/test_views.py -k relationships
uv run pytest --junitxml="${TMPDIR:-/tmp}/junit.xml" \
  && uv run python scripts/check_junit.py "${TMPDIR:-/tmp}/junit.xml" --max-skipped 0
```

---

## CI/CD Integration

### Pre-commit Hooks

```bash
# Install hooks
uv run pre-commit install

# Runs automatically on commit:
# - ruff (lint + format) and mypy, from uv.lock
# - file checks and the type:ignore / noqa limits
# pytest is not a hook; run it before pushing.
```

### GitHub Actions

`.github/workflows/check.yml` runs on pull requests to `main` and pushes to `main`:

- `lint and types`: `uv lock --locked` (uv.lock matches pyproject.toml), then pre-commit over
  all files; on pull requests, the base commit's `scripts/check_query_lock.py` (the pull
  request's own copy where the base has none) against the hydration-version and schema-version
  locks.
- `tests (Python 3.12 / 3.13)`: pytest with JUnit output; `scripts/check_junit.py --max-skipped 0`,
  which fails unless at least one test ran and none failed, errored, or was skipped (pytest
  reports `xfail` as skipped, so an `xfail` also fails the job); and `alembic upgrade head` plus
  `alembic check` on a fresh database.
- `built wheel`: builds the wheel with build dependencies hash-checked against
  `build-constraints.txt`, installs the `uv.lock` dependencies with hash checking and
  then the wheel into a new environment, runs `uv pip check`, and runs `scripts/wheel_smoke.py`
  and `ghactivity --help` from outside the checkout.

---

## Test Typing

Mypy runs on `src/`, `tests/`, and `scripts/`, with relaxed strictness for tests.

### Mypy Configuration

Tests run with relaxed mypy settings (see `pyproject.toml`):

```toml
[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false
disallow_any_generics = false
check_untyped_defs = true
warn_return_any = false
warn_unused_ignores = true
```

This allows flexibility for test-specific patterns (mocks, fixtures) while still catching type errors in test logic.

### Running Mypy on Tests

```bash
# Check tests (runs with relaxed settings)
uv run mypy tests/

# Check everything
uv run mypy src tests scripts

# Pre-commit runs mypy on src/, tests/, and scripts/
uv run pre-commit run mypy --all-files
```

```bash
uv run mypy src tests scripts                  # what the pre-commit hook runs
```

---

## Troubleshooting

### "Database is locked"

Build engines with `build_engine` (as the `test_engine` fixture does), which uses `NullPool` and
the application's connection settings, and close every `sqlite3` connection a test opens.

### Flaky Async Tests

Avoid timing-dependent assertions:
```python
# Bad - timing dependent
await asyncio.sleep(0.1)
assert task.done()

# Good - wait for completion
await asyncio.wait_for(task, timeout=1.0)
```
