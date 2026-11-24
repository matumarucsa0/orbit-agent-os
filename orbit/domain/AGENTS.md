# orbit/domain: models, enums, money, normalisation

Purpose: SQLAlchemy 2.0 models (`Mapped[...]`), enums, `money.py`, `normalize.py`. Strict mypy. Every model module is imported in
`orbit/domain/__init__.py` so `Base.metadata` (Alembic, tests) is complete.

## Local invariants
- Instants use `UTCDateTime` (`base.py`): naive datetimes are rejected on write. Dates without a time (`date`) for birthdays and due days.
- Enums are `StrEnum` in `enums.py` stored through `enum_column(...)`: `VARCHAR(20)` + CHECK, lowercase values. A new value or a longer
  one needs a migration that rebuilds the CHECK (`migrations/AGENTS.md`).
- Money: `amount_minor: int` + `currency` (ISO 4217); parse and format only with `money.py`.
- `*_norm` columns (`value_norm`, `search_name_norm`, `counterparty_norm`) hold `normalize.py` output; update them wherever the source column
  changes, and match on them, never on the raw text.
- Constraint names come from `NAMING_CONVENTION` (`base.py`); give CheckConstraints a `name=`.
- Models link to shared entities by foreign key (people, goals, meetings), not by names in free text. No business logic here.
- Any model change needs a migration in the same change (`tests/test_migrations.py` fails on drift) and a SPEC section 3 update.

## Test it
`make migration-check`, then the service tests of the module (`docs/TESTMAP.md`).

## Common mistakes
- A model change without a migration. A free-text column where an enum or a foreign key belongs. A float for money.
- Forgetting to import a new model module in `__init__.py`.
