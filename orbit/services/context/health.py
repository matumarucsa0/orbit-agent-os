"""Health: sleep, heart rate, steps and weight from the watch, for the window and the weeks
before it.

Read-only. Missing data stays missing (None), never zero. Weight here is from Apple Health; the
`fitness` module carries the bodyweight Liftoff noted on a workout, used only when this one has
none (owner decision, 2026-10-09). With a person focus the module is empty.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.health import HealthDay
from orbit.services.context.base import ContextFocus, ContextWindow, ModuleContext

SHORT_NIGHT_MINUTES = 6 * 60
RECENT_DAYS = 7
BASELINE_DAYS = 28
MAX_DAYS = 120


class HealthDayContext(BaseModel):
    day: date
    sleep_minutes: int | None  # time asleep (deep + core + REM)
    sleep_deep_minutes: int | None
    sleep_core_minutes: int | None
    sleep_rem_minutes: int | None
    sleep_awake_minutes: int | None  # awake during the night
    sleep_in_bed_minutes: int | None
    sleep_start: datetime | None  # local time
    sleep_end: datetime | None
    resting_hr: int | None
    avg_hr: int | None
    min_hr: int | None
    max_hr: int | None
    steps: int | None
    active_kcal: int | None
    weight_kg: float | None


class HealthAverages(BaseModel):
    """Means over the days that have the value; `n` says how many days that was."""

    days: int  # length of the period
    sleep_minutes: int | None = None
    sleep_nights: int = 0
    short_nights: int = 0  # under 6 hours
    resting_hr: float | None = None
    steps: int | None = None
    weight_kg: float | None = None


class HealthContext(ModuleContext):
    trimmable: ClassVar[tuple[str, ...]] = ("days",)
    module: Literal["health"] = "health"
    days: list[HealthDayContext] = Field(default_factory=list)  # in the window, oldest first
    last_7_days: HealthAverages = HealthAverages(days=RECENT_DAYS)
    last_28_days: HealthAverages = HealthAverages(days=BASELINE_DAYS)
    last_data: date | None = None  # newest day with any data, whatever the window
    latest_weight_kg: float | None = None


def _mean(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def averages(rows: list[HealthDay], days: int) -> HealthAverages:
    sleep = [r.sleep_minutes for r in rows if r.sleep_minutes is not None]
    rhr = [r.resting_hr for r in rows if r.resting_hr is not None]
    steps = [r.steps for r in rows if r.steps is not None]
    weight = [r.weight_grams for r in rows if r.weight_grams is not None]
    mean_sleep, mean_steps, mean_weight = _mean(sleep), _mean(steps), _mean(weight)
    return HealthAverages(
        days=days,
        sleep_minutes=None if mean_sleep is None else round(mean_sleep),
        sleep_nights=len(sleep),
        short_nights=sum(1 for m in sleep if m < SHORT_NIGHT_MINUTES),
        resting_hr=_mean(rhr),
        steps=None if mean_steps is None else round(mean_steps),
        weight_kg=None if mean_weight is None else round(mean_weight / 1000, 1),
    )


class HealthProvider:
    name = "health"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> HealthContext:
        if focus is not None and focus.person_id is not None:
            return HealthContext()
        first = max(window.start, window.end - timedelta(days=MAX_DAYS - 1))
        in_window = list(
            session.scalars(
                select(HealthDay)
                .where(HealthDay.day >= first, HealthDay.day <= window.end)
                .order_by(HealthDay.day)
            )
        )
        base_start = window.end - timedelta(days=BASELINE_DAYS - 1)
        baseline = list(
            session.scalars(
                select(HealthDay).where(HealthDay.day >= base_start, HealthDay.day <= window.end)
            )
        )
        recent_start = window.end - timedelta(days=RECENT_DAYS - 1)
        newest = session.scalars(select(HealthDay).order_by(HealthDay.day.desc()).limit(1)).first()
        weighed = session.scalars(
            select(HealthDay)
            .where(HealthDay.weight_grams.is_not(None), HealthDay.day <= window.end)
            .order_by(HealthDay.day.desc())
            .limit(1)
        ).first()
        return HealthContext(
            truncated=window.start < first,
            days=[
                HealthDayContext(
                    day=r.day,
                    sleep_minutes=r.sleep_minutes,
                    sleep_deep_minutes=r.sleep_deep_minutes,
                    sleep_core_minutes=r.sleep_core_minutes,
                    sleep_rem_minutes=r.sleep_rem_minutes,
                    sleep_awake_minutes=r.sleep_awake_minutes,
                    sleep_in_bed_minutes=r.sleep_in_bed_minutes,
                    sleep_start=r.sleep_start.astimezone(window.tz) if r.sleep_start else None,
                    sleep_end=r.sleep_end.astimezone(window.tz) if r.sleep_end else None,
                    resting_hr=r.resting_hr,
                    avg_hr=r.avg_hr,
                    min_hr=r.min_hr,
                    max_hr=r.max_hr,
                    steps=r.steps,
                    active_kcal=r.active_kcal,
                    weight_kg=None if r.weight_grams is None else round(r.weight_grams / 1000, 1),
                )
                for r in in_window
            ],
            last_7_days=averages([r for r in baseline if r.day >= recent_start], RECENT_DAYS),
            last_28_days=averages(baseline, BASELINE_DAYS),
            last_data=newest.day if newest else None,
            latest_weight_kg=None
            if weighed is None or weighed.weight_grams is None
            else round(weighed.weight_grams / 1000, 1),
        )
