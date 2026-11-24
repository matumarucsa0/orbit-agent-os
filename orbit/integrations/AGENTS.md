# orbit/integrations: the outside world

Purpose: clients and parsers for everything that is not Orbit's own database: Claude (`claude.py`, `fake_claude.py`,
`pricing.py`, `prompts_registry.py`), bank CSV parsers (`bank_parsers/`), CalDAV/CardDAV, vCard, ntfy, ECB rates, name
days, school sources (`sis.py`, `recodex.py`, `owl.py`, `resources.py`), Liftoff.

## Local invariants
- `claude.py` is the only place that talks to Claude. It holds each prompt (`*_SYSTEM`), its output model, request
  dataclass, `Protocol`, `build_*_prompt`, `*_spec()` and the `ClaudeClient` method. `fake_claude.py::FakeClaude`
  mirrors every method with rules (offline, 0 tokens); a new Claude method needs a FakeClaude twin and a `KindSpec` in
  `services/claude_jobs.py`. Use the `add-prompt-with-evals` skill.
- Text from outside Orbit goes through `untrusted(kind, text)`; `DATA_RULE` ends every system prompt; `mask_sensitive`
  hides IBANs and phones. Prompt and answer text never goes on a span or in a log.
- The prompt text is versioned: `prompts_registry.py` + `tests/prompt_hashes.json` + `docs/PROMPTS.md`.
- Clients are small classes behind a Protocol, built in `orbit/app.py::create_app` (None when unconfigured), closed in
  the lifespan. HTTP via `httpx`; timeouts always set; school sources go through `PoliteClient` (rate limit, redacts the secrets).
- Base URLs for school and Liftoff must be `https://` (validators in `settings.py`); example hosts are `*.example.edu`.
- Bank parsers: one module per bank with a `HEADER` tuple and `parse(rows, tz)`, registered in `bank_parsers/__init__.py::PARSERS`
  and `domain/enums.py::Bank`; built from a real anonymised sample in `tests/fixtures/bank/`; amounts via `to_minor`,
  times via `local_to_utc`. Do not guess layouts. Also update `Literal` in `api/imports.py` and the name dict in `web/templates/bank/import.html`.
- Parsers are pure (bytes in, typed rows out); network and file reads stay outside them.

## Test it
`make one T=tests/test_bank_parsers.py`; Claude: `tests/test_claude.py` (fake SDK client), `test_fake_claude.py`,
`test_prompt_versions.py`, `test_prompt_injection.py`. HTTP clients: `httpx.MockTransport`. Real network fails the suite.

## Common mistakes
- A second code path that calls `anthropic` directly. Hardcoding a model name (they come from settings).
- Editing a prompt string without the version bump and snapshot (the prompt-version test fails by design).
- Wrapping outside text without `untrusted(...)`, or logging a response body that may contain a token.
