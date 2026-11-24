# orbit/ (package root): app wiring, settings, auth, MCP

Purpose: `app.py::create_app` builds every client once and stores it on `app.state`; `deps.py` hands them to
routes; `settings.py` is the only config source; `mcp_server.py` is the read-only MCP tool; `tracing.py` OTel spans;
`cli.py` admin commands (list in `docs/CONFIG.md`). Map: `docs/CODEMAP.md`.

## Local invariants
- Settings: add a field to `Settings` (type, default, comment above it: `docs/CONFIG.md` is generated from them), add a
  commented line to `.env.example`, run `make codemap`. Secrets are `SecretStr`; optional services default to `None`
  and switch the feature off. Tests build `Settings(_env_file=None, ...)` through the `make_settings` fixture.
- `resolved_claude_backend` decides api / worker / fake / none. `ORBIT_CLAUDE_MODE=fake` wins over everything; a demo
  instance (`ORBIT_DEMO`) with no key and no worker also gets fake. Do not branch on the key elsewhere.
- Auth, four separate mechanisms (never mix them): API bearer `ORBIT_API_TOKEN` on `/api/v1/*` (`api/deps.py`, only
  `GET /api/v1/context` also accepts `ORBIT_MCP_TOKEN`); web password argon2 hash + signed session cookie
  (`web/auth.py::require_login`); ICS secret in the path (`web/calendar.py`, 404 on mismatch); signed single-use action
  tokens `/a/{token}` (`services/action_tokens.py`, HMAC with `ORBIT_SECRET_KEY`). The review and worker tokens
  (`ORBIT_REVIEW_TOKEN`) open only `/api/v1/review-agent/*` and `/api/v1/claude-worker/*`. Compare secrets with `secrets.compare_digest`.
- The app has no public OpenAPI docs on purpose. New error types: map them in `app.py::_register_error_handlers`.
- `mcp_server.py`: one static tool `get_context`; arguments are validated before any request (`validate_arguments`:
  allow-list preset, bounded dates and ids); the audit log never holds tokens or text. Do not add write tools
  without a `DECISIONS.md` entry (autonomy ladder, `docs/VISION.md` §3) and a threat-model update.
- `tracing.py` spans never carry prompt or answer text.

## Test it
`uv run pytest -q tests/test_settings.py tests/test_mcp_server.py tests/test_mcp_hardening.py tests/test_tracing.py tests/test_web_auth.py tests/test_api_auth.py`

## Common mistakes
- Reading `os.environ` directly instead of `Settings`. Logging a `SecretStr` through `.get_secret_value()`.
- Constructing a Claude client outside `create_app` (the budget guard and usage log are wired there).
- Adding a router to `app.py` without `require_login`, or to `api/` without `router.py` (see `orbit/api/AGENTS.md`).
