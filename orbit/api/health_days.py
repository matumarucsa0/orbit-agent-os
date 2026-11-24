"""Daily health numbers from an iOS Shortcut (SPEC §2.36)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from orbit.deps import DbSession, SettingsDep
from orbit.domain.health import HealthDay
from orbit.services import health
from orbit.services.sleep_parse import SleepParseError, parse_night

router = APIRouter(tags=["health"])
LIST_DAYS = 31
MAX_DAYS = 400


class DailyHealthIn(BaseModel):
    """Every number is optional: the Shortcut sends what it could read. Numbers may arrive as
    text ("54") or with decimals; they are rounded. Unknown keys are ignored. No `day` (or an
    empty one) means today in the configured timezone: the shortcut runs in the morning, and the
    night that just ended belongs to today."""

    model_config = ConfigDict(extra="ignore")

    day: date | None = None

    @field_validator("day", "sleep_start", "sleep_end", mode="before")
    @classmethod
    def _blank_is_missing(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    # Raw sleep samples from the shortcut: four parallel columns, one line per sample (see
    # services/sleep_parse.py). When all four are sent, Orbit works out the night itself and the
    # numbers below are ignored.
    sleep_starts: str | None = None
    sleep_ends: str | None = None
    sleep_durations: str | None = None
    sleep_values: str | None = None
    sleep_minutes: float | None = Field(default=None, ge=0, le=24 * 60)
    sleep_start: datetime | None = None
    sleep_end: datetime | None = None
    resting_hr: float | None = Field(default=None, ge=20, le=250)
    avg_hr: float | None = Field(default=None, ge=20, le=250)
    min_hr: float | None = Field(default=None, ge=20, le=250)
    max_hr: float | None = Field(default=None, ge=20, le=250)
    steps: float | None = Field(default=None, ge=0, le=200_000)
    active_kcal: float | None = Field(default=None, ge=0, le=20_000)
    weight_kg: float | None = Field(default=None, ge=20, le=400)


class HealthDayOut(BaseModel):
    day: date
    sleep_minutes: int | None
    sleep_start: datetime | None
    sleep_end: datetime | None
    sleep_deep_minutes: int | None
    sleep_core_minutes: int | None
    sleep_rem_minutes: int | None
    sleep_awake_minutes: int | None
    sleep_in_bed_minutes: int | None
    resting_hr: int | None
    avg_hr: int | None
    min_hr: int | None
    max_hr: int | None
    steps: int | None
    active_kcal: int | None
    weight_kg: float | None
    received_at: datetime
    created: bool = False


def day_out(row: HealthDay, *, created: bool = False) -> HealthDayOut:
    return HealthDayOut(
        day=row.day,
        sleep_minutes=row.sleep_minutes,
        sleep_start=_utc(row.sleep_start),
        sleep_end=_utc(row.sleep_end),
        sleep_deep_minutes=row.sleep_deep_minutes,
        sleep_core_minutes=row.sleep_core_minutes,
        sleep_rem_minutes=row.sleep_rem_minutes,
        sleep_awake_minutes=row.sleep_awake_minutes,
        sleep_in_bed_minutes=row.sleep_in_bed_minutes,
        resting_hr=row.resting_hr,
        avg_hr=row.avg_hr,
        min_hr=row.min_hr,
        max_hr=row.max_hr,
        steps=row.steps,
        active_kcal=row.active_kcal,
        weight_kg=None if row.weight_grams is None else round(row.weight_grams / 1000, 1),
        received_at=row.received_at,
        created=created,
    )


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _round(value: float | None) -> int | None:
    return None if value is None else round(value)


@router.post("/health/daily", response_model=HealthDayOut)
def post_daily(body: DailyHealthIn, db: DbSession, settings: SettingsDep) -> HealthDayOut:
    """Store one day. Idempotent: the same day again fills the fields it carries and keeps the
    others. A time without an offset is read in the configured timezone."""
    now = datetime.now(UTC)
    today = now.astimezone(settings.timezone).date()
    day = body.day or today
    if day > today + timedelta(days=1):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That day is in the future.")

    def aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=settings.timezone) if value.tzinfo is None else value

    start, end = aware(body.sleep_start), aware(body.sleep_end)
    if start is not None and end is not None and end < start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Sleep ends before it starts.")
    columns = (body.sleep_starts, body.sleep_ends, body.sleep_durations, body.sleep_values)
    night = None
    if any(c is not None and c.strip() for c in columns):
        if not all(c is not None and c.strip() for c in columns):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Send all four sleep columns (starts, ends, durations, values) or none.",
            )
        try:
            night = parse_night(
                body.sleep_starts or "",
                body.sleep_ends or "",
                body.sleep_durations or "",
                body.sleep_values or "",
                day,
                settings.timezone,
            )
        except SleepParseError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    row, created = health.upsert_day(
        db,
        health.DailyHealth(
            day=day,
            sleep_minutes=night.asleep_minutes if night else _round(body.sleep_minutes),
            sleep_start=night.start if night else start,
            sleep_end=night.end if night else end,
            sleep_deep_minutes=night.deep_minutes if night else None,
            sleep_core_minutes=night.core_minutes if night else None,
            sleep_rem_minutes=night.rem_minutes if night else None,
            sleep_awake_minutes=night.awake_minutes if night else None,
            sleep_in_bed_minutes=night.in_bed_minutes if night else None,
            resting_hr=_round(body.resting_hr),
            avg_hr=_round(body.avg_hr),
            min_hr=_round(body.min_hr),
            max_hr=_round(body.max_hr),
            steps=_round(body.steps),
            active_kcal=_round(body.active_kcal),
            weight_grams=None if body.weight_kg is None else round(body.weight_kg * 1000),
        ),
        now,
    )
    db.commit()
    return day_out(row, created=created)


@router.get("/health/days", response_model=list[HealthDayOut])
def list_days(
    db: DbSession, settings: SettingsDep, start: date | None = None, end: date | None = None
) -> list[HealthDayOut]:
    """Days from `start` to `end` (both included, oldest first); default the last 31 days."""
    today = datetime.now(UTC).astimezone(settings.timezone).date()
    end = end or today
    start = start or end - timedelta(days=LIST_DAYS - 1)
    if end < start or (end - start).days >= MAX_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Ask for 1 to {MAX_DAYS} days.")
    return [day_out(row) for row in health.list_days(db, start, end)]
