"""iCalendar (RFC 5545) feed for an Apple Calendar subscription: birthdays and name days
(yearly), open promise due dates and lent-item reminders (one-off), and appointments made
by capture (timed).

Output is deterministic: UIDs depend only on the record (and the name-day date), start dates on
fixed years and DTSTAMP on the record's updated_at, so re-fetching an unchanged calendar yields
identical bytes, and editing a record updates its event instead of duplicating it. A settled item
or closed promise simply drops out of the feed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection, PromiseStatus
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.people import Person
from orbit.services.dates import nameday_name, occurrence, person_namedays
from orbit.services.ledger import item_reminder_on
from orbit.services.meetings import ORBIT_CALENDAR

PRODID = "-//Orbit//Orbit personal CRM//EN"
UID_DOMAIN = "orbit"
DEFAULT_START_YEAR = 2000  # a leap year, so a 29 Feb start date exists
MAX_LINE_OCTETS = 75


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 §3.1) without splitting a UTF-8 character."""
    parts: list[str] = []
    current, size = "", 0
    limit = MAX_LINE_OCTETS
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append(current)
            current, size = "", 0
            limit = MAX_LINE_OCTETS - 1  # continuation lines start with a space
        current += char
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _yearly_rule(month: int, day: int) -> str:
    if month == 2 and day == 29:
        # Last day of February: the 29th in leap years, the 28th otherwise.
        return "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1"
    return "FREQ=YEARLY"


def _event(
    uid: str,
    start: date,
    summary: str,
    description: str,
    modified: datetime,
    rrule: str | None = None,
) -> list[str]:
    """An all-day event, yearly when `rrule` is given."""
    stamp = _stamp(modified)
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"LAST-MODIFIED:{stamp}",
        f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
        f"DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}",
        *([f"RRULE:{rrule}"] if rrule else []),
        f"SUMMARY:{escape_text(summary)}",
        f"DESCRIPTION:{escape_text(description)}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


def _birthday(person: Person) -> list[str]:
    if person.birthday_month is None or person.birthday_day is None:
        return []
    month, day = person.birthday_month, person.birthday_day
    year = person.birthday_year or DEFAULT_START_YEAR
    return _event(
        f"birthday-{person.id}@{UID_DOMAIN}",
        occurrence(month, day, year),
        f"🎂 {person.display_name}",
        f"Born {person.birthday_year}" if person.birthday_year else "Birthday",
        person.updated_at,
        _yearly_rule(month, day),
    )


def _namedays(person: Person) -> list[str]:
    lines: list[str] = []
    name = nameday_name(person) or ""
    for month, day in person_namedays(person):
        lines += _event(
            f"nameday-{person.id}-{month:02d}{day:02d}@{UID_DOMAIN}",
            occurrence(month, day, DEFAULT_START_YEAR),
            f"🎉 {person.display_name} (name day)",
            f"{name} · {person.nameday_country} name day",
            person.updated_at,
            _yearly_rule(month, day),
        )
    return lines


def _day(value: date) -> str:
    return f"{value.day} {value:%b %Y}"


def _promise(promise: Promise) -> list[str]:
    if promise.due is None:
        return []
    name = promise.person.display_name
    if promise.direction is PromiseDirection.I_PROMISED:
        summary = f"✅ Promised {name}: {promise.text}"
    else:
        summary = f"✅ {name} promised: {promise.text}"
    return _event(
        f"promise-{promise.id}@{UID_DOMAIN}",
        promise.due,
        summary,
        f"Promise made {_day(promise.created_at.date())}",
        promise.updated_at,
    )


def _appointment(meeting: Meeting) -> list[str]:
    """A timed event for an appointment Orbit made (SPEC §2.23)."""
    stamp = _stamp(meeting.updated_at)
    who = ", ".join(p.display_name for p in meeting.people)
    return [
        "BEGIN:VEVENT",
        f"UID:appointment-{meeting.id}@{UID_DOMAIN}",
        f"DTSTAMP:{stamp}",
        f"LAST-MODIFIED:{stamp}",
        f"DTSTART:{_stamp(meeting.start_utc)}",
        f"DTEND:{_stamp(meeting.end_utc)}",
        f"SUMMARY:{escape_text('🗓 ' + meeting.title)}",
        *([f"LOCATION:{escape_text(meeting.location)}"] if meeting.location else []),
        f"DESCRIPTION:{escape_text('With ' + who if who else 'Appointment')}",
        "END:VEVENT",
    ]


def _lent_item(debt: Debt, reminder_days: int, tz: ZoneInfo) -> list[str]:
    remind_on = item_reminder_on(debt, reminder_days, tz)
    if remind_on is None:
        return []
    return _event(
        f"item-{debt.id}@{UID_DOMAIN}",
        remind_on,
        f"📦 {debt.person.display_name} has your {debt.description}",
        f"Lent {_day(debt.created_at.astimezone(tz).date())}",
        debt.updated_at,
    )


def build_calendar(session: Session, item_reminder_days: int, tz: ZoneInfo) -> str:
    people = session.scalars(select(Person).where(Person.archived.is_(False)).order_by(Person.id))
    promises = session.scalars(
        select(Promise)
        .where(Promise.status == PromiseStatus.OPEN, Promise.due.is_not(None))
        .options(joinedload(Promise.person))
        .order_by(Promise.id)
    )
    items = session.scalars(
        select(Debt)
        .where(
            Debt.type == DebtType.ITEM,
            Debt.direction == DebtDirection.THEY_OWE_ME,
            Debt.settled_at.is_(None),
        )
        .options(joinedload(Debt.person))
        .order_by(Debt.id)
    )
    appointments = session.scalars(
        select(Meeting)
        .where(Meeting.calendar_name == ORBIT_CALENDAR, Meeting.cancelled.is_(False))
        .options(selectinload(Meeting.links).selectinload(MeetingPerson.person))
        .order_by(Meeting.id)
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Orbit",
        "X-WR-CALDESC:Birthdays, name days, promises, lent items and appointments",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for person in people:
        lines += _birthday(person)
        lines += _namedays(person)
    for promise in promises:
        lines += _promise(promise)
    for debt in items:
        lines += _lent_item(debt, item_reminder_days, tz)
    for meeting in appointments:
        lines += _appointment(meeting)
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)
