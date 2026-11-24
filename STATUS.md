# STATUS

Session memory for agents and humans. Read it first, update it last (convention in `docs/AGENT_SETUP.md`; blank form in
`docs/HANDOFF_TEMPLATE.md`). Keep it short and honest: facts with the command that proved them, not hopes.

Last updated: 2026-11-24 (agent-harness pass; no application behaviour changed)

## Current state
- Orbit 0.1.0 plus the unreleased public-release work (`CHANGELOG.md`). The built phases are ticked in `docs/ROADMAP.md`. Not built: 18b, 18d, 18e (school extras), 35 (nutrition,
  no spec), 36 (places, transit) and 37 (training coach; both specified, waiting for the owner's OK).
- Agent harness added: nested `AGENTS.md` files, `.claude/` (permissions, hooks, skills, subagents, commands), `docs/CODEMAP.md` and
  its generated companions, `Makefile`, `scripts/check_docs.py`, `scripts/gen_codemap.py`, `scripts/scan-personal.sh`.

## Verified (command, result, date)
| What | Command | Result |
|---|---|---|
| Full test suite | `make test` | 2192 passed in about 206 s (SQLite, 2026-11-24) |
| Fast loop | `make fast` | see `docs/AGENT_SETUP.md` for the measured time and count |
| Lint, format, types | `make lint types` | clean (ruff, mypy strict on `orbit/domain` and `orbit/services`) |
| Shell scripts | `make shell` | clean |
| Docs paths, generated docs, scan | `make docs codemap-check scan` | clean |
| Offline evals | `make evals` | recorded mode 100 percent on 152 cases; matches the committed baseline |
| Demo boot with fake Claude | `make demo` | passes (`/healthz`, `/api/v1/context`, 401 without token) |

## Open (not verified, honest list)
- **Nothing has been verified on a real deployed server with a real phone**: the unticked boxes in `docs/ROADMAP.md` (Shortcuts, iCloud calendar and
  contacts, ntfy timing, the Claude worker VM, a real bank month, the school feeds with a real token, Liftoff) are all still open.
- No live eval run: the 100 percent is recorded mode (hand-written answers), it measures the graders and the pipeline, not a model.
  The offline heuristic baseline for review summaries agrees with the labels on 15 of 20 (kappa 0.48), below the trust threshold; no live LLM judge has been calibrated yet (see `docs/EVALS.md`).
- The Postgres CI job and the Docker backup drill are not run by the agent harness; they run in CI only.
- Known security gaps (`docs/threat-model.md` section 8): no login throttle, no CSRF token on web POST routes.
- Only capture, classify and review have eval suites; meeting link, brief, draft, report and school read do not.
- Cache tokens are not tracked in cost figures.

## Known flaky or slow tests
- None known flaky. The suite is order-independent as far as run (full run, and the `fast` subset).
- Slow files (several seconds each, marked `slow`): see `SLOW_FILES` in `tests/conftest.py`; `test_select_tests.py` is the slowest (about 37 s).

## Next tasks (suggested order)
1. Real-server verification pass (deploy per `docs/DEPLOY.md`, then the checklists at the end of `docs/SHORTCUTS.md`); tick the ROADMAP boxes it proves.
2. A live eval run with a cost cap (`docs/EVALS.md`) to get a real pass rate and pass^k; add suites for the five prompts without one.
3. Login throttle and CSRF token (threat model section 8).
4. Docstrings on the public service functions still lacking them (report: `uv run python scripts/missing_docstrings.py`; not enforced).

## Decisions pending (owner)
- Phase 35: how food gets into Orbit (source not chosen).
- Phase 36 and 37: answers to the open questions of `docs/SPEC.md` §2.37 and §2.38 (transit source first).
- Whether the weekly review's 400,000-character context budget should be lower (`docs/ROADMAP.md` Phase 28).
- MCP: raw workouts endpoint and a health-days tool (`docs/SPEC.md` §2.39).

## Conventions
- Migration numbers: take the next free one (`ls migrations/versions | tail -1`; `ROADMAP` phase text that names a number is historical).
- Do not commit unless asked. The owner's identity in git is the no-reply address; never put a real name, address or number in the repo.
