from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from orbit.domain.people import Person
from orbit.integrations import namedays
from orbit.services import people
from orbit.services.dates import nameday_name, occurrence, person_namedays, upcoming


def make(db: Session, name: str, **fields: object) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name, **fields))


# --- Resolver ---


@pytest.mark.parametrize(
    ("name", "country", "expected"),
    [
        ("Tobiáš", "SK", [(9, 13)]),
        ("Tobias", "SK", [(9, 13)]),  # diacritics-insensitive
        ("TOBIÁŠ", "SK", [(9, 13)]),
        ("Tobiáš", "CZ", [(11, 2)]),
        ("Petr", "CZ", [(2, 22), (6, 29)]),  # a name on several days: all of them
        # The SK calendar lists accent variants on the same day (Šimon, Simon): one date.
        ("Šimon", "SK", [(10, 30)]),
        ("Jozef", "CZ", []),  # Slovak spelling isn't in the Czech calendar
        ("Kevin", "SK", [(6, 3)]),
        ("Nobody", "SK", []),
        ("Tobiáš", None, []),
        ("Tobiáš", "PL", []),
        ("  ", "SK", []),
    ],
)
def test_dates_for(name: str, country: str | None, expected: list[tuple[int, int]]) -> None:
    assert namedays.dates_for(name, country) == expected


def test_names_on() -> None:
    assert namedays.names_on(12, 24, "SK") == ["Adam", "Eva"]
    assert namedays.names_on(1, 1, "CZ") == []


# --- Person helpers ---


def test_nameday_name_prefers_first_name(db: Session) -> None:
    assert nameday_name(make(db, "Paťo", first_name="Patrik")) == "Patrik"
    assert nameday_name(make(db, "Tobiáš Novák")) == "Tobiáš"
    assert nameday_name(make(db, "someone")) == "someone"


def test_person_namedays(db: Session) -> None:
    assert person_namedays(make(db, "Paťo", first_name="Patrik")) == [(7, 6)]  # SK: 6 Jul
    assert person_namedays(make(db, "Petr Novák", nameday_country="CZ")) == [(2, 22), (6, 29)]
    assert person_namedays(make(db, "Tobiáš", nameday_country=None)) == []
    assert person_namedays(make(db, "Autoservis Kováč")) == []


@pytest.mark.parametrize(
    ("month", "day", "year", "expected"),
    [
        (2, 29, 2028, date(2028, 2, 29)),
        (2, 29, 2027, date(2027, 2, 28)),
        (12, 31, 2026, date(2026, 12, 31)),
    ],
)
def test_occurrence(month: int, day: int, year: int, expected: date) -> None:
    assert occurrence(month, day, year) == expected


# --- Upcoming window ---


def summary(db: Session, today: date, days: int = 7) -> list[tuple[date, str, str, str]]:
    return [(u.date, u.kind, u.person.display_name, u.detail) for u in upcoming(db, today, days)]


def test_upcoming_birthdays_and_namedays(db: Session) -> None:
    make(db, "Tobiáš", birthday_month=9, birthday_day=16, birthday_year=1996)
    make(db, "Terka", first_name="Terézia", birthday_month=9, birthday_day=30)
    make(db, "Far Away", birthday_month=12, birthday_day=1)
    assert summary(db, date(2026, 9, 13)) == [
        (date(2026, 9, 13), "nameday", "Tobiáš", "Tobiáš"),
        (date(2026, 9, 16), "birthday", "Tobiáš", "turns 30"),
    ]
    # Inclusive end: today + 7 days.
    assert (date(2026, 9, 30), "birthday", "Terka", "") in summary(db, date(2026, 9, 23))


def test_upcoming_crosses_new_year(db: Session) -> None:
    make(db, "Sylvia", first_name="Silvester")
    make(db, "Alex", first_name="Alexandra", birthday_month=1, birthday_day=3, birthday_year=2000)
    assert summary(db, date(2026, 12, 29)) == [
        (date(2026, 12, 31), "nameday", "Sylvia", "Silvester"),
        (date(2027, 1, 2), "nameday", "Alex", "Alexandra"),
        (date(2027, 1, 3), "birthday", "Alex", "turns 27"),
    ]


def test_feb_29_birthday_in_non_leap_year(db: Session) -> None:
    make(db, "Leap", birthday_month=2, birthday_day=29, birthday_year=2004)
    assert summary(db, date(2027, 2, 25)) == [(date(2027, 2, 28), "birthday", "Leap", "turns 23")]
    assert summary(db, date(2028, 2, 25))[0][0] == date(2028, 2, 29)


def test_archived_excluded(db: Session) -> None:
    make(db, "Old", birthday_month=9, birthday_day=22, archived=True)
    assert summary(db, date(2026, 9, 21)) == []


def test_sorted_by_date_then_kind_then_name(db: Session) -> None:
    make(db, "Zuzana B", birthday_month=9, birthday_day=22)
    make(db, "Adam A", birthday_month=9, birthday_day=22)
    kinds = [(d, k, n) for d, k, n, _ in summary(db, date(2026, 9, 22), days=0)]
    assert kinds == [
        (date(2026, 9, 22), "birthday", "Adam A"),
        (date(2026, 9, 22), "birthday", "Zuzana B"),
    ]
