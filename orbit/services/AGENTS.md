# orbit/services: business logic

Purpose: everything that decides something. No FastAPI imports; strict mypy (`pyproject.toml`). Entry points per
area are in `docs/CODEMAP.md` section 2; the shared context layer is `services/context/`.

## Local invariants
- **Callers commit.** Functions `flush()` and return ORM objects; the route or job commits. Exceptions say so in their
  docstring (`capture_defer`, `claude_jobs`, `report_run`, `review`, `demo`). Never call `session.commit()` in a new service.
- Raise `NotFoundError` / `InvalidInputError` / `HandleConflictError` (`services/errors.py`); the API and web layers map them.
- Take `today` / `now` / `tz` as parameters instead of calling the clock inside (tests pass them); store UTC, convert with
  `settings.timezone` only at the edge. Money in minor units (`domain/money.py`), text through `domain/normalize.py`.
- Anything automation proposes is a `Proposal` through a registered `ProposalHandler` (`proposal_kinds.py`; one `register(...)`
  line in `inbox.py`); `apply` runs only inside `inbox.confirm`, in the caller's transaction, and re-validates the payload.
- Services that ask Claude take a Protocol (`CaptureExtractor`, `TransactionClassifier`, ...), or `None` for "no Claude";
  every such path needs a no-Claude fallback (capture saves a note draft, classification stays manual).
- Pushes: `notifications.record` with a `dedupe_key` (month, id or date inside the key) so a push is never sent twice.
- Context providers (`services/context/`): typed, bounded (hard caps), masked (`text()`), read-only, linked by ids; they
  are the only way prompt builders read module data. Never put IBANs, phones, emails or handle values in a provider model.
- Idempotent ingest: hash (bank), UID (calendar, vCard), upsert by source id; re-running must change nothing.

## Test it
Service tests use the `db` fixture (in-memory SQLite, or Postgres via `ORBIT_TEST_DATABASE_URL`) and fakes, not the app:
`make one T=tests/test_ledger.py`. Add a test per public function; fake Claude with a small class implementing the Protocol.

## Common mistakes
- A write that skips the Inbox (rule 1). A float amount. A naive `datetime.now()`. `==` on names instead of `normalize_text`.
- Importing `orbit.web` or `orbit.api` from a service (layering). Querying module tables inside a prompt builder.
- Adding a public function without a docstring and full type hints (mypy strict will complain; docstrings are expected).
