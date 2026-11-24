---
name: debug-failing-test
description: Diagnose a failing or flaky Orbit test with minimal output: isolate it, read the short traceback, check fixtures, markers, time and ordering, then fix the cause. Use when pytest fails, a test passes alone but fails in the suite, or CI differs from local.
---

# Debug a failing test

1. Reproduce small: `uv run pytest -q -x --tb=short -p no:cacheprovider <file>::<test>`. Do not paste the whole suite output into the context.
2. Passes alone, fails in the suite: order or shared state. Run its whole file, then bisect with pairs of files:
   `uv run pytest -q -x tests/test_a.py tests/test_b.py` (no random-order plugin is installed, so the order is stable). Suspects: module-level state, `monkeypatch` missing, `lru_cache` on `get_settings`, files outside `tmp_path`.
3. Fails only on Postgres (CI): `ORBIT_TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest ...` against a throwaway database. Suspects: SQLite-only SQL,
   implicit ordering without `order_by`, naive datetimes, enum/CHECK differences.
4. "tests must not use the network": the autouse guard in `tests/conftest.py` caught a real connection; inject a fake behind the Protocol or `httpx.MockTransport`.
5. Time: pass `now=`/`today=`; tests around midnight, month ends and DST (`Europe/Prague`) need fixed instants. Money: integers.
6. Snapshot tests: `test_prompt_versions` (prompt text changed: bump the version, `make prompts-snapshot`, `docs/PROMPTS.md`), `test_evals`
   baseline (regenerate with the run command), `test_migrations` drift (missing migration, see `add-migration`), `scripts/gen_codemap.py --check` (`make codemap`).
7. Fix the cause, not the assertion. If the test was wrong, say why. A flaky test gets a note in `STATUS.md` ("Known flaky tests") until fixed.
8. Re-run: the test, its file, then `make changed`.
