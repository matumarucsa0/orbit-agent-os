# evals: golden cases, graders, runner

Purpose: regression net for the three Claude-driven features that have suites: `capture`, `classify`, `review` (the other
five prompts have none yet). Details and numbers: `docs/EVALS.md`.

## Key files
`cases/<suite>.jsonl` (input, `expected`, hand-written `recorded` answer, tags) | `cassettes/negatives.jsonl` (deliberately
wrong answers, each must fail named graders) | `graders.py` (deterministic graders per suite in `GRADERS`) | `loader.py` (`SUITES`,
`OUTPUT_MODELS`, request builders) | `run.py` (CLI, modes `recorded`/`fake`/`live`, cost cap) | `judge.py`, `calibrate.py` (one
optional LLM judge for review summaries) | `results/baseline-recorded.json` (committed; CI diffs against it).

## Local invariants
- `recorded` mode replays hand-written answers: it tests graders, case files and plumbing, not a model. `fake` mode runs
  `FakeClaude`. Only `live` measures a model, costs money and needs `ANTHROPIC_API_KEY`: never run it unasked (denied in `.claude/settings.json`).
- All cases use the fictional persona Alex Novak and invented people. No real data (`tests/test_evals.py` checks).
- CI requires `--min-pass-rate 1.0` in recorded mode and an unchanged baseline. After adding or changing cases, regenerate it:
  `uv run python -m evals.run --mode recorded --json evals/results/baseline-recorded.json`, then update the counts in `docs/EVALS.md`.
- Every new grader needs a negative in `cassettes/negatives.jsonl` that fails it (`tests/test_evals.py` enforces coverage).
- `tests/test_evals.py` pins 40 to 60 cases per suite, about a tenth adversarial, and the grader count (13 + 6 + 11): update the
  numbers when you add graders or push a suite past the limits.
- Ids are stable (`cap-001`, `cls-001`, ...); do not renumber.

## Run
`make evals` (recorded, offline, 100 percent expected) | `make evals-fake` | `uv run pytest -q tests/test_evals.py`.

## Common mistakes
- Changing a prompt without running the evals before and after. Editing the baseline by hand. A `recorded` answer written to
  pass a grader instead of being the answer a good model would give.
