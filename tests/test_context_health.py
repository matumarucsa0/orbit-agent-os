from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from orbit.services.context import ContextFocus, ContextWindow
from orbit.services.context.health import HealthProvider
from orbit.services.health import DailyHealth, upsert_day

PRAGUE = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
WINDOW = ContextWindow(date(2026, 10, 5), date(2026, 10, 11), PRAGUE)


def add(db: Session, day: date, **fields: int | None) -> None:
    upsert_day(db, DailyHealth(day=day, **fields), NOW)


def test_empty_database_is_an_empty_module(db: Session) -> None:
    result = HealthProvider().build(db, WINDOW)
    assert result.days == [] and result.last_data is None and result.latest_weight_kg is None
    assert result.last_7_days.sleep_minutes is None and result.last_7_days.sleep_nights == 0


def test_days_in_the_window_and_averages_over_what_exists(db: Session) -> None:
    for d, minutes in ((5, 480), (6, 300), (7, 420), (8, None)):
        add(db, date(2026, 10, d), sleep_minutes=minutes, resting_hr=50 + d, steps=1000 * d)
    result = HealthProvider().build(db, WINDOW)
    assert [d.day.day for d in result.days] == [5, 6, 7, 8]
    week = result.last_7_days
    assert (week.sleep_nights, week.short_nights) == (3, 1)  # 300 min is a short night
    assert week.sleep_minutes == 400  # mean of 480, 300, 420; the missing night is not a zero
    assert week.resting_hr == 56.5 and week.steps == 6500
    assert result.last_data == date(2026, 10, 8)


def test_the_heart_rate_range_is_in_the_days(db: Session) -> None:
    add(db, date(2026, 10, 6), resting_hr=52, min_hr=47, max_hr=171)
    day = HealthProvider().build(db, WINDOW).days[0]
    assert (day.resting_hr, day.min_hr, day.max_hr) == (52, 47, 171)


def test_sleep_stages_are_in_the_day(db: Session) -> None:
    upsert_day(
        db,
        DailyHealth(
            day=date(2026, 10, 6),
            sleep_minutes=497,
            sleep_deep_minutes=66,
            sleep_core_minutes=315,
            sleep_rem_minutes=116,
            sleep_awake_minutes=44,
            sleep_in_bed_minutes=540,
            sleep_start=datetime(2026, 10, 5, 21, 17, tzinfo=UTC),
            sleep_end=datetime(2026, 10, 6, 6, 18, tzinfo=UTC),
        ),
        NOW,
    )
    day = HealthProvider().build(db, WINDOW).days[0]
    assert (day.sleep_deep_minutes, day.sleep_core_minutes, day.sleep_rem_minutes) == (66, 315, 116)
    assert day.sleep_awake_minutes == 44 and day.sleep_in_bed_minutes == 540
    assert day.sleep_start is not None and day.sleep_start.hour == 23  # local time
    assert day.sleep_end is not None and day.sleep_end.hour == 8


def test_baseline_reaches_back_before_the_window(db: Session) -> None:
    add(db, date(2026, 9, 20), sleep_minutes=360, weight_grams=76000)
    add(db, date(2026, 10, 6), sleep_minutes=480, weight_grams=75000)
    result = HealthProvider().build(db, WINDOW)
    assert [d.day for d in result.days] == [date(2026, 10, 6)]
    assert result.last_28_days.sleep_minutes == 420 and result.last_28_days.sleep_nights == 2
    assert result.last_7_days.sleep_nights == 1
    assert result.latest_weight_kg == 75.0


def test_a_day_after_the_window_is_left_out(db: Session) -> None:
    add(db, date(2026, 10, 12), steps=500)
    result = HealthProvider().build(db, WINDOW)
    assert result.days == [] and result.last_7_days.steps is None
    assert result.last_data == date(2026, 10, 12)  # "latest data anywhere" ignores the window


def test_a_person_focus_has_no_health_data(db: Session) -> None:
    add(db, date(2026, 10, 6), steps=500)
    result = HealthProvider().build(db, WINDOW, ContextFocus(person_id=1))
    assert result.days == [] and result.last_data is None
