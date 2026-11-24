# migrations: Alembic

Purpose: schema history, `versions/NNNN_slug.py`, sequential four-digit revisions (`0001` onwards; the newest is the last file in the folder). `env.py` uses
`orbit.domain.Base.metadata`, `compare_type=True` and batch mode on SQLite. The models are the source of truth; the
drift test fails when they and the migrations disagree.

## Local invariants
- **Never edit a committed revision** (a hook blocks it): fix forward with a new one. Revisions are excluded from ruff.
- New revision: `uv run alembic revision --autogenerate -m "title"`, rename to the next number, set `revision = '00NN'` and
  `down_revision` to the previous one, review the generated code by hand (the `add-migration` skill has the checklist).
- Use `op.batch_alter_table` for any change to an existing table (SQLite). New NOT NULL columns need a `server_default`.
- Constraint and index names come from `NAMING_CONVENTION` in `orbit/domain/base.py`; do not name them differently.
- Enum columns are `VARCHAR(20)` + CHECK constraint: adding a value means rebuilding the CHECK in a batch operation.
- Write `downgrade()`; `tests/test_migrations.py` upgrades to head, runs `command.check` (no drift) and downgrades to base.
- Must work on SQLite and Postgres (CI runs both). No data in migrations beyond what the schema change needs.
- Data copy between databases is `orbit.cli copy-db`, not a migration.

## Test it
`make migration-check` (or `uv run pytest -q tests/test_migrations.py`). Try it on a scratch file database:
`DATABASE_URL=sqlite:///./agent-scratch.db uv run alembic upgrade head` (the default env of `.claude/settings.json`).

## Common mistakes
- Autogenerate output left as is (drops of unrelated things, missing batch mode, missing server defaults).
- A model change without a migration (drift test), or two revisions with the same `down_revision`.
- Editing `0001_baseline.py` or any earlier file "because it is only a test database".
