---
name: add-integration
description: Add an external service, file format or bank parser to Orbit (client behind a Protocol, settings, wiring in create_app, fixture-based tests, no network in tests). Use for a new data source, a new bank CSV, or a new outbound client.
---

# Add an integration

Rules: `orbit/integrations/AGENTS.md`. Do not guess formats: you need a real, anonymised sample (bank CSV, ICS, JSON) in
`tests/fixtures/<name>/`. If you only have a real file, stop and ask the owner to anonymise it; never commit it.

1. Spec first if it collects new data or sends something to Claude (`docs/SPEC.md` section, wait for the owner's OK).
2. Client or parser in `orbit/integrations/<name>.py`: a `Protocol` for what callers need, a small class implementing it, timeouts on every
   HTTP call (`httpx`), no secrets in logs or exceptions. Parsers are pure functions (bytes in, typed rows out).
3. Settings in `orbit/settings.py` (secrets `SecretStr`, default `None` = feature off; URLs validated like the school ones), a commented line in
   `.env.example`, `make codemap`.
4. Wire it in `orbit/app.py::create_app` (None when unconfigured; close it in the lifespan), expose it with a dependency in `orbit/deps.py`
   and/or `JobContext` in `orbit/jobs/scheduler.py` (a `JobSpec` with `available` giving the reason when it is off).
5. Ingest through a service (`orbit/services/`), idempotent (hash, UID, source id), writes that matter go to the Inbox.
6. Bank parser specifics: `orbit/integrations/bank_parsers/<bank>.py` with `HEADER` and `parse(rows, tz)`; register in `PARSERS` and in the `Bank` enum
   (`orbit/domain/enums.py`; needs a migration, see `add-migration`); also `Literal[...]` in `orbit/api/imports.py` and the name dict in
   `orbit/web/templates/bank/import.html`; HEADER must not be a subset of another bank's; tests in `tests/test_bank_parsers.py`, `test_bank_import.py`, `test_api_bank_import.py`, `test_intake.py`.
7. Tests: parser from the fixture; client with `httpx.MockTransport` or a hand-written fake; the autouse guard in `tests/conftest.py` fails real
   network use. Add the fixture name to a test (`scripts/select_tests.py` maps fixtures to tests by file name).
8. Docs: SPEC section + data model, ROADMAP tick, DECISIONS entry if a real choice, CHANGELOG. `make gates`.
