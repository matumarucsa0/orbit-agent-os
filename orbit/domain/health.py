"""Health: one row per local day from the Garmin watch through Apple Health (SPEC §2.36)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from orbit.domain.base import Base, TimestampMixin


class HealthDay(TimestampMixin, Base):
    """What a daily Shortcut posted for one day. A field the Shortcut could not read is None
    ("no data"), never 0. The sleep night belongs to the day it ends."""

    __tablename__ = "health_day"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(unique=True)
    sleep_minutes: Mapped[int | None]  # time asleep, not time in bed
    sleep_start: Mapped[datetime | None]
    sleep_end: Mapped[datetime | None]
    # Minutes per stage when the watch writes stages (Garmin does); awake: awake in the night.
    sleep_deep_minutes: Mapped[int | None]
    sleep_core_minutes: Mapped[int | None]
    sleep_rem_minutes: Mapped[int | None]
    sleep_awake_minutes: Mapped[int | None]
    sleep_in_bed_minutes: Mapped[int | None]
    resting_hr: Mapped[int | None]
    avg_hr: Mapped[int | None]
    min_hr: Mapped[int | None]  # lowest and highest heart rate of the period
    max_hr: Mapped[int | None]
    steps: Mapped[int | None]
    active_kcal: Mapped[int | None]
    weight_grams: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(20), default="apple_health")
    received_at: Mapped[datetime]
