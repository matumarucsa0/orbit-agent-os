"""Sleep samples from the Orbit Health shortcut -> one night (SPEC §2.36).

The shortcut cannot filter sleep by stage, so it sends four parallel columns, one line per
Health sleep sample, sorted by start, exactly as Shortcuts prints them. Built from a real
result (`tests/fixtures/health/sleep_probe.json`):

- times like `8 Oct 2026 at 23:17` (English month names; ISO 8601 is also accepted), local time
- durations like `51:00` (minutes:seconds) or `1:33:00` (hours:minutes:seconds)
- stages `Deep`, `Core`, `REM`, `Awake`, `In Bed` (and `Asleep` / `Unspecified` on watches
  that do not write stages)
- `In Bed` is one block over the whole night: kept apart, never added to the sleep time

The night that ends on `day` is the samples that start from 18:00 the evening before up to
18:00 on `day`. Nothing here guesses: a line that cannot be read is an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_TIME_RE = re.compile(
    r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})(?:\s+at|,)?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$"
)
_DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
NIGHT_STARTS_AT = time(18, 0)
ASLEEP = {"deep", "core", "rem", "asleep", "unspecified"}
STAGES = ASLEEP | {"awake", "in bed"}


class SleepParseError(ValueError):
    """The columns cannot be read as sleep; the message says which line."""


@dataclass(frozen=True)
class SleepNight:
    asleep_minutes: int
    deep_minutes: int
    core_minutes: int
    rem_minutes: int
    awake_minutes: int
    in_bed_minutes: int | None
    start: datetime  # UTC: first asleep sample (the In Bed block when there are no stages)
    end: datetime  # UTC: end of the last asleep sample


def parse_time(text: str, tz: ZoneInfo) -> datetime:
    """Local time as printed by Shortcuts, or ISO 8601 (with or without an offset) -> UTC."""
    text = text.strip()
    match = _TIME_RE.match(text)
    if match:
        day, month_name, year, hour, minute, second = match.groups()
        month = MONTHS.get(month_name[:3].lower())
        if month is None:
            raise SleepParseError(f"unknown month in {text!r}")
        try:
            local = datetime(
                int(year), month, int(day), int(hour), int(minute), int(second or 0), tzinfo=tz
            )
        except ValueError:
            raise SleepParseError(f"not a real time: {text!r}") from None
        return local.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise SleepParseError(f"unreadable time {text!r}") from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(UTC)


def parse_duration(text: str) -> int:
    """`51:00` -> 3060 seconds; `1:33:00` -> 5580."""
    match = _DURATION_RE.match(text.strip())
    if match is None:
        raise SleepParseError(f"unreadable duration {text!r}")
    hours, minutes, seconds = match.groups()
    if int(seconds) > 59 or (hours is not None and int(minutes) > 59):
        raise SleepParseError(f"not a real duration: {text!r}")
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_night(
    starts: str, ends: str, durations: str, values: str, day: date, tz: ZoneInfo
) -> SleepNight | None:
    """The night that ends on `day`, or None when no asleep sample falls in it."""
    columns = [_lines(starts), _lines(ends), _lines(durations), _lines(values)]
    if len({len(column) for column in columns}) != 1:
        counts = ", ".join(str(len(column)) for column in columns)
        raise SleepParseError(
            f"the four sleep columns differ in length ({counts}); they must be one line per sample"
        )
    window_start = datetime.combine(day - timedelta(days=1), NIGHT_STARTS_AT, tz).astimezone(UTC)
    window_end = datetime.combine(day, NIGHT_STARTS_AT, tz).astimezone(UTC)

    seconds = {name: 0 for name in ("deep", "core", "rem", "asleep", "awake", "in bed")}
    first: datetime | None = None
    last: datetime | None = None
    bed_start: datetime | None = None
    bed_end: datetime | None = None
    for number, (start_text, end_text, duration_text, value_text) in enumerate(
        zip(*columns, strict=True), 1
    ):
        stage = value_text.lower()
        if stage not in STAGES:
            raise SleepParseError(f"line {number}: unknown sleep stage {value_text!r}")
        try:
            start = parse_time(start_text, tz)
            end = parse_time(end_text, tz)
            length = parse_duration(duration_text)
        except SleepParseError as exc:
            raise SleepParseError(f"line {number}: {exc}") from None
        if end < start:
            raise SleepParseError(f"line {number}: ends before it starts")
        if not window_start <= start < window_end:
            continue
        if stage == "in bed":
            seconds["in bed"] += length
            bed_start = start if bed_start is None else min(bed_start, start)
            bed_end = end if bed_end is None else max(bed_end, end)
            continue
        key = "asleep" if stage in ("asleep", "unspecified") else stage
        seconds[key] += length
        if stage in ASLEEP:
            first = start if first is None else min(first, start)
            last = end if last is None else max(last, end)

    asleep = seconds["deep"] + seconds["core"] + seconds["rem"] + seconds["asleep"]
    if asleep == 0:
        return None
    night_start = first or bed_start
    night_end = last or bed_end
    assert night_start is not None and night_end is not None

    def minutes(value: int) -> int:
        return round(value / 60)

    return SleepNight(
        asleep_minutes=minutes(asleep),
        deep_minutes=minutes(seconds["deep"]),
        core_minutes=minutes(seconds["core"]),
        rem_minutes=minutes(seconds["rem"]),
        awake_minutes=minutes(seconds["awake"]),
        in_bed_minutes=minutes(seconds["in bed"]) if bed_start is not None else None,
        start=night_start,
        end=night_end,
    )
