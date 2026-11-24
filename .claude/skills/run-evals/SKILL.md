---
name: run-evals
description: Run Orbit's evals the safe way (offline recorded cassettes or the FakeClaude mode) and explain results; live mode against a real model only when the owner explicitly asks and sets a cost cap. Use after a prompt, grader or case change.
---

# Run the evals

Docs: `docs/EVALS.md`. Suites: `capture`, `classify`, `review` (not every prompt has one).

## Offline (default, free, no key, no network)
- `make evals`: recorded mode. Replays the hand-written `recorded` answers through the graders; must be 100 percent and the output must equal
  `evals/results/baseline-recorded.json`. It guards graders, case files and the pipeline, **not** a model.
- `make evals-fake`: the rule-based FakeClaude; expected to be imperfect (capture 28.6, classify 80.4, review 51.1 percent); shows how far rules get.
- `uv run pytest -q tests/test_evals.py`: case validity, negatives (each wrong answer must fail its graders), judge calibration.
- Subset: `uv run python -m evals.run --mode recorded --suite capture`.

## Baseline
After adding cases or graders: `uv run python -m evals.run --mode recorded --json evals/results/baseline-recorded.json`; review the diff, update the counts in
`docs/EVALS.md`. Never edit the baseline by hand (a hook blocks it).

## Live (costs money; denied in `.claude/settings.json`)
Only when the owner asks. Needs `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` in the environment (never in a file or a message). Run it from the owner's shell
or the manual `Evals (live)` workflow, with `--k 3 --max-eur 2` (cap) and `--json`. A prompt change is judged on the live pass rate and pass^k, not on recorded mode.
Report: suite, k, pass rate, pass^k, estimated euros, failed case ids.
