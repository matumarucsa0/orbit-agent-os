# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break things).

## [Unreleased]

### Added
- Public-release files: LICENSE, SECURITY.md, CONTRIBUTING.md, AGENTS.md, issue and pull request
  templates, Dependabot configuration.

- Agent harness (no application behaviour change): `AGENTS.md` as a short index plus nested `AGENTS.md` files per
  directory, `STATUS.md` and `docs/HANDOFF_TEMPLATE.md`, `docs/CODEMAP.md` with generated `ROUTES.md`, `CONFIG.md` and
  `TESTMAP.md` (`scripts/gen_codemap.py`), `docs/AGENT_SETUP.md`, `docs/README.md`, `.claude/` (permissions, hooks, ten
  skills, four subagents, five commands), a `Makefile`, `scripts/check_docs.py` (dead paths, links, agent config) and
  `scripts/scan-personal.sh` (personal data and secrets scan with an untracked private pattern list), a CI job
  `agent-tooling`, and pre-commit hooks for the three checks.
- Test suite: `endpoint` and `slow` markers (`make fast`), an autouse guard against real network connections, tests
  that ignore Orbit environment variables, `tests/test_agent_tooling.py`.
- `scripts/select_tests.py --worktree` selects the tests for uncommitted changes (`make changed`).
- Docstrings on 57 more public service functions.

### Changed
- Fixtures replaced with synthetic data; the owner's name is now a setting (`OWNER_NAME`).
- CLAUDE.md now points to AGENTS.md.
- CI workflow runs with read-only token permissions.

## [0.1.0]

First tagged state of a project built in autumn 2026 by one person with AI coding sessions.
Tested by its own suite (about 1,750 tests, SQLite and Postgres); not verified end to end on a
deployed server with a real phone, see `docs/ROADMAP.md` for the exact status per phase.

### Added
- People and handles, money/item/favor ledger, birthdays and name days, promises, goals, notes.
- Bank CSV import (two formats) with idempotent dedupe and debt/payment matching as proposals.
- Proposal Inbox: every Claude or matcher result waits for human confirmation.
- Calendar (CalDAV, ICS feed), contact sync with preview and undo, meeting briefs.
- School module (timetable, exams, deadlines) and fitness/health ingest.
- Claude integration with structured output, model routing, masking of identifiers, a no-key
  fallback, and a worker that runs Claude Code on the server; weekly review and reports.
- ntfy notifications with signed single-use action tokens; iOS Shortcuts API.
- Server-rendered mobile-first web UI (FastAPI, Jinja2, HTMX); minimal read-only MCP server.
- Docker, production compose with Caddy HTTPS, encrypted nightly backups, restore drill.

### Known limits
- Single user, English UI, Anthropic only, no audit log, prompt-injection tests not yet written.
