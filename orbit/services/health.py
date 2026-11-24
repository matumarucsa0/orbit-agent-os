"""Health: store the daily numbers a Shortcut posts (SPEC §2.36). Callers commit.

One row per local day. A post fills the fields it carries and keeps the others, so a Shortcut
that could not read sleep one morning does not erase what an earlier post stored. A field given
as null is "no data" and changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.health import HealthDay

FIELDS = (
    "sleep_minutes",
    "sleep_start",
    "sleep_end",
    "sleep_deep_minutes",
    "sleep_core_minutes",
    "sleep_rem_minutes",
    "sleep_awake_minutes",
    "sleep_in_bed_minutes",
    "resting_hr",
    "avg_hr",
    "min_hr",
    "max_hr",
    "steps",
    "active_kcal",
    "weight_grams",
)


@dataclass(frozen=True)
class DailyHealth:
    day: date
    sleep_minutes: int | None = None
    sleep_start: datetime | None = None
    sleep_end: datetime | None = None
    sleep_deep_minutes: int | None = None
    sleep_core_minutes: int | None = None
    sleep_rem_minutes: int | None = None
    sleep_awake_minutes: int | None = None
    sleep_in_bed_minutes: int | None = None
    resting_hr: int | None = None
    avg_hr: int | None = None
    min_hr: int | None = None
    max_hr: int | None = None
    steps: int | None = None
    active_kcal: int | None = None
    weight_grams: int | None = None


def upsert_day(session: Session, data: DailyHealth, now: datetime) -> tuple[HealthDay, bool]:
    """Returns the row and whether it is new."""
    row = session.scalars(select(HealthDay).where(HealthDay.day == data.day)).first()
    created = row is None
    if row is None:
        row = HealthDay(day=data.day)
        session.add(row)
    for name in FIELDS:
        value = getattr(data, name)
        if value is not None:
            setattr(row, name, value)
    row.received_at = now
    session.flush()
    return row, created


def list_days(session: Session, start: date, end: date) -> list[HealthDay]:
    """Days from `start` to `end`, both included, oldest first."""
    return list(
        session.scalars(
            select(HealthDay)
            .where(HealthDay.day >= start, HealthDay.day <= end)
            .order_by(HealthDay.day)
        )
    )
