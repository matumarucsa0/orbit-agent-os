---
name: add-migration
description: Add an Alembic migration for a model change in Orbit (new column, table, enum value, index) without breaking SQLite, Postgres or the drift test. Use whenever orbit/domain changes, or when the migration drift test fails.
---

# Add a migration

Rules: `migrations/AGENTS.md`. Never edit a committed revision (a hook blocks it). Work on the scratch database
(`DATABASE_URL` is already `sqlite:///./agent-scratch.db`).

1. Change the model in `orbit/domain/<module>.py` (types `Mapped[...]`, `UTCDateTime` for instants, `enum_column` for enums;
   enum values are `VARCHAR(20)` + CHECK, so keep names short).
2. `uv run alembic upgrade head` (scratch DB at the current head), then
   `uv run alembic revision --autogenerate -m "short title"`.
3. Rename the new file to the next number (`ls migrations/versions | tail -2`): `00NN_slug.py`, `revision = '00NN'`,
   `down_revision = '<previous>'`. Rewrite the generated body by hand:
   - every change to an existing table inside `with op.batch_alter_table('table') as batch_op:`
   - new NOT NULL column: `server_default=...` (existing rows)
   - new/changed enum: rebuild its CHECK in the batch; constraint names come from `NAMING_CONVENTION` (`orbit/domain/base.py`)
   - a real `downgrade()` that reverses it
   - drop anything unrelated that autogenerate added
4. `uv run alembic upgrade head` again, then `make migration-check` (upgrade, no drift via `command.check`, downgrade to base).
5. Update `docs/SPEC.md` section 3 (data model) and the service, API schema (`orbit/api/schemas.py`), tests; `make codemap`
   (the migrations table in `docs/CODEMAP.md` is generated); CHANGELOG line.
6. Delete nothing in `orbit/` you do not understand. If `alembic check` still reports a diff, the model and the migration disagree: fix the migration, not the test.

Postgres: CI runs the suite against it. Avoid SQLite-only SQL; do not rely on implicit type coercion.
Checklist for a new field on `Person` (also in `docs/CODEMAP.md` section 8): model (`orbit/domain/people.py`), `PersonUpdate`/`_PERSON_FIELDS` (`orbit/services/people.py`),
`PersonOut` (`orbit/api/schemas.py`), form + `templates/people/detail.html`, merge fields (`services/duplicates.py`), context
provider if agents should see it (`services/context/people.py`, masked), tests in `test_people_service.py` / `test_api_people.py`.
