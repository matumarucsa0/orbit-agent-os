from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import niquests
import pytest
from caldav.lib.error import AuthorizationError

from orbit.integrations import caldav_client
from orbit.integrations.caldav_client import (
    CalDavCalendarSource,
    CalDavError,
    CalendarEvent,
    parse_events,
    select_calendars,
)

FIXTURE = Path(__file__).parent / "fixtures" / "calendar" / "icloud.ics"
PRAGUE = ZoneInfo("Europe/Prague")
START = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
END = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
UID = "6F1C2B8E-0A51-4C34-9E0B-1D2A3B4C5D0"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def events() -> dict[str, CalendarEvent]:
    found = parse_events(FIXTURE.read_bytes(), "Personal", START, END, PRAGUE)
    return {e.key: e for e in found}


def test_single_event_in_local_time_becomes_utc() -> None:
    coffee = events()[f"{UID}1"]
    assert coffee.title == "kafe s Paťom"
    assert coffee.location == "Kaviareň Pod Hradom, Bratislava"
    assert (coffee.start, coffee.end) == (utc(2026, 9, 25, 12), utc(2026, 9, 25, 13))  # CEST
    assert coffee.calendar_name == "Personal"
    assert coffee.emails == ()
    assert not coffee.cancelled


def test_attendees_and_organizer_emails_lowercased() -> None:
    lunch = events()[f"{UID}2"]
    assert lunch.emails == ("tereza.novakova@example.com", "me@example.com")


def test_recurring_event_expanded_per_occurrence_with_a_stable_key() -> None:
    found = events()
    moved = found[f"{UID}3#20260929T160000Z"]  # keyed by the original 18:00 start
    assert moved.title == "Gym s Petrom (neskôr)"
    assert moved.start == utc(2026, 9, 29, 18)  # moved to 20:00 local
    regular = found[f"{UID}3#20261006T160000Z"]
    assert (regular.title, regular.start, regular.end) == (
        "Gym s Petrom",
        utc(2026, 10, 6, 16),
        utc(2026, 10, 6, 17, 30),
    )
    assert f"{UID}3" not in found


def test_cancelled_all_day_floating_and_out_of_window() -> None:
    found = events()
    assert found[f"{UID}4"].cancelled
    assert f"{UID}5" not in found  # all-day: not a meeting
    call = found[f"{UID}6"]  # floating 09:00 = local time; DURATION instead of DTEND
    assert (call.start, call.end) == (utc(2026, 9, 30, 7), utc(2026, 9, 30, 7, 30))
    assert f"{UID}7" not in found  # 1 Nov: outside the window
    assert len(found) == 6


def test_parsing_is_repeatable() -> None:
    assert events() == events()


def test_invalid_data() -> None:
    with pytest.raises(CalDavError):
        parse_events("not a calendar", "Personal", START, END, PRAGUE)


def test_select_calendars_ignores_case_and_diacritics() -> None:
    chosen, missing = select_calendars(
        ["Personal", "Práca", "Family"], ["personal", "Praca", "Uni"]
    )
    assert (chosen, missing) == (["Personal", "Práca"], ["Uni"])


# --- The iCloud client, with the caldav library faked ---


class FakeItem:
    def __init__(self, data: str) -> None:
        self.data = data


class FakeCalendar:
    def __init__(self, name: str, data: list[str]) -> None:
        self.name = name
        self.data = data
        self.searches: list[dict[str, Any]] = []

    def get_display_name(self) -> str:
        return self.name

    def search(self, **kwargs: Any) -> list[FakeItem]:
        self.searches.append(kwargs)
        return [FakeItem(d) for d in self.data]


class FakeClient:
    """Stands in for caldav.DAVClient: principal().calendars() lists `found`."""

    found: ClassVar[list[FakeCalendar]] = []
    error: ClassVar[Exception | None] = None

    last: ClassVar[FakeClient | None] = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.session = niquests.Session()
        FakeClient.last = self

    def __enter__(self) -> FakeClient:
        if FakeClient.error is not None:
            raise FakeClient.error
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def principal(self) -> FakeClient:
        return self

    def calendars(self) -> list[FakeCalendar]:
        return FakeClient.found


@pytest.fixture
def fake_dav(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    monkeypatch.setattr(caldav_client, "DAVClient", FakeClient)
    monkeypatch.setattr(FakeClient, "found", [])
    monkeypatch.setattr(FakeClient, "error", None)
    return FakeClient


def source(*names: str) -> CalDavCalendarSource:
    return CalDavCalendarSource("https://caldav.example.com", "me", "pw", list(names), PRAGUE)


def test_fetches_only_allow_listed_calendars(fake_dav: type[FakeClient]) -> None:
    personal = FakeCalendar("Personal", [FIXTURE.read_text(encoding="utf-8"), "broken"])
    work = FakeCalendar("Work", [FIXTURE.read_text(encoding="utf-8")])
    fake_dav.found = [personal, work]
    fetch = source("personal", "Uni").fetch_events(START, END)
    assert fetch.calendars == ["Personal"]
    assert fetch.missing == ["Uni"]
    assert len(fetch.events) == 6  # the broken item is skipped, not fatal
    assert {e.calendar_name for e in fetch.events} == {"Personal"}
    assert personal.searches == [{"start": START, "end": END, "event": True, "expand": False}]
    assert work.searches == []


def test_http3_is_disabled(fake_dav: type[FakeClient]) -> None:
    # Over QUIC, reads from iCloud failed on the Google Cloud VM ("Message too long").
    source("Personal").fetch_events(START, END)
    assert fake_dav.last is not None
    assert fake_dav.last.session._disable_http3 is True


def test_errors_become_caldav_errors(fake_dav: type[FakeClient]) -> None:
    fake_dav.error = AuthorizationError("401")
    with pytest.raises(CalDavError, match="rejected the username"):
        source("Personal").fetch_events(START, END)
    fake_dav.error = ConnectionError("no route")
    with pytest.raises(CalDavError, match="Could not read the calendar"):
        source("Personal").fetch_events(START, END)


def test_no_calendars_selected() -> None:
    with pytest.raises(CalDavError, match="CALDAV_CALENDARS"):
        source().fetch_events(START, END)
