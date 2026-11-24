from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    HandleType,
    InteractionKind,
    PromiseDirection,
    PromiseStatus,
)
from orbit.domain.ledger import Debt
from orbit.domain.meetings import Meeting
from orbit.domain.people import Person
from orbit.services import interactions, ledger, meetings, people, promises
from orbit.services.context import (
    PROVIDERS,
    ContextFocus,
    ContextWindow,
    LedgerProvider,
    MeetingsProvider,
    PeopleProvider,
    PromisesProvider,
)
from orbit.services.context import base as context_base
from orbit.services.context.ledger import MAX_DEBTS, LedgerContext
from orbit.services.context.meetings import MAX_MEETINGS, MeetingsContext
from orbit.services.context.people import MAX_NOTES, MAX_PEOPLE, PeopleContext
from orbit.services.context.promises import MAX_PROMISES, PromisesContext

PRAGUE = ZoneInfo("Europe/Prague")
WINDOW = ContextWindow(date(2026, 9, 24), date(2026, 9, 30), PRAGUE)
IBAN = "SK3112000000198742637541"
PHONE = "+421 905 123 456"


def person(db: Session, name: str, **fields: object) -> Person:
    created = people.create_person(db, people.PersonCreate(display_name=name))
    for key, value in fields.items():
        setattr(created, key, value)
    db.flush()
    return created


def meeting(db: Session, uid: str, title: str, start: datetime, **fields: object) -> Meeting:
    row = Meeting(
        calendar_uid=uid,
        calendar_name="Personal",
        title=title,
        start_utc=start,
        end_utc=start + timedelta(hours=1),
        **fields,
    )
    db.add(row)
    db.flush()
    return row


def money(db: Session, who: Person, amount: int, description: str | None = None) -> None:
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=who.id,
            direction=DebtDirection.THEY_OWE_ME,
            type=DebtType.MONEY,
            amount_minor=amount,
            currency="EUR",
            description=description,
        ),
        "EUR",
    )


def money_debt(db: Session, who: Person, amount: int) -> Debt:
    return ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=who.id,
            direction=DebtDirection.THEY_OWE_ME,
            type=DebtType.MONEY,
            amount_minor=amount,
            currency="EUR",
        ),
        "EUR",
    )


def promise(db: Session, who: Person, text: str, due: date | None = None) -> None:
    data = promises.PromiseCreate(
        person_id=who.id, direction=PromiseDirection.I_PROMISED, text=text, due=due
    )
    promises.create_promise(db, data, date(2026, 9, 20))


@pytest.fixture
def world(db: Session) -> dict[str, Person]:
    """Pato (birthday in the window, a rich record), Terka (nothing happening), and an archived
    person with a birthday in the window."""
    pato = person(db, "Patrik Veselý", birthday_month=9, birthday_day=30, birthday_year=2000)
    terka = person(db, "Terka")
    person(db, "Quiet")
    person(db, "Old Friend", birthday_month=9, birthday_day=26, archived=True)
    pato.city, pato.country, pato.how_met = "Bratislava", "SK", "from the gym"
    people.add_alias(db, pato, "Paťo")
    people.add_handle(db, pato, HandleType.PHONE, PHONE)
    people.add_handle(db, pato, HandleType.IBAN, IBAN)
    people.add_note(db, pato, f"Pay him back to {IBAN} or call {PHONE}")
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 12),
            kind=InteractionKind.MEET,
            summary="pizza po gyme",
            person_ids=[pato.id],
        ),
    )
    money(db, pato, 2400, f"pizza, send to {IBAN}")
    money(db, terka, 500)
    promise(db, pato, f"send the photos to {PHONE}", due=date(2026, 9, 28))
    promise(db, terka, "return the book", due=date(2026, 9, 20))
    inside = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    first = meeting(db, "in", f"kafe s Paťom {PHONE}", inside)
    meetings.link_person(db, first, pato.id)
    meeting(db, "other", "Gym", inside + timedelta(hours=3))
    meeting(db, "later", "Too late", datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    meeting(db, "off", "Cancelled", inside, cancelled=True)
    db.commit()
    return {"pato": pato, "terka": terka}


# --- Window ---


def test_window_bounds_are_local_days() -> None:
    assert WINDOW.start_utc == datetime(2026, 9, 23, 22, 0, tzinfo=UTC)  # CEST = UTC+2
    assert WINDOW.end_utc == datetime(2026, 9, 30, 22, 0, tzinfo=UTC)
    assert WINDOW.days == 6
    assert WINDOW.contains(date(2026, 9, 30))
    assert not WINDOW.contains(date(2026, 10, 1))


def test_window_must_not_end_before_it_starts() -> None:
    with pytest.raises(ValueError):
        ContextWindow(date(2026, 9, 30), date(2026, 9, 24), PRAGUE)


# --- People ---


def test_people_without_focus_lists_who_matters_in_the_window(
    db: Session, world: dict[str, Person]
) -> None:
    result = PeopleProvider().build(db, WINDOW)
    assert isinstance(result, PeopleContext)
    # Terka has something open with me; "Quiet" has nothing; the archived person never shows.
    assert [p.name for p in result.people] == ["Patrik Veselý", "Terka"]
    pato = result.people[0]
    assert [(d.kind, d.on, d.detail) for d in pato.upcoming_dates] == [
        ("birthday", date(2026, 9, 30), "turns 26")
    ]
    assert pato.last_contact is not None
    assert (pato.last_contact.on, pato.last_contact.kind) == (date(2026, 9, 12), "meet")
    assert pato.last_contact.summary == "pizza po gyme"
    assert pato.handle_types == ["iban", "phone"]  # which channels, never the values
    assert pato.aliases == ["Paťo"]
    assert (pato.city, pato.country, pato.how_met) == ("Bratislava", "SK", "from the gym")
    assert pato.birthday is not None
    assert (pato.birthday.month, pato.birthday.day, pato.birthday.year) == (9, 30, 2000)
    assert [(i.on, i.kind, i.summary) for i in pato.interactions] == [
        (date(2026, 9, 12), "meet", "pizza po gyme")
    ]
    assert not result.truncated


def test_people_picks_up_interactions_and_keep_in_touch(
    db: Session, world: dict[str, Person]
) -> None:
    terka = world["terka"]
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 25), person_ids=[terka.id], summary="call"
        ),
    )
    zuzka = person(db, "Zuzka", cadence_days=30)  # due by the window's end
    zuzka.created_at = datetime(2026, 8, 1, tzinfo=UTC)
    db.flush()
    found = {p.name: p for p in PeopleProvider().build(db, WINDOW).people}
    assert set(found) == {"Patrik Veselý", "Terka", "Zuzka"}
    assert found["Zuzka"].keep_in_touch_due_on == date(2026, 8, 31)
    assert found["Terka"].keep_in_touch_due_on is None


def test_people_focus_returns_only_that_person(db: Session, world: dict[str, Person]) -> None:
    result = PeopleProvider().build(db, WINDOW, ContextFocus(person_id=world["terka"].id))
    assert [p.name for p in result.people] == ["Terka"]  # nothing happens, still returned
    assert PeopleProvider().build(db, WINDOW, ContextFocus(person_id=999)).people == []


def test_people_with_a_meeting_or_open_item_in_the_window_are_included(
    db: Session, world: dict[str, Person]
) -> None:
    names = {p.name for p in PeopleProvider().build(db, WINDOW).people}
    assert "Quiet" not in names
    quiet = next(p for p in db.query(Person) if p.display_name == "Quiet")
    start = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    meetings.link_person(db, meeting(db, "q", "Lunch", start), quiet.id)
    assert "Quiet" in {p.name for p in PeopleProvider().build(db, WINDOW).people}


def test_people_is_capped(db: Session) -> None:
    for i in range(MAX_PEOPLE + 5):
        person(db, f"Person {i:03d}", birthday_month=9, birthday_day=26)
    result = PeopleProvider().build(db, WINDOW)
    assert len(result.people) == MAX_PEOPLE
    assert result.truncated
    assert result.people[0].name == "Person 000"  # a fixed, name-ordered selection


def test_people_notes_are_capped(db: Session, world: dict[str, Person]) -> None:
    for i in range(MAX_NOTES + 3):
        note = people.add_note(db, world["pato"], f"note {i}")
        note.created_at = datetime(2030, 1, 1, tzinfo=UTC) + timedelta(days=i)
    db.flush()
    db.expire(world["pato"], ["notes"])
    pato = PeopleProvider().build(db, WINDOW, ContextFocus(person_id=world["pato"].id)).people[0]
    assert len(pato.notes) == MAX_NOTES
    assert pato.notes[0].text == f"note {MAX_NOTES + 2}"  # newest first
    assert pato.notes[0].on == date(2030, 1, 1) + timedelta(days=MAX_NOTES + 2)


# --- Ledger ---


def test_ledger_totals_balances_and_debts(db: Session, world: dict[str, Person]) -> None:
    result = LedgerProvider().build(db, WINDOW)
    assert isinstance(result, LedgerContext)
    assert [(t.currency, t.owed_to_me_minor, t.i_owe_minor) for t in result.totals] == [
        ("EUR", 2900, 0)
    ]
    assert [(b.person, b.amount_minor) for b in result.balances] == [
        ("Patrik Veselý", 2400),
        ("Terka", 500),
    ]
    assert {d.person for d in result.debts} == {"Patrik Veselý", "Terka"}


def test_ledger_focus_and_due_flags(db: Session, world: dict[str, Person]) -> None:
    pato = world["pato"]
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=pato.id,
            direction=DebtDirection.I_OWE_THEM,
            type=DebtType.FAVOR,
            description="helped me move",
            due=date(2026, 9, 20),
        ),
        "EUR",
    )
    result = LedgerProvider().build(db, WINDOW, ContextFocus(person_id=pato.id))
    assert [t.owed_to_me_minor for t in result.totals] == [2400]
    assert {d.person for d in result.debts} == {"Patrik Veselý"}
    favor = next(d for d in result.debts if d.type is DebtType.FAVOR)
    assert (favor.overdue, favor.due_in_window) == (True, False)
    assert favor.amount_minor is None


def test_ledger_debts_are_capped(db: Session, world: dict[str, Person]) -> None:
    for _ in range(MAX_DEBTS + 5):
        money(db, world["terka"], 100)
    result = LedgerProvider().build(db, WINDOW)
    assert len(result.debts) == MAX_DEBTS
    assert result.truncated
    assert result.totals[0].owed_to_me_minor == 2900 + 100 * (MAX_DEBTS + 5)  # totals cover all


# --- Meetings ---


def test_meetings_in_the_window_only(db: Session, world: dict[str, Person]) -> None:
    result = MeetingsProvider().build(db, WINDOW)
    assert isinstance(result, MeetingsContext)
    assert [m.title for m in result.meetings] == ["kafe s Paťom ••• 456", "Gym"]
    first = result.meetings[0]
    assert first.start == datetime(2026, 9, 25, 14, 0, tzinfo=PRAGUE)  # local time
    assert [(p.id, p.name) for p in first.people] == [(world["pato"].id, "Patrik Veselý")]


def test_meetings_focus(db: Session, world: dict[str, Person]) -> None:
    pato = MeetingsProvider().build(db, WINDOW, ContextFocus(person_id=world["pato"].id))
    assert [m.title for m in pato.meetings] == ["kafe s Paťom ••• 456"]
    terka = MeetingsProvider().build(db, WINDOW, ContextFocus(person_id=world["terka"].id))
    assert terka.meetings == []


def test_meetings_are_capped(db: Session) -> None:
    start = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
    for i in range(MAX_MEETINGS + 5):
        meeting(db, f"m{i}", f"Meeting {i}", start + timedelta(minutes=i))
    result = MeetingsProvider().build(db, WINDOW)
    assert len(result.meetings) == MAX_MEETINGS
    assert result.truncated


# --- Promises ---


def test_promises_open_soonest_first(db: Session, world: dict[str, Person]) -> None:
    result = PromisesProvider().build(db, WINDOW)
    assert isinstance(result, PromisesContext)
    assert [(p.person, p.overdue, p.due_in_window) for p in result.promises] == [
        ("Terka", True, False),
        ("Patrik Veselý", False, True),
    ]


def test_promises_focus_and_cap(db: Session, world: dict[str, Person]) -> None:
    only_pato = PromisesProvider().build(db, WINDOW, ContextFocus(person_id=world["pato"].id))
    assert [p.person for p in only_pato.promises] == ["Patrik Veselý"]
    for i in range(MAX_PROMISES + 5):
        promise(db, world["terka"], f"thing {i}")
    result = PromisesProvider().build(db, WINDOW)
    assert len(result.promises) == MAX_PROMISES
    assert result.truncated


# --- History: recently settled debts and closed promises ---


def test_ledger_lists_recently_settled_debts(db: Session, world: dict[str, Person]) -> None:
    old, recent = (money_debt(db, world["pato"], 100 + i) for i in range(2))
    ledger.settle(db, old, None, datetime(2026, 5, 1, tzinfo=UTC))  # long before the window
    ledger.settle(db, recent, None, datetime(2026, 9, 10, 12, 0, tzinfo=UTC))
    result = LedgerProvider().build(db, WINDOW)
    assert [(d.amount_minor, d.settled_on) for d in result.recently_settled] == [
        (101, date(2026, 9, 10))
    ]
    assert all(d.settled_on is None for d in result.debts)  # open ones carry no settle date
    focused = LedgerProvider().build(db, WINDOW, ContextFocus(person_id=world["terka"].id))
    assert focused.recently_settled == []


def test_promises_list_recently_closed(db: Session, world: dict[str, Person]) -> None:
    done = promises.list_promises(db, person_id=world["terka"].id)[0]
    promises.set_status(done, PromiseStatus.DONE, datetime(2026, 9, 20, 12, 0, tzinfo=UTC))
    result = PromisesProvider().build(db, WINDOW)
    assert [(p.text, p.status, p.closed_on) for p in result.recently_closed] == [
        ("return the book", PromiseStatus.DONE, date(2026, 9, 20))
    ]
    assert [p.person for p in result.promises] == ["Patrik Veselý"]


def test_old_closed_promises_are_left_out(db: Session, world: dict[str, Person]) -> None:
    done = promises.list_promises(db, person_id=world["terka"].id)[0]
    promises.set_status(done, PromiseStatus.DROPPED, datetime(2026, 7, 1, tzinfo=UTC))
    assert PromisesProvider().build(db, WINDOW).recently_closed == []


# --- Masking ---


def test_text_masks_before_it_clips() -> None:
    masked = context_base.text(f"call {PHONE} now", limit=200)
    assert masked == "call ••• 456 now"
    clipped = context_base.text("x" * 10 + f" {IBAN}", limit=14)
    assert IBAN[:8] not in clipped  # never cut an IBAN in half and leave the start readable
    assert len(clipped) <= 14


def test_text_is_one_line() -> None:
    assert context_base.text("a\n\n  b\tc") == "a b c"


def test_nothing_sensitive_leaves_any_provider(db: Session, world: dict[str, Person]) -> None:
    dumped = []
    for focus in (None, ContextFocus(person_id=world["pato"].id)):
        for provider in PROVIDERS:
            dumped.append(provider.build(db, WINDOW, focus).model_dump_json())
    blob = " ".join(dumped)
    assert IBAN not in blob
    assert "1987426375" not in blob
    assert "905 123" not in blob
    assert "905123" not in blob
    assert "421905" not in blob
    assert "••• 456" in blob  # masked, not dropped


# --- All four, for one person ---


def test_all_providers_build_context_for_one_person(db: Session, world: dict[str, Person]) -> None:
    focus = ContextFocus(person_id=world["pato"].id)
    results = {p.name: p.build(db, WINDOW, focus) for p in PROVIDERS}
    assert set(results) == {
        "goals",
        "school",
        "people",
        "ledger",
        "meetings",
        "promises",
        "fitness",
        "health",
    }
    assert all(r.module == name and not r.truncated for name, r in results.items())
    assert isinstance(results["people"], PeopleContext)
    assert isinstance(results["ledger"], LedgerContext)
    assert isinstance(results["meetings"], MeetingsContext)
    assert isinstance(results["promises"], PromisesContext)
    assert [p.name for p in results["people"].people] == ["Patrik Veselý"]
    assert [d.person for d in results["ledger"].debts] == ["Patrik Veselý"]
    assert [m.title for m in results["meetings"].meetings] == ["kafe s Paťom ••• 456"]
    assert [p.person for p in results["promises"].promises] == ["Patrik Veselý"]
