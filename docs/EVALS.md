# Evals

Orbit asks Claude for structured answers in three places where a wrong answer costs the user a tap
or a wrong proposal: capture (a dictation becomes debts, promises, settles), bank-row
classification, and the weekly review. `evals/` is a small harness that grades those answers
without a human reading them. Every write in Orbit still waits for the user's confirmation; the
evals measure how often the proposals that reach the Inbox are right.

## What is in the box

| Path | What |
|---|---|
| `evals/cases/capture.jsonl` | 56 cases: Slovak, Czech and English dictations (new debts, splits, settles, promise updates, appointments, school deadlines), 6 adversarial |
| `evals/cases/classify.jsonl` | 51 cases: bank rows to `likely_shared`, `personal`, `incoming_from_person`, `ignore` (refunds, split dinners, exchanges, ambiguous rows), 5 adversarial |
| `evals/cases/review.jsonl` | 45 cases: weekly-review snapshots with signals (clashes, overdue, cadence, pending and rejected proposals), 5 adversarial |
| `evals/cassettes/negatives.jsonl` | 45 deliberately wrong answers; each names the graders that must fail |
| `evals/graders.py` | 30 deterministic graders (13 capture, 6 classify, 11 review, counting `schema_valid` once per suite) |
| `evals/judge.py`, `evals/calibrate.py`, `evals/calibration/review_judge_labels.jsonl` | the one optional LLM judge and its 20 labelled samples |
| `evals/run.py` | the CLI |
| `evals/results/baseline-recorded.json` | committed result of the recorded run; other files in `results/` are git-ignored |

All cases use the fictional persona Alex Novak and invented people. No real data.

## Running

```
uv run python -m evals.run --mode recorded                 # no key; this is what CI runs
uv run python -m evals.run --mode fake                     # the offline FakeClaude
uv run python -m evals.run --mode live --k 3 --max-eur 2   # real model, costs money
uv run python -m evals.calibrate                           # judge agreement (offline heuristic baseline)
uv run python -m evals.calibrate --judge live              # agreement of the real judge
```

Options: `--suite capture,classify,review` (default all), `--json PATH` (report file),
`--min-pass-rate X` (exit 1 below it), `--judge` (live only). Without `--json` a Markdown and a
JSON report are written to `evals/results/`.

Live mode needs `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` (optionally `ANTHROPIC_MODEL_FAST`,
`ANTHROPIC_MODEL_DEEP`, `ANTHROPIC_DEEP_EFFORT`). It uses the real `ClaudeClient`, so the prompts
under test are the prompts in production. The cost is an estimate from token counts; set
`EVALS_EUR_PER_MTOK_IN` and `EVALS_EUR_PER_MTOK_OUT` to your prices (the defaults are deliberately
high). `--max-eur` stops the run at the cap and marks the report as truncated.

## Case format

One JSON object per line:

```json
{"id": "cap-018", "suite": "capture", "tags": ["sk", "settle"],
 "input": {...}, "expected": {...}, "recorded": {...}}
```

- `input` is the request: for capture the fields of `CaptureRequest` (text, today, candidates with
  their open debts and promises, goals, courses, corrections, rejected), for classify the
  `TransactionRow` list plus `shared_min`, for review the fields of `ReviewRequest` (the two
  snapshots as JSON strings, the signals, pending and rejected proposals). `evals/loader.py` turns
  it into the real dataclasses.
- `expected` holds ids as integers and amounts as integer minor units (`1250` is 12.50). Capture
  lists debts, settles, promises, promise updates, appointments and school deadlines exactly (an
  empty list means "none allowed"). `forbidden_kinds` names output lists that must stay empty and
  `must_not_contain` lists strings that must not appear anywhere in the answer. Classification
  expects a list of acceptable labels per ref (more than one for honestly ambiguous rows). Review
  expects which meetings a conflict must cite, which proposal kinds are forbidden, and which people
  must not get a follow-up.
- `recorded` is the cassette: a valid JSON instance of the real Pydantic output model
  (`CaptureExtraction`, `TransactionClassification`, `ReviewOutput`), written by hand as a plausible
  correct answer.

## How the graders work

A grader is a pure function from (case, output) to pass or a reason. A case passes only when every
grader passes. The answer is first validated against the real Pydantic model (`schema_valid`); if
that fails, the other graders fail too.

- Capture: `refs_grounded` (every person, debt, promise, goal and course reference exists in the
  request), exact-match graders for debts (person, direction, type, amount in minor units,
  currency), settles, promises (with goal link where given), promise updates, appointments and
  school deadlines; `amounts_integer` (exact amounts with at most two decimals, none on item or
  favor debts), `amounts_grounded` (the number is in the dictation, or listed for spelled-out
  numbers), `no_invented_people` (names match the text by accent-free stem), `no_invented_dates`,
  `forbidden_absent`.
- Classify: `refs_exact` (every ref once, no extra), `labels_match`, `forbidden_labels_absent`,
  `confidence_in_range`, `forbidden_strings_absent`.
- Review: `ids_grounded` (every goal, person, meeting, promise id exists in the snapshots),
  `numbers_grounded` (numbers above 10 or with decimals must occur in the input; small counts are
  not checkable), `dates_grounded`, `quotes_grounded` (quoted text is in the input),
  `conflicts_cited` (a signalled clash must be cited, a quiet week must have no conflict),
  `slips_cited`, `forbidden_proposals_absent`, `proposal_hygiene` (at most about 10 proposals, at
  most 7 priorities, a goal re-rank lists every active goal once, nothing repeats a pending or
  rejected item), `forbidden_strings_absent`, `summary_shape`.

The adversarial cases carry instructions inside the data (a bank description, a meeting title, a
contact note, a pending-proposal line, a corrections list) and assert that the answer still
validates and no forbidden kind appears.

### Do the graders discriminate?

`evals/cassettes/negatives.jsonl` holds 45 wrong answers (wrong amount, invented id, followed
injection, missing row, invented quote, and so on). `tests/test_evals.py` requires that each one
fails the grader it names and that every grader is named by at least one negative.

### The LLM judge (optional, live only)

`evals/judge.py` judges only the review summary (grounded, specific, honest, useful, short). It never
decides whether a case passes. Before its verdicts are reported, `evals/run.py --judge` measures its
agreement with `calibration/review_judge_labels.jsonl` (20 samples, 11 good and 9 bad) and only
reports verdicts if accuracy is at least 0.85 and Cohen's kappa at least 0.6. The judge prompt treats
the summary as data. `python -m evals.calibrate` computes the agreement for any judge function; the
tests check the arithmetic with stand-in judges.

## Adding a case

1. Append a line to the right `cases/*.jsonl` with a new unique `id` and tags. Keep the fictional
   persona; never paste real messages or bank rows.
2. Write `expected` first from the rules in the prompt, then write `recorded` as the answer you would
   accept. Run `uv run python -m evals.run --mode recorded`.
3. If the case is meant to catch a failure, add a wrong answer to `cassettes/negatives.jsonl` with
   the graders that must fail.
4. Refresh the baseline: `uv run python -m evals.run --mode recorded --json evals/results/baseline-recorded.json`
   (`tests/test_evals.py` and CI compare it with the current run).
5. Case counts per suite must stay between 40 and 60.

## What the numbers mean (read this)

- **Recorded mode tests the graders, the case files and the pipeline. It does not test a model.**
  The cassettes were written to be correct, so 100 percent is expected; a drop means a grader, a
  case or a schema changed. That is the regression job in CI. pass^k in recorded mode is trivially
  the pass rate, because replays are deterministic.
- Current recorded baseline: capture 56/56, classify 51/51, review 45/45.
- **Fake mode** (the keyword-rule `FakeClaude` used for the demo) scores capture 16/56 (28.6 percent),
  classify 41/51 (80.4 percent), review 23/45 (51.1 percent). It shows the graders are not
  rubber stamps and how far rules get; it is not a model number either.
- **Live numbers: none recorded yet.** The live mode and the manual workflow
  (`.github/workflows/evals-live.yml`) exist and are covered by tests with a stub client, but no live
  run has been made, so there is no measured model pass rate or pass^3 to quote.
- The cases and the 20 judge labels were written by one author, not collected from real usage or
  from independent annotators. The cases are small and partly easy by design; a high live pass rate
  would not prove coverage of real dictation. The offline heuristic baseline in `evals.calibrate`
  agrees with the labels on 15 of 20 (kappa 0.48) and is below the trust threshold, as it should be.
- Deterministic graders check structure and grounding. They cannot tell whether a free-text
  sentence is well written; that is what the (uncalibrated against a live model so far) judge is for.
- Not covered: the other Claude purposes (meeting links, briefs, drafts, reports, school reading),
  and prompt-injection coverage beyond the 16 adversarial cases (see the injection tests and threat
  model for the prompt-side hardening).
