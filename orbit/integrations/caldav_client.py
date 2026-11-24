"""Read-only iCloud Calendar access over CalDAV (app-specific password), for the allow-listed
calendars only (`CALDAV_CALENDARS`).

The server is asked for the events overlapping a time window; recurring events are expanded here
(`parse_events`), so every occurrence becomes its own `CalendarEvent` with a stable key.
All-day events are left out: they aren't meetings.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import icalendar
import niquests
import recurring_ical_events
from caldav.davclient import DAVClient
from caldav.lib.error import AuthorizationError, DAVError

from orbit.domain.normalize import normalize_text

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


class CalDavError(Exception):
    pass


@dataclass(frozen=True)
class CalendarEvent:
    # The event UID; "<UID>#<original start, UTC>" for one occurrence of a recurring event.
    key: str
    calendar_name: str
    title: str
    location: str | None
    start: datetime  # UTC
    end: datetime  # UTC
    # Attendees and organizer, lowercased, without "mailto:".
    emails: tuple[str, ...] = ()
    cancelled: bool = False


@dataclass
class CalendarFetch:
    events: list[CalendarEvent] = field(default_factory=list)
    # The calendars that were read in full: only their meetings can be missing (= cancelled).
    calendars: list[str] = field(default_factory=list)
    # Allow-listed names the server doesn't have.
    missing: list[str] = field(default_factory=list)


class CalendarSource(Protocol):
    def fetch_events(self, start: datetime, end: datetime) -> CalendarFetch: ...


# --- Parsing ---


def _as_utc(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:  # a "floating" time: it's local
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)


def _emails(component: Any) -> tuple[str, ...]:
    found: list[str] = []
    for name in ("ORGANIZER", "ATTENDEE"):
        values = component.get(name)
        if values is None:
            continue
        for value in values if isinstance(values, list) else [values]:
            text = str(value).strip()
            if text.lower().startswith("mailto:"):
                text = text[len("mailto:") :]
            if "@" in text:
                found.append(text.casefold())
    return tuple(dict.fromkeys(found))


def _text(component: Any, name: str) -> str | None:
    value = component.get(name)
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def _recurring_uids(calendar: icalendar.Calendar) -> set[str]:
    uids = set()
    for component in calendar.walk("VEVENT"):
        if any(name in component for name in ("RRULE", "RDATE", "RECURRENCE-ID")):
            uids.add(str(component.get("UID", "")))
    return uids


def parse_events(
    ics: str | bytes, calendar_name: str, start: datetime, end: datetime, tz: ZoneInfo
) -> list[CalendarEvent]:
    """The timed events of one iCalendar object that overlap [start, end), recurrences expanded.
    Raises CalDavError on data that isn't iCalendar."""
    try:
        calendar = icalendar.Calendar.from_ical(ics)
        occurrences = recurring_ical_events.of(calendar).between(start, end)
    except ValueError as exc:
        raise CalDavError(f"Invalid calendar data: {exc}") from exc
    recurring = _recurring_uids(calendar)
    events = []
    for component in occurrences:
        if component.name != "VEVENT":
            continue
        uid = str(component.get("UID", "")).strip()
        dtstart = component.get("DTSTART")
        if not uid or dtstart is None or not isinstance(dtstart.dt, datetime):
            continue  # all-day (a date) or broken
        begin = _as_utc(dtstart.dt, tz)
        dtend = component.get("DTEND")
        if dtend is not None and isinstance(dtend.dt, datetime):
            finish = _as_utc(dtend.dt, tz)
        elif component.get("DURATION") is not None:
            finish = begin + component["DURATION"].dt
        else:
            finish = begin
        key = uid
        if uid in recurring:
            original = component.get("RECURRENCE-ID")
            moment = (
                _as_utc(original.dt, tz)
                if original is not None and isinstance(original.dt, datetime)
                else begin
            )
            key = f"{uid}#{moment:%Y%m%dT%H%M%SZ}"
        status = str(component.get("STATUS", "")).upper()
        events.append(
            CalendarEvent(
                key=key,
                calendar_name=calendar_name,
                title=_text(component, "SUMMARY") or "(no title)",
                location=_text(component, "LOCATION"),
                start=begin,
                end=max(finish, begin),
                emails=_emails(component),
                cancelled=status == "CANCELLED",
            )
        )
    return events


def select_calendars(
    available: Sequence[str], wanted: Sequence[str]
) -> tuple[list[str], list[str]]:
    """(available names that are allow-listed, allow-listed names not available); names compare
    case- and diacritics-insensitively."""
    by_norm = {normalize_text(name): name for name in available}
    chosen, missing = [], []
    for name in wanted:
        found = by_norm.get(normalize_text(name))
        if found is None:
            missing.append(name)
        elif found not in chosen:
            chosen.append(found)
    return chosen, missing


# --- iCloud ---


class CalDavCalendarSource:
    def __init__(
        self, url: str, username: str, password: str, calendars: Sequence[str], tz: ZoneInfo
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._calendars = list(calendars)
        self._tz = tz

    def fetch_events(self, start: datetime, end: datetime) -> CalendarFetch:
        if not self._calendars:
            raise CalDavError("No calendars selected: set CALDAV_CALENDARS.")
        fetch = CalendarFetch()
        try:
            with DAVClient(
                url=self._url,
                username=self._username,
                password=self._password,
                timeout=TIMEOUT_SECONDS,
            ) as client:
                # iCloud offers HTTP/3 and niquests takes it, but QUIC reads fail on some cloud
                # VMs (Google Cloud: "OSError 90, Message too long"). Stay on HTTP/1.1 and HTTP/2.
                client.session.close()
                client.session = niquests.Session(disable_http3=True)
                principal = client.principal()  # type: ignore[no-untyped-call]
                calendars = {
                    (cal.name or cal.get_display_name() or ""): cal for cal in principal.calendars()
                }
                chosen, fetch.missing = select_calendars(list(calendars), self._calendars)
                for name in chosen:
                    found = calendars[name].search(start=start, end=end, event=True, expand=False)
                    for item in found:
                        try:
                            fetch.events += parse_events(item.data, name, start, end, self._tz)
                        except CalDavError as exc:  # one broken event doesn't stop the sync
                            log.warning("skipped an event in %s: %s", name, exc)
                    fetch.calendars.append(name)
        except AuthorizationError as exc:
            raise CalDavError(
                "The calendar server rejected the username or app-specific password."
            ) from exc
        except (DAVError, OSError) as exc:  # OSError: network errors (niquests)
            raise CalDavError(f"Could not read the calendar: {exc.__class__.__name__}") from exc
        return fetch
