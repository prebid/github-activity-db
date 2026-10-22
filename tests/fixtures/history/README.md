# Earlier versions of repository files

Each file under `<short-sha>/` is `git show <sha>:<path>` copied unchanged to
`<short-sha>/<path>`: a repository file as an earlier commit holds it, which a test reads as input
(redesign plan D39). CI checks out with depth 1 and has no history, so tests read these copies
instead of git history.

The Blob column is the file's git blob id: the SHA-1 of `blob <size>`, a NUL byte, and the file's
bytes. `tests/test_history_fixtures.py` recomputes it from each copy and compares it with the row,
and requires exactly one row for every file git tracks under this directory other than this
README. With history, `git ls-tree <source commit> <path>` prints the mode, `blob`, the blob id
the source commit holds for `<path>`, and the path; the copy is unchanged when that id is the
row's. For example, `git ls-tree ea7d12e798fefe1971d1b409523a87f4d8caaf17 pyproject.toml` prints
the blob id `d865ae0d2390d2dbdf626f4f91fca7c1034045f7`, the row of `ea7d12e/pyproject.toml`.

| File | Source commit | Blob | Read by |
|------|---------------|------|---------|
| `21a4267/docs/data-dictionary.md` | `21a4267adc84e69327f2795d6f045ea5514881b1` | `c4e778a39ab2e98dc0ece20d27b80252dccd284c` | `tests/test_data_dictionary.py` |
| `77663a7/docs/data-dictionary.md` | `77663a7a1039fbf1892fa7fc42ef3ecefa55ea5f` | `f639e61164fde84407cc88049b8e227a1772b171` | `tests/test_data_dictionary.py` |
| `c1f474b/tests/query_versions.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `6269da133a613b36d1c419994f675be54839786a` | `tests/test_check_query_lock.py` |
| `21a4267/tests/query_versions.json` | `21a4267adc84e69327f2795d6f045ea5514881b1` | `bd342e4e9242249ebff54fc1a8c7838f2a6fb8f2` | `tests/test_check_query_lock.py` |
| `6fedb05/tests/schema_versions.json` | `6fedb05fde7bd26cc79b0a7eb2dd91d020655a58` | `30f862a867e675a17f76a7ad9ab0b54fee408de7` | `tests/test_check_query_lock.py` |
| `21a4267/tests/schema_versions.json` | `21a4267adc84e69327f2795d6f045ea5514881b1` | `067e5a8c6ec870ce6da995afa4a9f8d651f4f96e` | `tests/test_check_query_lock.py`, `tests/test_data_dictionary.py` |
| `77663a7/tests/schema_versions.json` | `77663a7a1039fbf1892fa7fc42ef3ecefa55ea5f` | `bc40c9c73ddce85f01a11dbc177fd2f0306e5fb1` | `tests/test_check_query_lock.py`, `tests/test_data_dictionary.py` |
| `ea7d12e/pyproject.toml` | `ea7d12e798fefe1971d1b409523a87f4d8caaf17`, the parent of the commit that declared httpx (gap 76) | `d865ae0d2390d2dbdf626f4f91fca7c1034045f7` | `tests/test_declared_dependencies.py` |
| `c1f474b/tests/fixtures/source/recorded/pull_request/prebid__Prebid.js/13481.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `71524134cbf78e867460ee4eff36995bb506eda1` | `tests/source/test_recordings.py` |
| `c1f474b/tests/fixtures/source/recorded/overflow/PR_kwDOBUaT8s6WsAjJ/comments/0e3c0d53c5b8c0eb.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `5314ac3ff708182c8b2a6005c0d1e8a8e0b9d733` | `tests/source/test_recordings.py` |
| `c1f474b/tests/fixtures/source/recorded/resolve/prebid__go-gdpr.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `e77b1704c546ac63854505b882f133cb77445e1e` | `tests/source/test_recordings.py` |
