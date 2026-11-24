from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    InteractionKind,
    PromiseDirection,
    PromiseStatus,
    ProposalKind,
    ProposalStatus,
)
from orbit.domain.inbox import Notification, Proposal
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting
from orbit.domain.people import Person
from orbit.services import interactions, ledger, meetings, people, promises, reminders
from orbit.services.notifications import QuietHours, flush_due
from tests.test_notifications import FakeSender

PRAGUE = ZoneInfo("Europe/Prague")
QUIET = QuietHours(time(22, 0), time(8, 0), PRAGUE)
URL = "https://orbit.example.com"
TODAY = date(2026, 9, 24)  # a Thursday; CEST, UTC+2
NINE = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)  # 09:00 in Prague
SUNDAY_SIX = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)  # Sunday 18:00 in Prague


def run(
    db: Session,
    now: datetime,
    sender: FakeSender | None = None,
    quiet: QuietHours = QUIET,
    item_days: int = 21,
) -> int:
    return reminders.send_reminders(
        db,
        sender if sender is not None else FakeSender(),
        base_url=URL,
        tz=PRAGUE,
        quiet=quiet,
        item_reminder_days=item_days,
        now=now,
    )


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=PRAGUE).astimezone(UTC)


def person(db: Session, name: str, *tags: str, **fields: Any) -> Person:
    created = people.create_person(db, people.PersonCreate(display_name=name, **fields))
    for tag in tags:
        people.add_tag(db, created, tag)
    return created


def birthday(db: Session, name: str, on: date, *tags: str, year: int | None = None) -> Person:
    return person(
        db,
        name,
        *tags,
        birthday_month=on.month,
        birthday_day=on.day,
        birthday_year=year,
        nameday_country=None,
    )


def keys(db: Session) -> set[str]:
    return set(db.scalars(select(Notification.dedupe_key)))


def lend(db: Session, who: Person, what: str, days_ago: int, **fields: Any) -> Debt:
    debt = ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=who.id,
            direction=fields.pop("direction", DebtDirection.THEY_OWE_ME),
            type=DebtType.ITEM,
            description=what,
            **fields,
        ),
        "EUR",
    )
    debt.created_at = NINE - timedelta(days=days_ago)
    db.flush()
    return debt


def promise(
    db: Session,
    who: Person,
    text: str,
    due: date,
    direction: PromiseDirection = PromiseDirection.I_PROMISED,
) -> Promise:
    created = promises.create_promise(
        db,
        promises.PromiseCreate(person_id=who.id, direction=direction, text=text, due=due),
        TODAY,
    )
    created.created_at = local(date(2026, 9, 10), 12)
    db.flush()
    return created


# --- Birthdays and name days ---


def test_birthday_push_at_nine_on_the_day(db: Session) -> None:
    pato = birthday(db, "Paťo", TODAY, year=1996)
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 12),
            kind=InteractionKind.MEET,
            summary="pizza po gyme",
            person_ids=[pato.id],
        ),
    )
    sender = FakeSender()
    assert run(db, NINE - timedelta(minutes=1), sender) == 0
    assert run(db, NINE, sender) == 1
    [push] = sender.sent
    assert push.title == "🎂 Paťo: birthday today"
    assert push.body == "Turns 30\n🗓 last meet 12 Sep: pizza po gyme"
    assert push.click == f"{URL}/people/{pato.id}"
    assert keys(db) == {f"birthday:{pato.id}:2026-09-24:day"}
    # Every minute after that: nothing new.
    assert run(db, NINE + timedelta(minutes=1), sender) == 0
    assert run(db, local(TODAY, 20), sender) == 0
    assert len(sender.sent) == 1


def test_missed_nine_oclock_still_sends_later_that_day(db: Session) -> None:
    birthday(db, "Terka", TODAY)
    sender = FakeSender()
    assert run(db, local(TODAY, 14, 30), sender) == 1
    assert sender.sent[0].body == "Wish them well."


def test_no_birthday_push_that_would_arrive_the_next_day(db: Session) -> None:
    """Server down all day, back at 23:30 (quiet hours until 08:00 tomorrow): too late."""
    birthday(db, "Terka", TODAY)
    assert run(db, local(TODAY, 23, 30)) == 0
    assert keys(db) == set()
    # And the next day it's no longer "today".
    assert run(db, local(TODAY + timedelta(days=1), 9)) == 0


def test_quiet_hours_ending_later_in_the_morning_hold_the_push(db: Session) -> None:
    birthday(db, "Terka", TODAY)
    sender = FakeSender()
    late_mornings = QuietHours(time(22, 0), time(10, 0), PRAGUE)
    assert run(db, NINE, sender, quiet=late_mornings) == 1
    assert sender.sent == []
    queued = db.scalars(select(Notification)).one()
    assert queued.not_before == local(TODAY, 10)
    assert flush_due(db, sender, quiet=late_mornings, now=local(TODAY, 10)) == 1
    assert sender.sent[0].title == "🎂 Terka: birthday today"


def test_close_people_get_a_push_five_days_before(db: Session) -> None:
    soon = TODAY + timedelta(days=5)  # Tue 29 Sep
    pato = birthday(db, "Paťo", soon, "Close", year=2000)
    birthday(db, "Jano", soon)  # not close
    birthday(db, "Fero", TODAY + timedelta(days=4), "close")  # close, but not 5 days away
    sender = FakeSender()
    assert run(db, NINE, sender) == 1
    [push] = sender.sent
    assert push.title == "🎂 Paťo: birthday in 5 days"
    assert push.body == "Tue 29 Sep · Turns 26"
    assert keys(db) == {f"birthday:{pato.id}:2026-09-29:ahead"}
    # On the day itself, both Paťo and Jano get the usual push.
    assert run(db, local(soon, 9), sender) == 2
    assert {p.title for p in sender.sent[1:]} == {
        "🎂 Paťo: birthday today",
        "🎂 Jano: birthday today",
    }


def test_name_day_push(db: Session) -> None:
    pato = person(db, "Paťo", first_name="Patrik")  # SK: 6 Jul
    sender = FakeSender()
    assert run(db, local(date(2026, 7, 6), 9), sender) == 1
    [push] = sender.sent
    assert push.title == "🎉 Paťo: name day today"
    assert push.body == "Name day of Patrik"
    assert keys(db) == {f"nameday:{pato.id}:2026-07-06:day"}


def test_archived_people_get_no_reminders(db: Session) -> None:
    gone = birthday(db, "Old Friend", TODAY)
    gone.archived = True
    assert run(db, NINE) == 0


# --- Lent items ---


def test_lent_item_reminder_once_when_due(db: Session) -> None:
    pato = person(db, "Paťo")
    charger = lend(db, pato, "charger", days_ago=21)
    lend(db, pato, "book", days_ago=20)  # not yet
    lend(db, pato, "drill", days_ago=30, direction=DebtDirection.I_OWE_THEM)  # I borrowed it
    sender = FakeSender()
    assert run(db, NINE - timedelta(minutes=1), sender) == 0
    assert run(db, NINE, sender) == 1
    [push] = sender.sent
    assert push.title == "📦 Paťo still has your charger"
    assert push.body == "Lent 3 Sep (21 days ago). Time to ask for it back?"
    assert keys(db) == {f"lent_item:{charger.id}:2026-09-24"}
    assert run(db, NINE + timedelta(days=1), sender) == 1  # the book, a day later
    assert run(db, NINE + timedelta(days=2), sender) == 0


def test_item_reminder_days_and_settling(db: Session) -> None:
    pato = person(db, "Paťo")
    lend(db, pato, "tent", days_ago=5, reminder_days=5)
    settled = lend(db, pato, "charger", days_ago=40)
    ledger.settle(db, settled)
    assert run(db, NINE) == 1
    assert run(db, NINE, item_days=3) == 0


def test_moving_an_item_reminder_rearms_it(db: Session) -> None:
    pato = person(db, "Paťo")
    tent = lend(db, pato, "tent", days_ago=5, reminder_days=5)
    assert run(db, NINE) == 1
    ledger.update_debt(db, tent, ledger.DebtUpdate(reminder_days=7))
    assert run(db, NINE + timedelta(days=1)) == 0
    assert run(db, NINE + timedelta(days=2)) == 1


def test_old_items_are_reminded_once_even_long_after(db: Session) -> None:
    lend(db, person(db, "Paťo"), "charger", days_ago=90)
    assert run(db, NINE) == 1
    assert run(db, NINE + timedelta(days=3)) == 0


# --- Promises ---


def test_promise_push_on_the_due_date(db: Session) -> None:
    terka = person(db, "Terka")
    schedule = promise(db, terka, "send the schedule", TODAY)
    promise(db, terka, "return the drill", TODAY, PromiseDirection.THEY_PROMISED)  # theirs
    promise(db, terka, "call back", TODAY + timedelta(days=1))  # not yet
    sender = FakeSender()
    assert run(db, NINE - timedelta(minutes=1), sender) == 0
    assert run(db, NINE, sender) == 1
    [push] = sender.sent
    assert push.title == "✅ Due today: send the schedule"
    assert push.body == "You promised Terka on 10 Sep."
    assert keys(db) == {f"promise_due:{schedule.id}:2026-09-24"}
    assert run(db, NINE + timedelta(hours=5), sender) == 0


def test_overdue_promise_is_pushed_once(db: Session) -> None:
    terka = person(db, "Terka")
    late = promise(db, terka, "send the schedule", date(2026, 9, 20))
    done = promise(db, terka, "buy tickets", date(2026, 9, 20))
    promises.set_status(done, PromiseStatus.DONE)
    sender = FakeSender()
    assert run(db, NINE, sender) == 1
    assert sender.sent[0].title == "⏰ Overdue since 20 Sep: send the schedule"
    assert run(db, NINE + timedelta(days=1), sender) == 0
    # A new due date re-arms it.
    promises.update_promise(db, late, promises.PromiseUpdate(due=date(2026, 9, 26)))
    assert run(db, local(date(2026, 9, 26), 9), sender) == 1
    assert sender.sent[-1].title == "✅ Due today: send the schedule"


# --- The Sunday digest ---


def fill_week(db: Session) -> None:
    pato = person(db, "Paťo")
    terka = person(db, "Terka")
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=pato.id,
            direction=DebtDirection.THEY_OWE_ME,
            amount_minor=2400,
            description="pizza",
        ),
        "EUR",
    )
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=terka.id,
            direction=DebtDirection.I_OWE_THEM,
            amount_minor=1000,
            currency="CZK",
        ),
        "EUR",
    )
    lend(db, pato, "charger", days_ago=21)
    promise(db, terka, "send the schedule", date(2026, 9, 20))
    promise(db, pato, "return the drill", date(2026, 9, 30), PromiseDirection.THEY_PROMISED)
    birthday(db, "Jano", date(2026, 9, 29), year=2000)
    birthday(db, "Later", date(2026, 10, 5))  # after the coming week
    for day, title in ((date(2026, 9, 28), "kafe s Paťom"), (date(2026, 10, 5), "too late")):
        meeting = Meeting(
            calendar_uid=title,
            calendar_name="Personal",
            title=title,
            start_utc=local(day, 10),
            end_utc=local(day, 11),
            cancelled=False,
        )
        db.add(meeting)
        db.flush()
        meetings.link_person(db, meeting, pato.id)
    for created in (SUNDAY_SIX - timedelta(days=2), SUNDAY_SIX - timedelta(days=10)):
        db.add(
            Proposal(
                kind=ProposalKind.CAPTURE,
                status=ProposalStatus.PENDING,
                summary="x",
                payload_json={},
                source_type="capture",
                created_at=created,
            )
        )
    db.flush()


def test_digest_on_sunday_evening(db: Session) -> None:
    fill_week(db)
    sender = FakeSender()
    # Before 18:00 only the lent charger and the overdue promise are reminded.
    assert run(db, SUNDAY_SIX - timedelta(minutes=1), sender) == 2
    assert not any(k.startswith("digest") for k in keys(db))
    assert run(db, SUNDAY_SIX, sender) == 1
    push = sender.sent[-1]
    assert push.title == "🗓 Your week: 28 Sep to 4 Oct"
    assert push.click == f"{URL}/"
    assert push.body.split("\n\n") == [
        "📥 2 waiting in the Inbox, 1 new this week",
        "💶 You owe 10.00 Kč; owed to you 24.00 €",
        "⏰ Overdue promises (1)\n• you → Terka: send the schedule (20 Sep)",
        "✅ Promises due this week (1)\n• Paťo → you: return the drill (30 Sep)",
        "📦 Lent out (1)\n• charger → Paťo (since 3 Sep)",
        "Birthdays & name days\n• 🎂 Tue 29 Sep Jano (turns 26)",
        "📅 Meetings (1)\n• Mon 10:00 kafe s Paťom (with Paťo)",
    ]
    assert keys(db) >= {"digest:2026-09-27"}
    assert run(db, SUNDAY_SIX + timedelta(hours=1), sender) == 0
    assert run(db, SUNDAY_SIX + timedelta(days=1), sender) == 0  # Monday: none
    assert len([p for p in sender.sent if p.title.startswith("🗓")]) == 1


def test_empty_digest(db: Session) -> None:
    sender = FakeSender()
    assert run(db, SUNDAY_SIX, sender) == 1
    assert sender.sent[0].body == "Nothing open and nothing planned. A quiet week."


def test_digest_waits_for_quiet_hours_to_end(db: Session) -> None:
    evenings = QuietHours(time(17, 0), time(9, 0), PRAGUE)
    sender = FakeSender()
    assert run(db, SUNDAY_SIX, sender, quiet=evenings) == 1
    assert sender.sent == []
    queued = db.scalars(select(Notification)).one()
    assert queued.not_before == local(date(2026, 9, 28), 9)


def test_long_digest_lists_are_cut(db: Session) -> None:
    pato = person(db, "Paťo")
    for n in range(8):
        lend(db, pato, f"thing {n}", days_ago=1)
    body = reminders.digest_push(reminders.build_digest(db, SUNDAY_SIX, PRAGUE), URL, PRAGUE).body
    assert body.startswith("📦 Lent out (8)\n• thing 0 → Paťo")
    assert body.endswith("• thing 4 → Paťo (since 23 Sep)\n• … and 3 more")


@pytest.mark.parametrize("hour", [8, 12, 17])
def test_no_digest_on_other_days_or_earlier(db: Session, hour: int) -> None:
    assert run(db, local(date(2026, 9, 27), hour)) == 0
    assert run(db, local(date(2026, 9, 26), 19)) == 0


# --- Home page helpers ---


def test_due_items_and_promises(db: Session) -> None:
    pato = person(db, "Paťo")
    charger = lend(db, pato, "charger", days_ago=30)
    lend(db, pato, "book", days_ago=2)
    mine = promise(db, pato, "send photos", TODAY)
    theirs = promise(db, pato, "pay back", date(2026, 9, 1), PromiseDirection.THEY_PROMISED)
    assert reminders.due_items(db, TODAY, 21, PRAGUE) == [charger]
    assert reminders.due_promises(db, TODAY) == [mine]
    assert reminders.overdue_promises(db, TODAY) == [theirs]
