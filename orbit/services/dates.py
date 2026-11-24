"""Birthdays and name days: resolving a person's dates and the upcoming window."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.people import Person
from orbit.integrations import namedays

Kind = Literal["birthday", "nameday"]


@dataclass(frozen=True)
class UpcomingDate:
    date: date
    kind: Kind
    person: Person
    detail: str  # "turns 32" / "" for birthdays; the celebrated name for name days


def occurrence(month: int, day: int, year: int) -> date:
    """The date a yearly (month, day) falls on in `year`; 29 Feb -> 28 Feb in non-leap years."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def nameday_name(person: Person) -> str | None:
    """The name whose name day counts: the assigned calendar name, else first_name, else the
    first word of display_name."""
    if person.nameday_name:
        return person.nameday_name
    if person.first_name and person.first_name.strip():
        return person.first_name.strip().split()[0]
    words = person.display_name.split()
    return words[0] if words else None


def person_namedays(person: Person) -> list[tuple[int, int]]:
    name = nameday_name(person)
    if not name:
        return []
    return namedays.dates_for(name, person.nameday_country)


def _age_detail(person: Person, on: date) -> str:
    if person.birthday_year is None:
        return ""
    return f"turns {on.year - person.birthday_year}"


def person_upcoming(person: Person, today: date, days: int) -> list[UpcomingDate]:
    """One person's birthday and name days from `today` to `today + days` (inclusive)."""
    end = today + timedelta(days=days)
    years = range(today.year, end.year + 1)
    found: list[UpcomingDate] = []
    if person.birthday_month is not None and person.birthday_day is not None:
        for year in years:
            when = occurrence(person.birthday_month, person.birthday_day, year)
            if today <= when <= end:
                found.append(UpcomingDate(when, "birthday", person, _age_detail(person, when)))
    name = nameday_name(person)
    for month, day in person_namedays(person):
        for year in years:
            when = occurrence(month, day, year)
            if today <= when <= end:
                found.append(UpcomingDate(when, "nameday", person, name or ""))
    found.sort(key=lambda u: (u.date, u.kind))
    return found


def upcoming(session: Session, today: date, days: int = 7) -> list[UpcomingDate]:
    """Birthdays and name days from `today` to `today + days` (inclusive), by date then name."""
    people = session.scalars(select(Person).where(Person.archived.is_(False)))
    found = [u for person in people for u in person_upcoming(person, today, days)]
    found.sort(key=lambda u: (u.date, u.kind, u.person.search_name_norm, u.person.id))
    return found
