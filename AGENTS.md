# Orbit: agent instructions

Shared by every coding agent (Claude Code, Codex, Cursor, ...) and by human contributors. This file is an
index: rules and commands here, detail in the files it points to. Local rules live in nested `AGENTS.md`
files (the closest one wins). Harness setup: [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md).

## What Orbit is
Single-user, self-hosted personal operating system (people, money / item / favor debts, birthdays, bank-import
flagging, meeting briefs, promises, goals, school timetable, fitness and health). Claude proposes, the owner
confirms; Apple devices are the interface (Shortcuts, ntfy, ICS feed, mobile-first HTMX web UI). Python 3.12 + `uv`,
FastAPI + Jinja2 + HTMX, SQLAlchemy 2.0 + Alembic (SQLite dev, Postgres prod), Pydantic v2, APScheduler, `anthropic` SDK.

## Start here (read in this order, only as far as the task needs)
1. [STATUS.md](STATUS.md): current state, open items, flaky tests, pending decisions. Update it at the end of a session.
2. [docs/CODEMAP.md](docs/CODEMAP.md): module map, data flows, extension recipes ("add a migration / prompt / route ...").
3. `docs/VISION.md` (north star, module contract), `docs/SPEC.md` (what is built; sections 3 data model, 4 API),
   `docs/ROADMAP.md` (one phase at a time), `docs/DECISIONS.md` (do not reverse an entry without asking).
4. [docs/README.md](docs/README.md) indexes every doc, one line each.

## Commands (all offline; `.claude/settings.json` sets fake Claude, no scheduler, a scratch database)
| Need | Command |
|---|---|
| one test | `make one T=tests/test_money.py::test_name` (or `uv run pytest -q -x <path>`) |
| tests my change can affect | `make changed` |
| fast loop (skips endpoint and slow tests) | `make fast` |
| full suite / all gates | `make test` / `make gates` (pytest, ruff, mypy, shellcheck, docs, codemap, scan, evals) |
| lint, format, types | `make lint`, `make fmt`, `make types` |
| offline evals (recorded cassettes) | `make evals` (live evals cost money: never run them unasked) |
| demo server with fake Claude | `make demo` (smoke test); `uv run fastapi dev orbit/main.py` needs secrets, see README |
| regenerate generated docs | `make codemap` (CODEMAP, ROUTES, CONFIG, TESTMAP; CI fails when stale) |
| personal-data scan | `make scan` |

## Layout
`orbit/api` JSON routers (`/api/v1`, bearer token) | `orbit/web` pages + Jinja/HTMX templates | `orbit/services` business
logic, no FastAPI imports | `orbit/domain` models, enums, money, normalisation | `orbit/integrations` Claude client and
prompts, FakeClaude, bank parsers, CalDAV/CardDAV, ntfy, school sources | `orbit/jobs` scheduler | `orbit/mcp_server.py`
read-only MCP tool | `migrations/` Alembic | `evals/` golden cases and graders | `tests/` + `tests/fixtures/` (synthetic) |
`scripts/` tooling | `deploy/`, `docker/` ops | `docs/` all documentation.

## Non-negotiable rules
1. **Claude proposes, the user confirms.** LLM output and automatic matching (debts from bank rows, payment matches,
   dictated notes/promises) become a `Proposal` in the Inbox; nothing touches ledger, promises, notes or people until
   confirm (`services/inbox.py`). No auto-settle, ever. Only exception: a user-pressed contact sync may fill an *empty*
   field without a proposal (SPEC §2.13; shown in the review, logged with Undo).
2. **Money = integer minor units** (`amount_minor`) + ISO 4217 `currency`; never floats; balances per currency; conversion is
   display-only. Use `orbit/domain/money.py`.
3. **Times in UTC** (`UTCDateTime`, never naive datetimes), shown in `Europe/Prague` (`settings.timezone`). Birthdays and
   name days are month/day (+ optional year).
4. **Secrets only from env**, as `SecretStr`; a feature is off when its settings are unset. Never print, log or commit values.
5. **Claude calls go through `orbit/integrations/claude.py`** (API client, worker queue, FakeClaude share its
   request/output types), structured output validated by Pydantic. Model names come from env, never hardcoded. Outside text
   goes inside `untrusted(kind, text)`; IBANs and phones are masked (`mask_sensitive`). Every feature must work with no key. Send Claude generous context
   (DECISIONS #7), but never raw identifiers.
6. **Idempotency**: bank rows by hash (+ occurrence counter), calendar by event UID, contacts by vCard UID, pushes by dedupe key.
7. **Normalise with the helpers** in `orbit/domain/normalize.py` (`normalize_text`, `normalize_handle`, `normalize_phone`);
   search and matching are diacritics-insensitive ("Jan" finds "Ján").
8. **Services flush, callers commit.** Services never import FastAPI; routes and jobs commit.
9. Prompts are versioned: a text change bumps the version, the snapshot and `docs/PROMPTS.md` (`add-prompt-with-evals` skill).
10. Never guess a bank CSV layout: build parsers from a real anonymised sample in `tests/fixtures/`, or stop and ask.
11. Keep it simple: single user, no premature abstraction; ask before adding a dependency. A new module follows the
    module contract (`docs/VISION.md` §4: typed models, idempotent ingest, context provider, proposal kinds, tests).
    Auth rules (API token, web password, ICS token, signed action tokens): `orbit/AGENTS.md`.

## Gotchas that cost time
- Enum columns are `VARCHAR(20)` + CHECK (`domain/base.py::enum_column`): a new enum value or a longer name needs a migration.
- Every `orbit/web` router needs `dependencies=[Depends(require_login)]`; every `orbit/api` router must be included in
  `orbit/api/router.py` (that is what adds the bearer token). `tests/test_mcp_hardening.py` walks all routes.
- SQLite and Postgres must both work: no SQLite-only SQL, use `op.batch_alter_table` in migrations. CI runs both.
- `mypy` is strict on `orbit/domain` and `orbit/services`; ruff line length 100; `migrations/versions` is excluded from ruff.
- Tests: no real network (an autouse guard fails it), external services are faked behind small Protocols, no mocks of the DB.
- `tests/prompt_hashes.json` and `evals/results/baseline-recorded.json` are generated: never edit by hand (a hook blocks it).

## Definition of done
Code + tests + docs in the same change (SPEC section, ROADMAP tick, DECISIONS entry for a real choice, CHANGELOG
`[Unreleased]` line, `make codemap` if names/settings/routes/migrations changed) and `make gates` green. Report what
you ran and what you did not. Update STATUS.md. Do not commit unless asked; conventional commits (`feat:`, `fix:` ...).

## Off-limits and data hygiene
- Never read, print, edit or commit `.env*` (only `.env.example`), `*.db`, `*.dump`, `*.age`, `backups/`,
  `.pre-publish-patterns`, `.claude/settings.local.json` or `*.shortcut` files. Never run live evals, push, send a push
  notification, copy a database or hit a real service without being asked.
- Never edit an applied migration (already committed): add a new revision. `seed-demo --force` and `alembic downgrade` are denied.
- Fixtures and examples are synthetic: invented people, merchants, hosts (`example.*`), numbers. Refer to the person who
  runs Orbit as "the owner"; no real names, addresses, phones, employers, schools or hostnames in code, docs or tests.
- Before publishing a fork or export run `make scan` (see CONTRIBUTING.md).

## How to work
- Plan before touching more than two files (list files and tests first); one concern per change.
- Spec before code for anything new (module, agent, data source, anything changing what is collected or sent to Claude):
  write the `docs/SPEC.md` section with open questions and wait for the owner's OK. Ask about product decisions; decide
  engineering details yourself and copy the shape of a similar existing module.
- Check requests against `docs/VISION.md`: no isolated trackers, no writes without a proposal, no breadth before the proof point.
- Add or update tests with the change; fix flaky tests instead of retrying. Keep failing-test output small (`-q -x`).
- Skills (`.claude/skills/`), subagents (`.claude/agents/`) and slash commands (`.claude/commands/`) are repo-specific
  playbooks; use them (add-migration, add-prompt-with-evals, add-api-route, run-gates, debug-failing-test ...).

## Language
Code, comments, UI: English. User content is often Slovak/Czech: handle diacritics everywhere.
