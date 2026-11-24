import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbit.services.sleep_parse import (
    SleepParseError,
    parse_duration,
    parse_night,
    parse_time,
)

PRAGUE = ZoneInfo("Europe/Prague")
SAMPLE = json.loads(
    (Path(__file__).parent / "fixtures" / "health" / "sleep_probe.json").read_text()
)


def night(day: date, **columns: str):
    data = {**SAMPLE, **columns}
    return parse_night(
        data["sleep_starts"],
        data["sleep_ends"],
        data["sleep_durations"],
        data["sleep_values"],
        day,
        PRAGUE,
    )


def test_durations() -> None:
    assert parse_duration("51:00") == 3060
    assert parse_duration("1:33:00") == 5580
    assert parse_duration("9:00:00") == 32400
    assert parse_duration(" 0:59 ") == 59
    for bad in ("", "abc", "12", "1:75:00", "5:90"):
        with pytest.raises(SleepParseError):
            parse_duration(bad)


def test_times() -> None:
    assert parse_time("8 Oct 2026 at 23:17", PRAGUE) == datetime(2026, 10, 8, 21, 17, tzinfo=UTC)
    assert parse_time("9 Oct 2026 at 08:18", PRAGUE) == datetime(2026, 10, 9, 6, 18, tzinfo=UTC)
    assert parse_time("1 Jan 2026 at 00:05", PRAGUE) == datetime(2025, 12, 31, 23, 5, tzinfo=UTC)
    assert parse_time("2026-10-08T23:17:00+02:00", PRAGUE) == datetime(
        2026, 10, 8, 21, 17, tzinfo=UTC
    )
    assert parse_time("2026-10-08T23:17:00", PRAGUE) == datetime(2026, 10, 8, 21, 17, tzinfo=UTC)
    assert parse_time("8 October 2026 at 23:17", PRAGUE) == datetime(
        2026, 10, 8, 21, 17, tzinfo=UTC
    )
    for bad in ("tomorrow", "8 Foo 2026 at 23:17", "31 Feb 2026 at 10:00", ""):
        with pytest.raises(SleepParseError):
            parse_time(bad, PRAGUE)


def test_the_sample_night_that_ended_on_9_october() -> None:
    result = night(date(2026, 10, 9))
    assert result is not None
    assert (result.deep_minutes, result.core_minutes, result.rem_minutes) == (62, 288, 93)
    assert result.asleep_minutes == 443  # 7 h 23 min; the In Bed block is not added
    assert result.awake_minutes == 21
    assert result.in_bed_minutes == 540
    assert result.start == datetime(2026, 10, 8, 21, 5, tzinfo=UTC)  # 23:05 local
    assert result.end == datetime(2026, 10, 9, 4, 49, tzinfo=UTC)  # 06:49 local


def test_the_sample_night_that_ended_on_8_october_has_no_in_bed_block() -> None:
    result = night(date(2026, 10, 8))
    assert result is not None
    assert result.asleep_minutes == 408
    assert result.in_bed_minutes is None
    assert result.start == datetime(2026, 10, 7, 22, 20, tzinfo=UTC)  # 00:20 local


def test_a_day_with_no_night_is_none() -> None:
    assert night(date(2026, 10, 10)) is None
    assert night(date(2026, 10, 1)) is None


def test_a_watch_without_stages_counts_asleep_samples() -> None:
    result = parse_night(
        "8 Oct 2026 at 23:00\n9 Oct 2026 at 03:00",
        "9 Oct 2026 at 03:00\n9 Oct 2026 at 07:00",
        "4:00:00\n4:00:00",
        "Asleep\nAwake",
        date(2026, 10, 9),
        PRAGUE,
    )
    assert result is not None
    assert (result.asleep_minutes, result.awake_minutes, result.deep_minutes) == (240, 240, 0)


def test_columns_of_different_length_are_refused() -> None:
    with pytest.raises(SleepParseError, match="differ in length"):
        night(date(2026, 10, 9), sleep_values="Deep\nCore")


def test_an_unreadable_line_names_the_line() -> None:
    values = SAMPLE["sleep_values"].split("\n")
    values[3] = "Dreaming"
    with pytest.raises(SleepParseError, match="line 4: unknown sleep stage"):
        night(date(2026, 10, 9), sleep_values="\n".join(values))
    durations = SAMPLE["sleep_durations"].split("\n")
    durations[5] = "soon"
    with pytest.raises(SleepParseError, match="line 6"):
        night(date(2026, 10, 9), sleep_durations="\n".join(durations))


def test_blank_lines_are_ignored() -> None:
    padded = {k: "\n" + v.replace("\n", "\n\n") + "\n" for k, v in SAMPLE.items() if k != "note"}
    result = parse_night(
        padded["sleep_starts"],
        padded["sleep_ends"],
        padded["sleep_durations"],
        padded["sleep_values"],
        date(2026, 10, 9),
        PRAGUE,
    )
    assert result is not None and result.asleep_minutes == 443
