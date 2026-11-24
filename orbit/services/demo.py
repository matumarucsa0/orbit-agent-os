"""Demo data: a fictional persona ("Alex Novak", a student and developer in Prague).

`seed_demo` fills an empty database so the UI, the Inbox and the weekly review can be tried, and
screenshots taken, on a clean machine. Everything is invented: names, merchants, phone numbers
(UK drama range), e-mail (example.com) and IBANs (all zeros). The bank rows are written as real
Tatra and Revolut CSV text and go through the real import path; the Inbox proposals, the weekly
review and the token statistics come from `FakeClaude` through the same services the app uses.

Dates are relative to `today`, and a fixed seed drives every number, so two runs on the same day
give the same data (DECISIONS #35).
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from orbit.domain import Base
from orbit.domain.bank import BankImport
from orbit.domain.base import utcnow
from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    GoalHorizon,
    HandleType,
    InteractionKind,
    MeetingLinkSource,
    NamedayCountry,
    PromiseDirection,
)
from orbit.domain.goals import Goal
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.people import Person
from orbit.domain.usage import ClaudeCall
from orbit.integrations.fake_claude import FakeClaude
from orbit.services import (
    bank_import,
    capture,
    fitness,
    goals,
    health,
    interactions,
    ledger,
    people,
    promises,
    review,
)
from orbit.services.classify import ClassifyContext
from orbit.services.errors import InvalidInputError
from orbit.services.usage import UsageLog

SEED = 20261124
OWNER_FIRST_NAME = "Alex"
PERSONA = "Alex Novak"
DEMO_FILE_PREFIX = "demo-"
CURRENCY = "EUR"
SHARED_MIN_MINOR = 1500
ZERO_IBAN = "SK00 0000 0000 0000 0000 00{:02d}"


class NotEmptyError(InvalidInputError):
    """The database already holds data that is not the demo."""


@dataclass
class DemoReport:
    counts: dict[str, int] = field(default_factory=dict)
    already_seeded: bool = False

    def line(self) -> str:
        return ", ".join(f"{n} {name}" for name, n in self.counts.items())


# --- Who is in the database already ---

_CONTENT_MODELS: tuple[Any, ...] = (Person, Goal, Debt, Promise, Meeting, BankImport, Proposal)


def is_empty(session: Session) -> bool:
    return all(session.scalar(select(func.count()).select_from(m)) == 0 for m in _CONTENT_MODELS)


def is_demo(session: Session) -> bool:
    """Seeded by `seed_demo` before: its bank imports carry the demo file names."""
    found = session.scalar(
        select(func.count())
        .select_from(BankImport)
        .where(BankImport.filename.like(f"{DEMO_FILE_PREFIX}%"))
    )
    return bool(found)


def wipe(session: Session) -> None:
    """Delete every row of every table (`seed-demo --force`)."""
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(delete(table))
    session.flush()


# --- The cast ---


@dataclass(frozen=True)
class _Friend:
    name: str
    nick: str | None
    country: str
    city: str
    tags: tuple[str, ...]
    birthday: tuple[int, int, int | None]  # month, day, year; (0, offset, year) = in `offset` days
    how_met: str
    instagram: str | None = None
    phone: str | None = None
    email: str | None = None
    iban: int | None = None  # the last two digits of an all-zero IBAN
    notes: tuple[str, ...] = ()
    last_met_days_ago: int | None = None


FRIENDS: tuple[_Friend, ...] = (
    _Friend(
        "Jana Svobodova",
        "Janca",
        "CZ",
        "Prague",
        ("uni", "friend"),
        (0, 3, 2004),
        "Linear algebra tutorial",
        "jana.svob",
        "+44 7700 900101",
        "jana@example.com",
        1,
        ("Moving to a new flat in December.", "Plays volleyball on Tuesdays."),
        6,
    ),
    _Friend(
        "Tomas Dvorak",
        "Tom",
        "CZ",
        "Prague",
        ("gym", "friend"),
        (3, 18, 2002),
        "Gym",
        "tom.dvorak",
        "+44 7700 900102",
        None,
        2,
        ("Training for a 10 km race in spring.",),
        2,
    ),
    _Friend(
        "Eva Novakova",
        None,
        "CZ",
        "Brno",
        ("family",),
        (0, 12, 1998),
        "Family",
        None,
        "+44 7700 900103",
        "eva@example.com",
        None,
        ("Starts a new job on the 1st.",),
        15,
    ),
    _Friend(
        "Martin Kral",
        "Marty",
        "CZ",
        "Prague",
        ("friend", "flatmate"),
        (7, 9, 2003),
        "Flatmate",
        "marty.kral",
        "+44 7700 900104",
        None,
        3,
        (),
        1,
    ),
    _Friend(
        "Lenka Horakova",
        None,
        "CZ",
        "Prague",
        ("uni",),
        (11, 30, 2004),
        "Programming course",
        "lenka.h",
        None,
        "lenka@example.com",
        None,
        ("Needs the graph theory notes.",),
        11,
    ),
    _Friend(
        "Petr Cerny",
        None,
        "CZ",
        "Plzen",
        ("friend",),
        (4, 2, 2001),
        "Summer job",
        None,
        "+44 7700 900105",
        None,
        4,
        ("Is learning to climb.",),
        34,
    ),
    _Friend(
        "Tereza Mala",
        "Terka",
        "SK",
        "Bratislava",
        ("friend",),
        (0, 21, 2003),
        "School",
        "terka.mala",
        "+44 7700 900106",
        None,
        5,
        ("Concert in January, bought the tickets.",),
        9,
    ),
    _Friend(
        "Ondrej Prochazka",
        "Ondra",
        "CZ",
        "Prague",
        ("uni", "friend"),
        (9, 14, 2004),
        "Student club",
        "ondra.p",
        None,
        "ondra@example.com",
        None,
        (),
        4,
    ),
    _Friend(
        "Sofia Marek",
        "Sofi",
        "SK",
        "Kosice",
        ("friend",),
        (2, 25, 2003),
        "Erasmus",
        "sofi.marek",
        None,
        None,
        None,
        ("Lives in Warsaw this semester.",),
        52,
    ),
    _Friend(
        "Daniel Vesely",
        "Dano",
        "SK",
        "Bratislava",
        ("friend", "gym"),
        (6, 6, 2002),
        "Gym",
        None,
        "+44 7700 900107",
        None,
        6,
        (),
        20,
    ),
)

TAG_CADENCE = {"family": 14, "friend": 30, "uni": 45}


def _birthday(item: _Friend, today: date) -> tuple[int, int, int | None]:
    month, day, year = item.birthday
    if month == 0:  # "in `day` days"
        due = today + timedelta(days=day)
        return due.month, due.day, year
    return month, day, year


def _seed_people(session: Session, today: date) -> dict[str, Person]:
    found: dict[str, Person] = {}
    for item in FRIENDS:
        month, day, year = _birthday(item, today)
        first, _, last = item.name.partition(" ")
        person = people.create_person(
            session,
            people.PersonCreate(
                display_name=item.name,
                first_name=first,
                last_name=last or None,
                birthday_month=month,
                birthday_day=day,
                birthday_year=year,
                nameday_country=NamedayCountry(item.country),
                country=item.country,
                city=item.city,
                how_met=item.how_met,
            ),
        )
        if item.nick:
            people.add_alias(session, person, item.nick)
        for tag in item.tags:
            people.add_tag(session, person, tag)
        if item.instagram:
            people.add_handle(session, person, HandleType.INSTAGRAM, item.instagram)
        if item.phone:
            people.add_handle(session, person, HandleType.PHONE, item.phone)
        if item.email:
            people.add_handle(session, person, HandleType.EMAIL, item.email)
        if item.iban is not None:
            people.add_handle(session, person, HandleType.IBAN, ZERO_IBAN.format(item.iban))
        for text in item.notes:
            people.add_note(session, person, text)
        if item.last_met_days_ago is not None:
            interactions.create_interaction(
                session,
                interactions.InteractionCreate(
                    occurred_on=today - timedelta(days=item.last_met_days_ago),
                    kind=InteractionKind.MEET,
                    summary="Coffee and a catch-up"
                    if item.last_met_days_ago > 3
                    else "Quick hello",
                    person_ids=[person.id],
                ),
            )
        found[first] = person
        if item.nick:
            found[item.nick] = person
    for tag in {t for item in FRIENDS for t in item.tags}:
        if tag in TAG_CADENCE:
            people.get_or_create_tag(session, tag).cadence_days = TAG_CADENCE[tag]
    session.flush()
    return found


def _backdate(row: Any, when: datetime) -> None:
    row.created_at = when
    row.updated_at = when


def _seed_ledger(
    session: Session, who: dict[str, Person], today: date, now: datetime
) -> list[Debt]:
    def money(
        name: str,
        direction: DebtDirection,
        minor: int,
        text: str,
        since: int,
        due: int | None = None,
        currency: str = CURRENCY,
    ) -> Debt:
        debt = ledger.create_debt(
            session,
            ledger.DebtCreate(
                person_id=who[name].id,
                direction=direction,
                amount_minor=minor,
                currency=currency,
                description=text,
                due=today + timedelta(days=due) if due is not None else None,
            ),
            CURRENCY,
        )
        _backdate(debt, now - timedelta(days=since))
        return debt

    def thing(name: str, direction: DebtDirection, kind: DebtType, text: str, since: int) -> Debt:
        debt = ledger.create_debt(
            session,
            ledger.DebtCreate(
                person_id=who[name].id, direction=direction, type=kind, description=text
            ),
            CURRENCY,
        )
        _backdate(debt, now - timedelta(days=since))
        return debt

    they, mine = DebtDirection.THEY_OWE_ME, DebtDirection.I_OWE_THEM
    made = [
        money("Tomas", they, 2450, "Pizza and drinks after the gym", 20, due=-5),
        money("Martin", they, 1500, "Shared groceries", 8),
        money("Jana", mine, 1200, "Cinema ticket", 6, due=4),
        money("Terka", they, 4500, "Concert tickets", 25, due=-9),
        money("Dano", they, 20000, "Taxi back from the station", 12, currency="CZK"),
        thing("Lenka", they, DebtType.ITEM, "USB-C cable", 30),
        thing("Petr", mine, DebtType.FAVOR, "Help with moving boxes", 14),
        thing("Eva", they, DebtType.ITEM, "Board game (Azul)", 45),
    ]
    paid = money("Ondrej", they, 800, "Coffee beans", 18)
    ledger.settle(session, paid, now=now - timedelta(days=3))
    session.flush()
    return made


def _seed_goals_promises(session: Session, who: dict[str, Person], today: date) -> None:
    now = utcnow()
    made = [
        goals.create_goal(
            session,
            goals.GoalCreate(
                title="Pass the linear algebra exam",
                why="The first-year exam decides whether I can take the second-year courses.",
                horizon=GoalHorizon.QUARTER,
                target="At least 60 points on the exam",
                target_date=today + timedelta(days=40),
            ),
            now - timedelta(days=9),
        ),
        goals.create_goal(
            session,
            goals.GoalCreate(
                title="Run a 10 km race in spring",
                why="Three runs a week keeps my head clear.",
                horizon=GoalHorizon.YEAR,
                target="10 km under 55 minutes",
                target_date=today + timedelta(days=120),
            ),
            now - timedelta(days=3),
        ),
        goals.create_goal(
            session,
            goals.GoalCreate(
                title="Ship the Orbit demo",
                why="A small project I can show in interviews.",
                horizon=GoalHorizon.MONTH,
                target="Public repository with screenshots",
            ),
            now - timedelta(days=1),
        ),
        goals.create_goal(
            session,
            goals.GoalCreate(
                title="Stay in touch with family",
                horizon=GoalHorizon.ONGOING,
                why="Call home every Sunday.",
            ),
            now - timedelta(days=12),
        ),
    ]
    algebra, running, demo, family = made
    goals.add_person(session, algebra, who["Jana"].id)
    goals.add_person(session, running, who["Tomas"].id)
    goals.add_person(session, family, who["Eva"].id)

    def promise(
        name: str, direction: PromiseDirection, text: str, due: int, goal: Goal | None = None
    ) -> None:
        promises.create_promise(
            session,
            promises.PromiseCreate(
                person_id=who[name].id,
                direction=direction,
                text=text,
                due=today + timedelta(days=due),
                goal_id=goal.id if goal else None,
            ),
            today,
        )

    mine, theirs = PromiseDirection.I_PROMISED, PromiseDirection.THEY_PROMISED
    promise("Jana", mine, "Send the tutorial notes on eigenvalues", -3, algebra)
    promise("Lenka", mine, "Share the graph theory notes", 2)
    promise("Tomas", theirs, "Plan a long run route for Saturday", 3, running)
    promise("Eva", mine, "Call on Sunday evening", 4, family)
    promise("Terka", theirs, "Send the concert seat numbers", -1)
    promise("Martin", mine, "Fix the shelf in the hallway", -6)
    promise("Ondrej", theirs, "Review my README before I publish it", 6, demo)


def _seed_meetings(session: Session, who: dict[str, Person], today: date, tz: ZoneInfo) -> int:
    plan: list[tuple[int, str, str, int, tuple[str, ...], str | None]] = [
        # day offset, title, start "HH:MM", minutes, who, location
        (-5, "Algebra tutorial", "08:00", 90, ("Jana", "Lenka"), "Lecture hall B2"),
        (-4, "Gym with Tom", "18:00", 75, ("Tomas",), "Fitline Gym"),
        (-2, "Dinner with Martin", "19:30", 90, ("Martin",), "Grill Zahrada"),
        (-1, "Study group", "16:00", 120, ("Jana", "Ondrej"), "Library"),
        (0, "Coffee with Jana", "10:30", 45, ("Jana",), "Northwood Cafe"),
        (1, "Gym with Tom", "18:00", 75, ("Tomas",), "Fitline Gym"),
        (2, "Lunch with Terka", "12:30", 60, ("Terka",), "Bistro Modry Pes"),
        (3, "Algebra seminar", "09:15", 90, (), "Lecture hall B2"),
        (5, "Dinner with Eva", "19:00", 120, ("Eva",), "Pizzeria Luna"),
        (7, "Project work session", "14:00", 180, ("Ondrej",), None),
        (9, "Birthday drinks for Janca", "20:00", 150, ("Jana", "Martin", "Tomas"), "Bar Kometa"),
    ]
    for offset, title, start, minutes, names, where in plan:
        hour, minute = (int(x) for x in start.split(":"))
        begin = datetime.combine(today + timedelta(days=offset), time(hour, minute), tzinfo=tz)
        meeting = Meeting(
            calendar_uid=f"demo-{offset}-{title}".replace(" ", "-").lower(),
            calendar_name="Calendar",
            title=title,
            location=where,
            start_utc=begin.astimezone(UTC),
            end_utc=(begin + timedelta(minutes=minutes)).astimezone(UTC),
            cancelled=False,
            brief_sent_at=begin.astimezone(UTC) if offset <= 0 else None,
            capture_sent_at=begin.astimezone(UTC) if offset < 0 else None,
        )
        for name in names:
            meeting.links.append(
                MeetingPerson(person_id=who[name].id, link_source=MeetingLinkSource.TITLE)
            )
        session.add(meeting)
    session.flush()
    return len(plan)


# --- Bank statements, written as the banks write them ---

_MONTH_TEMPLATES: tuple[tuple[str, int, int, int], ...] = (
    # merchant, min cents, max cents, how often in 30 days
    ("Potraviny Sever", 380, 1450, 7),
    ("Northwood Cafe", 290, 890, 5),
    ("Fitline Gym", 1400, 1400, 1),
    ("City Transit Ticket", 390, 390, 3),
    ("Corner Pharmacy", 450, 1190, 2),
)
_SHARED_MERCHANTS: tuple[tuple[str, int, int], ...] = (
    ("Grill Zahrada", 3480, 5290),
    ("Pizzeria Luna", 2260, 4120),
    ("Bistro Modry Pes", 1890, 3350),
    ("Kino Alfa", 1800, 2600),
    ("Taxi Hvezda", 1520, 2480),
)


def _money(cents: int) -> str:
    return f"{cents // 100},{cents % 100:02d}"


def _tatra_csv(rows: list[dict[str, str]]) -> bytes:
    header = [
        "Dátum spracovania",
        "Dátum zúčtovania",
        "Suma",
        "Mena",
        "Typ",
        "Predčíslie",
        "Číslo účtu",
        "Kód banky",
        "IBAN",
        "Variabilný symbol",
        "Špecifický symbol",
        "Konštantný symbol",
        "Referencia platiteľa",
        "Informácia pre príjemcu",
        "Popis",
    ]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, header, restval="", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("cp1250")


def _revolut_csv(rows: list[list[str]]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [
            "Type",
            "Product",
            "Started Date",
            "Completed Date",
            "Description",
            "Amount",
            "Fee",
            "Currency",
            "State",
            "Balance",
        ]
    )
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def build_statements(rng: random.Random, today: date) -> tuple[bytes, bytes]:
    """Thirty days of synthetic rows in both bank formats: (Tatra cp1250, Revolut utf-8)."""
    tatra: list[dict[str, str]] = []

    def card(day: date, at: time, cents: int, merchant: str, place: str = "Praha 2") -> None:
        stamp = f"{day:%Y%m%d} {at:%H:%M:%S}"
        tatra.append(
            {
                "Dátum spracovania": f"{day:%d.%m.%Y}",
                "Dátum zúčtovania": f"{day:%d.%m.%Y}",
                "Suma": f"{_money(cents)}",
                "Mena": CURRENCY,
                "Typ": "Debet",
                "Informácia pre príjemcu": (
                    f"400000******0000 {place} NOVAK ALEX {stamp} {merchant}"
                ),
                "Popis": "Platba kartou 4000**0000",
            }
        )

    for merchant, low, high, times in _MONTH_TEMPLATES:
        for _ in range(times):
            day = today - timedelta(days=rng.randint(1, 30))
            at = time(rng.randint(8, 21), rng.randint(0, 59), rng.randint(0, 59))
            card(day, at, rng.randint(low, high), merchant)
    for merchant, low, high in _SHARED_MERCHANTS:
        for _ in range(1):
            day = today - timedelta(days=rng.randint(1, 28))
            card(
                day,
                time(rng.randint(12, 22), rng.randint(0, 59), 0),
                rng.randint(low, high),
                merchant,
            )
    # Meal bills that fall on calendar meetings (Dinner with Martin 2 days ago, study group, ...).
    card(today - timedelta(days=2), time(21, 12, 5), 4260, "Grill Zahrada")
    card(today - timedelta(days=1), time(17, 48, 31), 2190, "Northwood Cafe")
    # A friend pays Alex back: 24.50 EUR matches the open debt of "Tomas".
    paid = today - timedelta(days=1)
    tatra.append(
        {
            "Dátum spracovania": f"{paid:%d.%m.%Y}",
            "Dátum zúčtovania": f"{paid:%d.%m.%Y}",
            "Suma": "24,50",
            "Mena": CURRENCY,
            "Typ": "Kredit",
            "IBAN": ZERO_IBAN.format(2).replace(" ", ""),
            "Informácia pre príjemcu": "pizza",
            "Popis": f"Platba OO{paid:%y%m%d}00012345",
        }
    )
    tatra.append(
        {
            "Dátum spracovania": f"{today - timedelta(days=15):%d.%m.%Y}",
            "Dátum zúčtovania": f"{today - timedelta(days=15):%d.%m.%Y}",
            "Suma": "300,00",
            "Mena": CURRENCY,
            "Typ": "Debet",
            "IBAN": "SK0000000000000000000099",
            "Informácia pre príjemcu": "Rent share",
            "Popis": f"Platba OO{today - timedelta(days=15):%y%m%d}00054321",
        }
    )
    tatra.sort(
        key=lambda r: (
            r["Dátum spracovania"][6:],
            r["Dátum spracovania"][3:5],
            r["Dátum spracovania"][:2],
        )
    )

    revolut: list[list[str]] = []
    balance = 18034
    for offset in (29, 22, 15, 8, 1):
        day = today - timedelta(days=offset)
        started = datetime.combine(day, time(9, rng.randint(0, 59), rng.randint(0, 59)))
        done = started + timedelta(seconds=3)
        balance += 5000
        revolut.append(
            [
                "Topup",
                "Current",
                f"{started:%Y-%m-%d %H:%M:%S}",
                f"{done:%Y-%m-%d %H:%M:%S}",
                "Apple Pay top-up by *0000",
                "50.00",
                "0.00",
                "EUR",
                "COMPLETED",
                f"{balance / 100:.2f}",
            ]
        )
    for merchant, low, high in (
        ("StreamBox", 599, 599),
        ("Bistro Modry Pes", 1500, 2800),
        ("Kino Alfa", 900, 1200),
        ("Corner Bookshop", 1200, 3200),
        ("Taxi Hvezda", 780, 1650),
    ):
        day = today - timedelta(days=rng.randint(1, 28))
        started = datetime.combine(day, time(rng.randint(10, 22), rng.randint(0, 59), 12))
        cents = rng.randint(low, high)
        balance -= cents
        revolut.append(
            [
                "Card Payment",
                "Current",
                f"{started:%Y-%m-%d %H:%M:%S}",
                f"{started + timedelta(seconds=2):%Y-%m-%d %H:%M:%S}",
                merchant,
                f"-{cents / 100:.2f}",
                "0.00",
                "EUR",
                "COMPLETED",
                f"{balance / 100:.2f}",
            ]
        )
    revolut.sort(key=lambda r: r[2])
    return _tatra_csv(tatra), _revolut_csv(revolut)


def _seed_bank(
    session: Session, today: date, tz: ZoneInfo, classifier: FakeClaude, rng: random.Random
) -> int:
    tatra, revolut = build_statements(rng, today)
    context = ClassifyContext(CURRENCY, SHARED_MIN_MINOR, today, tz)
    total = 0
    for name, raw in (("tatra", tatra), ("revolut", revolut)):
        report = bank_import.import_statement(
            session,
            raw,
            filename=f"{DEMO_FILE_PREFIX}{name}.csv",
            bank=None,
            context=context,
            classifier=classifier,
            rates=None,
        )
        total += len(report.new)
    return total


# --- Workouts and sleep ---

_LIFTS = (
    ("Barbell Squat", "barbell_squat", 135, 5, 5),
    ("Bench Press", "bench_press", 115, 8, 4),
    ("Deadlift", "deadlift", 185, 5, 3),
    ("Overhead Press", "overhead_press", 75, 8, 4),
    ("Lat Pulldown", "lat_pulldown", 110, 10, 3),
    ("Cable Row", "cable_row", 100, 10, 3),
)


def build_workouts(rng: random.Random, today: date, count: int = 9) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for index in range(count):
        day = today - timedelta(days=2 + index * 3 + rng.randint(0, 1))
        start = datetime.combine(day, time(17, rng.randint(0, 40)), tzinfo=UTC) - timedelta(hours=1)
        minutes = rng.randint(48, 80)
        lifts = rng.sample(_LIFTS, 4)
        exercises: list[dict[str, Any]] = []
        for position, (name, key, base, reps, sets) in enumerate(lifts):
            weight = base + 5 * (index // 2) + rng.choice((0, 0, 5))
            exercises.append(
                {
                    "exerciseIndex": position,
                    "exerciseName": name,
                    "exerciseId": key,
                    "exerciseTypes": "WR",
                    "exerciseNotes": "",
                    "setsData": [
                        {
                            "setIndex": s,
                            "setType": "normal",
                            "inputOne": float(weight),
                            "inputTwo": reps - (1 if s == sets - 1 else 0),
                        }
                        for s in range(sets)
                    ],
                }
            )
        posts.append(
            {
                "id": f"demo-workout-{index + 1:03d}",
                "startedAt": start.isoformat().replace("+00:00", "Z"),
                "postedAt": (start + timedelta(minutes=minutes + 5))
                .isoformat()
                .replace("+00:00", "Z"),
                "sessionDuration": (
                    f"{minutes // 60:02d} hours {minutes % 60:02d} minutes 00 seconds"
                ),
                "sessionNotes": rng.choice(
                    ("Felt strong.", "Shoulders a bit tired.", "Short on sleep, kept it easy.", "")
                ),
                "bodyweight": f"{165 + rng.random() * 2:.1f}",
                "caloriesBurned": rng.randint(240, 420),
                "prCount": rng.choice((0, 0, 1)),
                "exerciseData": exercises,
            }
        )
    return posts


class _Posts:
    def __init__(self, posts: list[dict[str, Any]]) -> None:
        self._posts = posts

    def workouts(self) -> list[dict[str, Any]]:
        return self._posts


def _seed_health(session: Session, today: date, tz: ZoneInfo, rng: random.Random) -> int:
    count = 0
    for back in range(1, 31):
        day = today - timedelta(days=back)
        asleep = rng.randint(380, 500)
        awake = rng.randint(15, 45)
        deep, rem = rng.randint(55, 95), rng.randint(70, 115)
        bed = datetime.combine(day - timedelta(days=1), time(23, rng.randint(0, 50)), tzinfo=tz)
        end = bed + timedelta(minutes=asleep + awake)
        health.upsert_day(
            session,
            health.DailyHealth(
                day=day,
                sleep_minutes=asleep,
                sleep_start=bed.astimezone(UTC),
                sleep_end=end.astimezone(UTC),
                sleep_deep_minutes=deep,
                sleep_rem_minutes=rem,
                sleep_core_minutes=asleep - deep - rem,
                sleep_awake_minutes=awake,
                sleep_in_bed_minutes=asleep + awake,
                resting_hr=rng.randint(52, 60),
                steps=rng.randint(4200, 13500),
                active_kcal=rng.randint(280, 760),
                weight_grams=75000 + rng.randint(-600, 600),
            ),
            utcnow(),
        )
        count += 1
    return count


# --- Inbox: dictations through the real capture path ---

DICTATIONS = (
    "Coffee with Jana, she moved to Brno last week. She owes me 12 euro for the tickets.",
    "I promised Lenka the graph theory notes by Friday",
    "Haircut tomorrow at 15:00, then a beer with Katka from the student club",
    "Tomas lent me his powerbank, I owe him that",
)


def _seed_inbox(
    session: Session, claude: FakeClaude, today: date, tz: ZoneInfo, now: datetime
) -> None:
    for text in DICTATIONS:
        capture.capture(
            session, claude, text, today=today, default_currency=CURRENCY, tz=tz, now=now
        )
    # One dictation that Claude could not read: stays a note draft, as it does in real life.
    capture.capture(
        session,
        None,
        "Ask Sofi about the Warsaw trip",
        today=today,
        default_currency=CURRENCY,
        tz=tz,
        now=now,
    )


def _seed_review(
    session: Session, claude: FakeClaude, tz: ZoneInfo, now: datetime, budget: int
) -> None:
    plan_start = review.weeks_around(now.astimezone(tz).date()).plan_start
    row = review.claim(session, plan_start, now, retry=True)
    if row is None:
        return
    review.generate(session, row, claude, tz=tz, budget_chars=budget, now=now)


def seed_demo(
    sessions: sessionmaker[Session],
    *,
    today: date | None = None,
    tz: ZoneInfo | None = None,
    force: bool = False,
    context_budget_chars: int = 400_000,
) -> DemoReport:
    """Fill an empty database with the demo persona. Already seeded: nothing happens.

    A database with other data raises `NotEmptyError` unless `force`, which empties it first."""
    tz = tz or ZoneInfo("Europe/Prague")
    now = datetime.now(UTC) if today is None else datetime.combine(today, time(12), tzinfo=tz)
    today = today or now.astimezone(tz).date()
    with sessions() as session:
        if force:
            wipe(session)
            session.commit()
        elif is_demo(session):
            return DemoReport({}, already_seeded=True)
        elif not is_empty(session):
            raise NotEmptyError("The database already has data; use --force to replace it.")
        rng = random.Random(SEED)
        usage_log = UsageLog()
        claude = FakeClaude(on_usage=usage_log.add, owner_name=OWNER_FIRST_NAME)
        who = _seed_people(session, today)
        debts = _seed_ledger(session, who, today, now)
        _seed_goals_promises(session, who, today)
        meetings = _seed_meetings(session, who, today, tz)
        rows = _seed_bank(session, today, tz, claude, rng)
        fitness.sync_workouts(session, _Posts(build_workouts(rng, today)))
        sleep_days = _seed_health(session, today, tz, rng)
        _seed_inbox(session, claude, today, tz, now)
        session.flush()
        _seed_review(session, claude, tz, now, context_budget_chars)
        usage_log.flush(session)
        session.commit()
        return DemoReport(
            {
                "people": len(FRIENDS),
                "debts": len(debts) + 1,
                "meetings": meetings,
                "bank rows": rows,
                "workouts": fitness.total_workouts(session),
                "sleep days": sleep_days,
                "inbox proposals": session.scalar(select(func.count()).select_from(Proposal)) or 0,
                "claude calls": session.scalar(select(func.count()).select_from(ClaudeCall)) or 0,
            }
        )
