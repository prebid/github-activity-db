# Earlier versions of repository files

Each file under `<short-sha>/` is `git show <sha>:<path>` copied unchanged to
`<short-sha>/<path>`: a repository file as an earlier commit holds it, which a test reads as input
(redesign plan D39). CI checks out with depth 1 and has no history, so tests read these copies
instead of git history.

| File | Source commit | Read by |
|------|---------------|---------|
| `21a4267/docs/data-dictionary.md` | `21a4267adc84e69327f2795d6f045ea5514881b1` | `tests/test_data_dictionary.py` |
| `77663a7/docs/data-dictionary.md` | `77663a7a1039fbf1892fa7fc42ef3ecefa55ea5f` | `tests/test_data_dictionary.py` |
| `c1f474b/tests/query_versions.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `tests/test_check_query_lock.py` |
| `21a4267/tests/query_versions.json` | `21a4267adc84e69327f2795d6f045ea5514881b1` | `tests/test_check_query_lock.py` |
| `6fedb05/tests/schema_versions.json` | `6fedb05fde7bd26cc79b0a7eb2dd91d020655a58` | `tests/test_check_query_lock.py` |
| `21a4267/tests/schema_versions.json` | `21a4267adc84e69327f2795d6f045ea5514881b1` | `tests/test_check_query_lock.py`, `tests/test_data_dictionary.py` |
| `77663a7/tests/schema_versions.json` | `77663a7a1039fbf1892fa7fc42ef3ecefa55ea5f` | `tests/test_check_query_lock.py`, `tests/test_data_dictionary.py` |
| `ea7d12e/pyproject.toml` | `ea7d12e798fefe1971d1b409523a87f4d8caaf17`, the parent of the commit that declared httpx (gap 76) | `tests/test_declared_dependencies.py` |
| `c1f474b/tests/fixtures/source/recorded/pull_request/prebid__Prebid.js/13481.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `tests/source/test_recordings.py` |
| `c1f474b/tests/fixtures/source/recorded/overflow/PR_kwDOBUaT8s6WsAjJ/comments/0e3c0d53c5b8c0eb.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `tests/source/test_recordings.py` |
| `c1f474b/tests/fixtures/source/recorded/resolve/prebid__go-gdpr.json` | `c1f474bf86117a38a5b6c45dd5808cf99e3f0da0` | `tests/source/test_recordings.py` |
