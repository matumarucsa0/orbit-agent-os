---
name: add-prompt-with-evals
description: Add a new Claude prompt to Orbit, or change an existing one, with versioning, FakeClaude, worker queue, injection tests and eval cases. Use for any change to a *_SYSTEM prompt or a new structured Claude call.
---

# Add or change a prompt

Everything Claude-related is in `orbit/integrations/claude.py`; see the flow in `docs/CODEMAP.md` section 3.3.

## New prompt
1. In `claude.py`: output model (Pydantic), request dataclass, `Protocol`, `NAME_SYSTEM` (ends with `DATA_RULE`), `build_name_prompt`
   (outside text only via `untrusted(kind, text)`; identifiers masked with `mask_sensitive`; context only from a provider),
   `name_spec()` returning `JobSpec`, and a `ClaudeClient` method that calls `self._run(spec)`. Fast or deep model: add the purpose to
   `FAST_PURPOSES` / `DEEP_PURPOSES`.
2. `orbit/integrations/prompts_registry.py`: `PromptInfo("purpose", "NAME_SYSTEM", 1)`.
3. `orbit/services/claude_jobs.py`: a `KindSpec` in `KINDS` and a `JobClaudeClient` method (worker path); add the kind to `BACKGROUND_KINDS` in
   `scripts/claude_worker.py` if it is background work. The request dataclass needs an `owner` field; the system text uses the `{owner}` placeholders.
4. `orbit/integrations/fake_claude.py`: a rule-based `FakeClaude` method (offline demo and CI).
5. `uv run python -m orbit.cli prompts-snapshot` (or `make prompts-snapshot`), then `docs/PROMPTS.md`: table row + changelog line.
6. Result goes through a `ProposalHandler` if it can change data (never writes directly).
7. Tests: prompt builder test, `tests/test_prompt_injection.py` (several parallel structures: spec dict, `calls_for`, `runs`, FakeClaude call; hostile text stays inside the tags), `tests/test_fake_claude.py`,
   claude call path in `tests/test_claude.py`.

## Changing an existing prompt
Edit text, bump its number in `prompts_registry.py`, run `make prompts-snapshot`, add a `docs/PROMPTS.md` changelog line.
Run `make evals` before and after and put both results in the changelog line. `tests/test_prompt_versions.py` fails on a silent edit.

## Eval cases (suites exist only for capture, classify, review)
- Add cases to `evals/cases/<suite>.jsonl`: `id`, `suite`, `tags`, `input`, `expected`, and a hand-written `recorded` answer (a good answer, not one
  bent to the grader). Persona Alex Novak, invented people. Keep each suite at 40 to 60 cases, about a tenth adversarial.
- New grader: `evals/graders.py::GRADERS[suite]` plus a failing case in `evals/cassettes/negatives.jsonl`; update `13 + 6 + 11` in `tests/test_evals.py::test_grader_count`.
- New suite: `evals/loader.py` (`SUITES`, `OUTPUT_MODELS`, request builder), explicit branches in `evals/run.py` `fake_responder` and `live_responder` (they fall
  through to review), graders, cases, negatives, and an `elif` in `tests/test_evals.py::test_case_validates_against_the_real_schemas`.
- Regenerate `evals/results/baseline-recorded.json` with `uv run python -m evals.run --mode recorded --json evals/results/baseline-recorded.json`
  and update counts in `docs/EVALS.md`. Then `make evals` and `uv run pytest -q tests/test_evals.py tests/test_prompt_versions.py`.
Live evals cost money: do not run them without being asked (`run-evals` skill).
