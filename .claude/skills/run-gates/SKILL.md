---
name: run-gates
description: Run Orbit's quality gates (tests, ruff, mypy, shellcheck, docs paths, generated code map, personal-data scan, offline evals) in the cheapest order and report what passed. Use before saying work is done, before a commit, or when asked to "run the gates/checks".
---

# Run the gates

Cheapest first; stop at the first red and fix it before going on. All offline, no key.

1. `make changed`: only the tests your uncommitted changes can affect (`scripts/select_tests.py --worktree`). If it prints
   "Shared code changed", run `make test` instead.
2. `make lint types`: `ruff check`, `ruff format --check`, `mypy` (strict on `orbit/domain` and `orbit/services`).
   Fix formatting with `make fmt`.
3. `make docs codemap-check scan`: dead paths/links in docs and `.claude/`, generated docs current, personal-data scan.
   If codemap is stale: `make codemap`, then include the regenerated files in the change.
4. `make shell` when you touched `*.sh`.
5. Before reporting done: `make gates` (full pytest about 3.5 min, then `make evals`).

Report as: command, result, count. If you skipped one, say which and why. Never claim green without having run it.
Small output: failures print with `-q -x`; use `uv run pytest -q -x --tb=short <id>` to look at one.
For a failing test use the `debug-failing-test` skill.
