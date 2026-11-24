# Agent setup

How Orbit is prepared for AI coding agents, what each piece does, and how to use it with Claude Code, Codex or Cursor. The
guiding idea: the repo carries its own memory and guardrails, so a fresh agent session can be productive and safe without a human
re-explaining anything.

## 1. The pieces

| Piece | Where | What it does |
|---|---|---|
| Shared instructions | `AGENTS.md` (root, under 120 lines) | The one index every tool reads: commands, layout, rules, definition of done, off-limits. |
| Local instructions | `orbit/`, `orbit/domain/`, `orbit/services/`, `orbit/integrations/`, `orbit/api/`, `orbit/web/`, `orbit/jobs/`, `migrations/`, `tests/`, `evals/` (each an `AGENTS.md`, 20 to 50 lines) | Invariants, how to test and common mistakes for that directory; the closest file wins. |
| Claude-specific notes | `CLAUDE.md` (short) | Points to `AGENTS.md`; plan mode, subagent and model hints. |
| Code map | `docs/CODEMAP.md` + generated `docs/ROUTES.md`, `CONFIG.md`, `TESTMAP.md` | Where things are, data flows, recipes. Generated parts come from `scripts/gen_codemap.py`. |
| Session memory | `STATUS.md`, `docs/HANDOFF_TEMPLATE.md` | Current state, verified vs open, next tasks, flaky tests, pending decisions. |
| Permissions and env | `.claude/settings.json` | Least-privilege allow list, deny list, safe environment. |
| Hooks | `.claude/hooks/*.sh` | Format on edit, block forbidden edits, light gate before stopping. |
| Skills | `.claude/skills/<name>/SKILL.md` | Step-by-step playbooks loaded on demand (progressive disclosure). |
| Subagents | `.claude/agents/*.md` | `code-reviewer`, `security-reviewer`, `test-writer`, `explorer`. |
| Slash commands | `.claude/commands/*.md` | `/gates`, `/evals`, `/demo`, `/review`, `/plan-feature`. |
| Commands | `Makefile` | One name per task (`make fast`, `make changed`, `make gates`, `make evals`, `make codemap`, `make scan`). |
| Regression net | `tests/` (about 2170), `evals/` (152 cases, 30 graders) | Tests guard behaviour; recorded evals guard the graders and the Claude pipeline. |
| Guards in CI | `.github/workflows/ci.yml` job `agent-tooling`, `.pre-commit-config.yaml` | Dead paths, stale generated docs, personal data and secrets. |

Context loading, cheapest first: `AGENTS.md` (always) -> `STATUS.md` -> `docs/CODEMAP.md` -> the nested `AGENTS.md` of the directory being edited ->
a skill when its description matches -> `docs/SPEC.md` sections by grep. Agents should search big files, not read them whole (see section 9).

## 2. Permissions and environment (`.claude/settings.json`)

Allowed without a prompt: `make`, `uv run pytest|ruff|mypy`, `alembic check|current|heads|history|upgrade head|revision --autogenerate`, the offline eval
modes, the codegen and check scripts, `seed-demo` (no `--force`), `scripts/scan-personal.sh`, `scripts/demo_smoke.sh`, `shellcheck`, read-only `git`
(`status`, `diff`, `log`, `show`, `blame`, `ls-files`), `ls`, `wc`, `rg`. Everything else asks.

Denied (a deny rule beats an allow rule): reading `.env`, `.env.local|prod|production|staging`, `.pre-publish-patterns`, `*.db`, `*.sqlite*`, `*.dump`, `*.age`,
`*.shortcut`, `backups/`, uploaded school files and the MCP audit log; editing `.env*` and databases; `rm -rf`, `git push --force`/`-f`, `git reset --hard`,
`git clean`, `docker volume rm|prune`, `docker compose down -v`, `DROP TABLE|DATABASE`, `drop_all`, `sqlite3`, `psql`, `alembic downgrade`, `seed-demo --force`,
the CLI commands that touch real data or networks (`copy-db`, `test-push`, `smoke-test`, `export`), live evals (`--mode live`, `--judge live`),
`scripts/claude_worker.py`, `restore.sh`, `backup-drill.sh`. Plain `git push`, `git commit`, `curl` and `docker` are not allowed or denied: they ask.

Environment defaults for every command the agent runs: `ORBIT_CLAUDE_MODE=fake` (Claude answers offline from rules: no key, no spend, no network),
`SCHEDULER_ENABLED=false`, `ORBIT_OTEL_EXPORTER=none`, `DATABASE_URL=sqlite:///./agent-scratch.db` (git-ignored scratch file, never `orbit.db`), `OWNER_NAME=Alex`,
and `NTFY_URL`, `CALDAV_URL`, `CARDDAV_URL` pointed at a dead loopback port so even a configured topic or account cannot reach a real service.
The test suite ignores these on purpose: `tests/conftest.py` removes every Settings variable from the environment for each test, so tests assert the real
defaults, and an autouse guard fails any connection to a non-local host.

These rules are guardrails against mistakes, not a sandbox: a determined shell command can still get around a pattern. For stronger isolation run the agent in a
container or VM without the owner's data, and keep secrets out of its environment.

## 3. Hooks (small, fast, shell-safe)

| Hook | Event and matcher | Behaviour |
|---|---|---|
| `guard-edits.sh` | PreToolUse `Edit\|Write\|MultiEdit` | Exit 2 (blocks, message shown to the agent) for `.env*` (not `.env.example`), `.pre-publish-patterns`, database and dump files, `tests/prompt_hashes.json` and `evals/results/baseline-recorded.json` (regenerate them), and any `migrations/versions/*.py` that is already committed (applied: add a new revision). |
| `format-python.sh` | PostToolUse `Edit\|Write\|MultiEdit` | `ruff format` on the edited `.py` file. Never blocks. |
| `stop-gate.sh` | Stop | When the tree has changes: ruff on changed files, `scripts/check_docs.py`, `gen_codemap.py --check`, `scripts/scan-personal.sh`, and the tests `select_tests.py --worktree` picks if at most 12 files. Exit 2 returns the failure to the agent once; a second stop in a row passes (`stop_hook_active`). Typical cost 10 to 40 s. |

Each hook reads the tool call as JSON on stdin, quotes every path, uses `set -uo pipefail` where failure must not abort, and passes shellcheck (`make shell`).
To disable one temporarily, remove it in `.claude/settings.local.json` (git-ignored); do not edit the committed file for a personal preference.

## 4. Skills, subagents, commands

Skills (`name` equals the directory, a one-sentence `description` that says when to use it): `run-gates`, `add-migration`, `add-prompt-with-evals`,
`add-integration`, `add-api-route`, `run-evals`, `seed-demo-and-screenshot`, `pre-publish-scan`, `debug-failing-test`, `write-adr`. Only the descriptions are
in context until a skill is used. Subagents run in their own context and return a short report: use `explorer` for "where is X" (it starts at `docs/CODEMAP.md`),
`code-reviewer` after a change, `security-reviewer` for prompts, auth, tokens and untrusted input (mapped to the OWASP LLM Top 10 and `docs/threat-model.md`),
`test-writer` for tests only. Slash commands wrap the common flows (`/gates`, `/evals`, `/demo`, `/review`, `/plan-feature`).

`scripts/check_docs.py` validates all of it: each skill has a matching `name` and a description, each agent and command has a description, hook scripts exist and
are executable, `settings.json` is valid JSON, and none of it is git-ignored.

## 5. Generated documentation that cannot rot

`make codemap` runs `scripts/gen_codemap.py`. It rewrites the marked tables in `docs/CODEMAP.md` (modules with their docstring and main names, proposal kinds, prompts,
scheduler jobs, migrations) and the whole of `docs/ROUTES.md`, `docs/CONFIG.md` and `docs/TESTMAP.md`. It reads the code (AST and the registries), never `.env` or a
database, and writes no line numbers, so it changes only when a name, setting, route, prompt, job, migration or test import changes. `--check` fails when a file is
stale; CI (`agent-tooling`) and pre-commit run it. The hand-written parts of the docs are checked by `scripts/check_docs.py`: every relative link resolves, every
backticked repo path exists (and `path::name` finds the name in that file), every Makefile target named in a `make ...` command exists, `AGENTS.md` stays under 120 lines, `CLAUDE.md` under 40,
nested `AGENTS.md` between 15 and 60.

## 6. Evals as the regression net

Unit tests prove code behaviour. Evals (`evals/`, `docs/EVALS.md`) prove that the Claude-facing pipeline still grades answers correctly: `make evals` replays 152
hand-written answers through 30 deterministic graders and compares with the committed baseline (CI runs it too). A prompt change must run them before and after
(`add-prompt-with-evals`). Only live mode measures a model; it costs money, so it is denied for agents and run by the owner (manual workflow `evals-live.yml`).

## 7. Session notes

Agents read `STATUS.md` first and update it last: what was verified (with the command), what is open, flaky tests, next tasks, decisions pending the owner. Longer
unfinished work uses `docs/HANDOFF_TEMPLATE.md`. Decisions that matter go to `docs/DECISIONS.md` (`write-adr`), not into chat. `STATUS.md` states what is not verified:
real-server verification is still open.

## 8. Using it with each tool

- **Claude Code**: reads `CLAUDE.md` (which points to `AGENTS.md`), `.claude/settings.json` (permissions, env, hooks), skills, subagents and commands automatically.
  Personal overrides go in `.claude/settings.local.json` (git-ignored). `.mcp.json` registers the read-only Orbit MCP server (needs `ORBIT_MCP_TOKEN`; see `docs/MCP.md`).
- **Codex and other `AGENTS.md` tools**: read `AGENTS.md` and the nested ones (closest wins). They do not read `.claude/`: the commands in `AGENTS.md` and the
  `Makefile` are the whole interface, the skills are plain Markdown you can point the agent at (`.claude/skills/<name>/SKILL.md`), and safety relies on that tool's own
  sandbox and approval settings: run it with network off and without your `.env`.
- **Cursor**: reads `AGENTS.md`. For a rule or command not covered, add `.cursor/rules` that only say "follow AGENTS.md"; do not copy content (one source of truth).
- Any tool: run `make gates` before declaring done; `make changed` while working.

## 9. Context cost

Large or generated files are not meant to be read whole: `uv.lock` (about 237 KB), `docs/SPEC.md` (about 220 KB; use the section index with grep `^## \|^### `),
`evals/cases/*.jsonl`, `tests/fixtures/` (one SIS page is 44 KB), `docs/images/`, `orbit/web/static/vendor/htmx.min.js`, `docs/ROUTES.md` and `docs/TESTMAP.md`
(search them). `.claudeignore` lists them for tools that honour it; Claude Code itself enforces access through the deny rules in `.claude/settings.json`, and the
nested `AGENTS.md` files keep local rules short so the root file stays an index. `docs/README.md` gives one line per document.

## 10. Measured numbers

Measured on one development machine (SQLite, no Postgres), 2026-11-24:

| Command | What | Time |
|---|---|---|
| `make test` | all 2192 tests | about 206 s |
| `make fast` | `-m "not endpoint and not slow"` | FAST_RESULT |
| `make changed` | tests a change can affect | depends on the change: seconds to the full suite |
| `make lint types` | ruff + mypy | about 10 to 20 s |

Where time goes: the 345 endpoint tests build the app per test, and nine files hold most of the rest of the slow tests (`SLOW_FILES` in `tests/conftest.py`).

## 11. Keeping it healthy

- Changed a name, setting, route, job, prompt or migration: `make codemap`. Added a doc that links or names paths: `make docs`.
- A skill or instruction contradicts the code: fix the instruction in the same change; `scripts/check_docs.py` catches dead paths, not wrong prose, so review
  prose when you touch the thing it describes.
- Missing docstrings on public service functions: `uv run python scripts/missing_docstrings.py` (report only; add them when you touch the function).
- Review this file and `AGENTS.md` when the harness changes (new tool, new gate, new rule). Keep `AGENTS.md` under 120 lines: move detail into the nested files.
