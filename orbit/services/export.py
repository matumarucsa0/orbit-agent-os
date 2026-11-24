"""A portable export of everything in the database, as one JSON document.

Every table, every row, column by column (so nothing is left out as the schema grows), with dates
and times as ISO 8601 strings (UTC) and decimals (school points) as strings. It holds personal
data (IBANs, notes, bank rows): keep it as private as the database itself.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from orbit.domain import Base
from orbit.domain.base import utcnow

FORMAT = "orbit-export"
VERSION = 1


def _value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):  # points (school): exact, as a string
        return str(value)
    return value


def _schema_revision(session: Session) -> str | None:
    if not inspect(session.connection()).has_table("alembic_version"):
        return None  # a database made without Alembic (tests)
    revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    return str(revision) if revision is not None else None


def export_data(session: Session, now: datetime | None = None) -> dict[str, Any]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in Base.metadata.sorted_tables:
        order = list(table.primary_key.columns) or list(table.columns)
        rows = session.execute(select(table).order_by(*order)).mappings()
        tables[table.name] = [{key: _value(value) for key, value in row.items()} for row in rows]
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": (now or utcnow()).isoformat(),
        "schema_revision": _schema_revision(session),
        "tables": tables,
    }


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)
