# tests: pytest suite (about 2170 tests, SQLite by default, Postgres in CI)

Purpose and map: `docs/CODEMAP.md` section 7 and the generated `docs/TESTMAP.md`. `conftest.py` has the fixtures.

## Fixtures and markers
- `db` (fresh schema, in-memory SQLite or `ORBIT_TEST_DATABASE_URL`) for service tests; `app`, `client`, `api` (bearer header),
  `web` (logged in) for endpoint tests; `make_settings(**overrides)` / `settings`. Settings never read `.env` here.
- Markers (added automatically in `conftest.py`): `endpoint` (boots the app, the slowest per test) and `slow` (files in
  `SLOW_FILES`). `make fast` runs `-m "not endpoint and not slow"`; `make changed` runs only affected tests.
- An autouse fixture fails any socket connection to a non-local host. Fake external services behind their Protocol or use
  `httpx.MockTransport`; Claude is a small fake class or `FakeClaude`; the clock is a `now=`/`today=` argument.

## Local invariants
- Fixtures under `tests/fixtures/` are synthetic and anonymised (invented people, `example.*` hosts, documentation IBANs).
  Never paste a real export. `.pre-publish-allow` lists the invented numbers the scan accepts.
- Parsers get fixture-file tests; every service function gets a unit test; every route gets an auth test (401 without token).
- `tests/prompt_hashes.json` is generated (`make prompts-snapshot`); `tests/route_inventory.py` lists routes for walk-all tests.
- Tests must be order-independent and not touch files outside `tmp_path`. `test_select_tests.py` guards the CI test selector:
  a new top-level directory or file kind may need a rule in `scripts/select_tests.py`.
- Concurrency tests use a file database (`concurrent_database_url`), because in-memory SQLite shares one connection.
- Naming: `test_<area>.py`; web tests `test_web_<area>.py`; API tests `test_api_<area>.py`.

## Run
`make one T=tests/test_x.py::test_name` | `make changed` | `make fast` | `make test` (206 s, 2171 tests, measured).
Small output: `-q -x -p no:cacheprovider`; one failing test with its traceback only: `uv run pytest -q -x --tb=short <id>`.

## Common mistakes
- Hitting the network or a real path (the guard fails it). A test that depends on another test's data or the wall clock.
- Using mocks for the database instead of the `db` fixture. Putting real-looking personal data in a fixture.
