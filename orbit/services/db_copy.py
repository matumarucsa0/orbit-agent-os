"""Copy all data between two Orbit databases, e.g. the dev SQLite file into production Postgres.

Both databases must be migrated to the same Alembic revision, and the target must be empty.
Rows are copied table by table in foreign-key order, in one transaction on the target.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Connection, Engine, Table, func, inspect, select, text, update

from orbit.domain import Base
from orbit.services.errors import InvalidInputError

BATCH_SIZE = 500


def schema_revision(connection: Connection) -> str | None:
    if not inspect(connection).has_table("alembic_version"):
        return None
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _batches(connection: Connection, table: Table) -> Iterator[list[dict[str, Any]]]:
    # Primary-key order, so rows that point at earlier rows of their own table come after them.
    result = connection.execute(select(table).order_by(*table.primary_key.columns))
    while rows := result.fetchmany(BATCH_SIZE):
        yield [dict(row._mapping) for row in rows]


def _self_references(table: Table) -> list[str]:
    return [fk.parent.name for fk in table.foreign_keys if fk.column.table is table]


def _reset_sequences(connection: Connection) -> None:
    """Postgres: move each id sequence past the copied ids, so new rows don't collide."""
    for table in Base.metadata.sorted_tables:
        pk = list(table.primary_key.columns)
        if len(pk) != 1 or not pk[0].autoincrement or pk[0].type.python_type is not int:
            continue
        connection.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence(:table, :column), "
                f'COALESCE(MAX("{pk[0].name}"), 1), MAX("{pk[0].name}") IS NOT NULL) '
                f'FROM "{table.name}"'
            ),
            {"table": table.name, "column": pk[0].name},
        )


def copy_database(source: Engine, target: Engine) -> dict[str, int]:
    """Copy every row of every Orbit table from `source` into the empty `target`.

    Returns the number of copied rows per table. Raises InvalidInputError, and changes nothing,
    if the schemas are at different revisions or the target already holds data.
    """
    with source.connect() as src, target.begin() as dst:
        source_rev, target_rev = schema_revision(src), schema_revision(dst)
        if source_rev is None:
            raise InvalidInputError("The source database has no schema (alembic_version).")
        if target_rev is None:
            raise InvalidInputError(
                "The target database has no schema; run `alembic upgrade head` on it first."
            )
        if source_rev != target_rev:
            raise InvalidInputError(
                f"Schema revisions differ (source {source_rev}, target {target_rev}); "
                "upgrade both with `alembic upgrade head` first."
            )
        tables = Base.metadata.sorted_tables
        for table in tables:
            if dst.execute(select(func.count()).select_from(table)).scalar_one():
                raise InvalidInputError(
                    f"The target database isn't empty (table {table.name} has rows)."
                )

        counts: dict[str, int] = {}
        for table in tables:
            self_refs = _self_references(table)
            deferred: list[dict[str, Any]] = []
            counts[table.name] = 0
            for rows in _batches(src, table):
                if self_refs:
                    # A row may point at a later row of the same table: insert the links as
                    # NULL first and set them once every row exists.
                    pk = [c.name for c in table.primary_key.columns]
                    for row in rows:
                        links = {name: row[name] for name in self_refs}
                        if any(value is not None for value in links.values()):
                            deferred.append({**{f"pk_{k}": row[k] for k in pk}, **links})
                            row.update(dict.fromkeys(self_refs))
                dst.execute(table.insert(), rows)
                counts[table.name] += len(rows)
            for links in deferred:
                where = [c == links[f"pk_{c.name}"] for c in table.primary_key.columns]
                dst.execute(
                    update(table).where(*where).values({name: links[name] for name in self_refs})
                )
        if dst.dialect.name == "postgresql":
            _reset_sequences(dst)
    return counts
