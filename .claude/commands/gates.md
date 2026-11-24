---
description: Run the quality gates in the cheapest order and report results (pytest, ruff, mypy, shellcheck, docs, codemap, scan, offline evals)
---

Follow the `run-gates` skill. Run `make changed`, then `make lint types`, then `make docs codemap-check scan`, then (if shell scripts changed) `make shell`.
If $ARGUMENTS contains "full", finish with `make gates` (full suite, about 3.5 minutes). Report each command with its result and test counts; stop at the first failure
and show only the failing test's short traceback. Do not claim a gate you did not run.
