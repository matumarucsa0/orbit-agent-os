from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection, PromiseStatus
from orbit.domain.ledger import Debt, Promise
from orbit.domain.people import Person
from orbit.services import ledger, people, promises
from orbit.services.ics import build_calendar, escape_text, fold
from orbit.services.ledger import DebtCreate
from orbit.services.promises import PromiseCreate, PromiseUpdate
from orbit.settings import Settings


def make(db: Session, name: str, **fields: object) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name, **fields))


PRAGUE = ZoneInfo("Europe/Prague")


def feed(db: Session) -> str:
    return build_calendar(db, 21, PRAGUE)


def unfold(text: str) -> list[str]:
    """Minimal RFC 5545 reader: undo folding, split into content lines."""
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")  # CRLF only
    return text.replace("\r\n ", "").split("\r\n")[:-1]


def events(text: str) -> dict[str, dict[str, str]]:
    """UID -> {property name (with params): value}."""
    found: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            assert current is not None
            assert current["UID"] not in found, f"duplicate UID {current['UID']}"
            found[current["UID"]] = current
            current = None
        elif current is not None:
            name, _, value = line.partition(":")
            current[name] = value
    return found


# --- Encoding ---


def test_escape_text() -> None:
    assert escape_text("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"


def test_fold_respects_octets_and_utf8() -> None:
    line = "SUMMARY:" + "🎂 Ľubomír Šťastný " * 10
    folded = fold(line)
    parts = folded.split("\r\n")
    assert len(parts) > 1
    for i, part in enumerate(parts):
        assert len(part.encode("utf-8")) <= 75
        if i:
            assert part.startswith(" ")
    assert folded.replace("\r\n ", "") == line  # no character was split or lost


def test_short_line_not_folded() -> None:
    assert fold("VERSION:2.0") == "VERSION:2.0"


# --- Calendar content ---


def test_calendar_structure_and_events(db: Session) -> None:
    pato = make(
        db,
        "Patrik Veselý",
        first_name="Patrik",
        birthday_month=3,
        birthday_day=17,
        birthday_year=1994,
    )
    terka = make(db, "Terka, Nováková", first_name="Terézia", birthday_month=11, birthday_day=2)
    make(db, "No Dates Here", nameday_country=None)
    db.commit()

    text = feed(db)
    lines = unfold(text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in lines and "X-WR-CALNAME:Orbit" in lines

    found = events(text)
    assert set(found) == {
        f"birthday-{pato.id}@orbit",
        f"nameday-{pato.id}-0706@orbit",
        f"birthday-{terka.id}@orbit",
        f"nameday-{terka.id}-1015@orbit",
    }
    birthday = found[f"birthday-{pato.id}@orbit"]
    assert birthday["DTSTART;VALUE=DATE"] == "19940317"
    assert birthday["DTEND;VALUE=DATE"] == "19940318"
    assert birthday["RRULE"] == "FREQ=YEARLY"
    assert birthday["SUMMARY"] == "🎂 Patrik Veselý"
    assert birthday["DESCRIPTION"] == "Born 1994"
    assert birthday["TRANSP"] == "TRANSPARENT"
    assert birthday["DTSTAMP"].endswith("Z")

    nameday = found[f"nameday-{pato.id}-0706@orbit"]
    assert nameday["DTSTART;VALUE=DATE"] == "20000706"
    assert nameday["SUMMARY"] == "🎉 Patrik Veselý (name day)"
    assert nameday["DESCRIPTION"] == "Patrik · SK name day"

    # No birth year: fixed start year; commas in names are escaped.
    terka_birthday = found[f"birthday-{terka.id}@orbit"]
    assert terka_birthday["DTSTART;VALUE=DATE"] == "20001102"
    assert terka_birthday["SUMMARY"] == "🎂 Terka\\, Nováková"


def test_feb_29_uses_last_day_of_february(db: Session) -> None:
    leap = make(db, "Leap", birthday_month=2, birthday_day=29, birthday_year=2004)
    horymir = make(db, "Horymír Dvořák", nameday_country="CZ")
    found = events(feed(db))
    assert found[f"birthday-{leap.id}@orbit"]["RRULE"] == "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-1"
    assert found[f"birthday-{leap.id}@orbit"]["DTSTART;VALUE=DATE"] == "20040229"
    assert found[f"nameday-{horymir.id}-0229@orbit"]["DTSTART;VALUE=DATE"] == "20000229"


def test_accent_variants_on_one_day_give_one_event(db: Session) -> None:
    simon = make(db, "Šimon Kráľ")
    assert list(events(feed(db))) == [f"nameday-{simon.id}-1030@orbit"]


def test_edit_keeps_uid_and_moves_event(db: Session) -> None:
    person = make(db, "Pato", birthday_month=3, birthday_day=17)
    db.commit()
    before = events(feed(db))
    people.update_person(db, person, people.PersonUpdate(birthday_month=4, birthday_day=1))
    db.commit()
    after = events(feed(db))
    uid = f"birthday-{person.id}@orbit"
    assert set(before) == set(after)  # same UIDs: updated, not duplicated
    assert after[uid]["DTSTART;VALUE=DATE"] == "20000401"
    assert after[uid]["DTSTAMP"] >= before[uid]["DTSTAMP"]


def test_output_is_stable_between_fetches(db: Session) -> None:
    make(db, "Pato", first_name="Patrik", birthday_month=3, birthday_day=17)
    db.commit()
    assert feed(db) == feed(db)


def test_archived_and_deleted_people_are_left_out(db: Session) -> None:
    archived = make(db, "Old", birthday_month=1, birthday_day=5, archived=True)
    gone = make(db, "Gone", birthday_month=1, birthday_day=6)
    people.delete_person(db, gone)
    db.commit()
    text = feed(db)
    assert f"birthday-{archived.id}@orbit" not in text
    assert "Gone" not in text


def test_empty_calendar_is_valid(db: Session) -> None:
    assert unfold(feed(db))[-1] == "END:VCALENDAR"


# --- Promises and lent items ---


def _promise(
    db: Session, person: Person, text: str, direction: PromiseDirection, due: date
) -> Promise:
    data = PromiseCreate(person_id=person.id, direction=direction, text=text, due=due)
    return promises.create_promise(db, data, date(2026, 9, 23))


def _item(
    db: Session, person: Person, what: str, direction: DebtDirection, **extra: object
) -> Debt:
    data = DebtCreate(
        person_id=person.id,
        direction=direction,
        type=DebtType.ITEM,
        description=what,
        **extra,  # type: ignore[arg-type]
    )
    debt = ledger.create_debt(db, data, "EUR")
    debt.created_at = datetime(2026, 10, 2, 23, 30, tzinfo=UTC)  # 3 Oct in Prague
    return debt


def test_open_promises_on_their_due_date(db: Session) -> None:
    pato, terka = make(db, "Pato", nameday_country=None), make(db, "Terka", nameday_country=None)
    mine = _promise(db, pato, "send the photos", PromiseDirection.I_PROMISED, date(2026, 10, 1))
    theirs = _promise(
        db, terka, "return the book", PromiseDirection.THEY_PROMISED, date(2026, 10, 5)
    )
    done = _promise(db, pato, "done already", PromiseDirection.I_PROMISED, date(2026, 10, 2))
    promises.update_promise(db, done, PromiseUpdate(status=PromiseStatus.DONE))
    undated = _promise(db, pato, "someday", PromiseDirection.I_PROMISED, date(2026, 10, 3))
    promises.update_promise(db, undated, PromiseUpdate(due=None))
    db.commit()

    found = events(feed(db))
    assert set(found) == {f"promise-{mine.id}@orbit", f"promise-{theirs.id}@orbit"}
    event = found[f"promise-{mine.id}@orbit"]
    assert event["DTSTART;VALUE=DATE"] == "20261001"
    assert event["DTEND;VALUE=DATE"] == "20261002"
    assert "RRULE" not in event
    assert event["SUMMARY"] == "✅ Promised Pato: send the photos"
    assert found[f"promise-{theirs.id}@orbit"]["SUMMARY"] == "✅ Terka promised: return the book"


def test_lent_items_on_their_reminder_date(db: Session) -> None:
    pato = make(db, "Pato", nameday_country=None)
    charger = _item(db, pato, "charger", DebtDirection.THEY_OWE_ME)
    book = _item(db, pato, "book", DebtDirection.THEY_OWE_ME, reminder_days=60)
    _item(db, pato, "drill", DebtDirection.I_OWE_THEM)  # borrowed, not lent: no reminder
    returned = _item(db, pato, "umbrella", DebtDirection.THEY_OWE_ME)
    ledger.settle(db, returned)
    db.commit()

    found = events(feed(db))
    assert set(found) == {f"item-{charger.id}@orbit", f"item-{book.id}@orbit"}
    event = found[f"item-{charger.id}@orbit"]
    assert event["DTSTART;VALUE=DATE"] == "20261024"  # 3 Oct + 21 days
    assert event["SUMMARY"] == "📦 Pato has your charger"
    assert event["DESCRIPTION"] == "Lent 3 Oct 2026"
    assert "RRULE" not in event
    assert found[f"item-{book.id}@orbit"]["DTSTART;VALUE=DATE"] == "20261202"


def test_item_reminder_days_come_from_the_caller(db: Session) -> None:
    charger = _item(
        db, make(db, "Pato", nameday_country=None), "charger", DebtDirection.THEY_OWE_ME
    )
    db.commit()
    found = events(build_calendar(db, 7, PRAGUE))
    assert found[f"item-{charger.id}@orbit"]["DTSTART;VALUE=DATE"] == "20261010"


def test_editing_a_promise_updates_its_event(db: Session) -> None:
    p = _promise(db, make(db, "Pato"), "photos", PromiseDirection.I_PROMISED, date(2026, 10, 1))
    db.commit()
    promises.update_promise(db, p, PromiseUpdate(due=date(2026, 10, 8)))
    db.commit()
    found = events(feed(db))
    assert found[f"promise-{p.id}@orbit"]["DTSTART;VALUE=DATE"] == "20261008"


# --- HTTP ---


def test_feed_endpoint(client: TestClient, settings: Settings) -> None:
    token = settings.ics_token.get_secret_value()
    response = client.get(f"/calendar/{token}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.text.startswith("BEGIN:VCALENDAR\r\n")


def test_feed_wrong_token_is_404(client: TestClient) -> None:
    assert client.get("/calendar/wrong-token.ics").status_code == 404
    assert client.get("/calendar/.ics").status_code == 404
