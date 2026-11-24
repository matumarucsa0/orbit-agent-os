from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.enums import HandleType
from orbit.domain.people import Handle, Person
from orbit.integrations.carddav import RawCard
from orbit.integrations.vcard import Birthday, LabeledValue, VCard
from orbit.services import people
from orbit.services.contacts_import import (
    ImportReport,
    import_from_source,
    import_vcard_text,
    import_vcards,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "vcard" / "apple_contacts.vcf").read_text(
    encoding="utf-8"
)


def by_uid(db: Session, suffix: str) -> Person:
    person = db.scalar(select(Person).where(Person.vcard_uid.like(f"%{suffix}")))
    assert person is not None
    return person


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Person)) or 0


def handles(person: Person) -> dict[HandleType, list[str]]:
    out: dict[HandleType, list[str]] = {}
    for h in person.handles:
        out.setdefault(h.type, []).append(h.value_norm)
    return out


def test_first_import(db: Session) -> None:
    report = import_vcard_text(db, FIXTURE)
    db.commit()
    assert (report.created, report.updated, report.unchanged, report.skipped) == (7, 0, 0, 0)
    assert report.conflicts == []
    assert count(db) == 7

    pato = by_uid(db, "001")
    assert (pato.display_name, pato.first_name, pato.last_name) == (
        "Patrik Veselý",
        "Patrik",
        "Veselý",
    )
    assert (pato.birthday_day, pato.birthday_month, pato.birthday_year) == (17, 3, 1994)
    assert (pato.city, pato.country, pato.nameday_country) == ("Košice", "SK", "SK")
    assert [a.value for a in pato.aliases] == ["Paťo"]  # "Pato" is the same alias normalized
    assert handles(pato) == {
        HandleType.PHONE: ["+421905123456"],
        HandleType.EMAIL: ["patrik.vesely@example.com"],
        HandleType.INSTAGRAM: ["pato.vesely"],
    }
    assert [h.label for h in pato.handles if h.type is HandleType.PHONE] == ["mobile"]
    assert [n.text for n in pato.notes] == ["Met at Uni, loves climbing.\nHas a dog."]

    terka = by_uid(db, "002")
    assert handles(terka)[HandleType.PHONE] == ["+421905111222"]  # no prefix -> SK default
    assert handles(terka)[HandleType.TELEGRAM] == ["terka_n"]
    assert (terka.birthday_month, terka.birthday_day, terka.birthday_year) == (11, 2, None)

    jiri = by_uid(db, "003")
    assert jiri.country == "CZ"  # from the address
    assert handles(jiri)[HandleType.PHONE] == ["+420601234567"]

    assert by_uid(db, "004").display_name == "Autoservis Kováč"
    assert handles(by_uid(db, "005"))[HandleType.LINKEDIN] == ["martin-horvath-123"]
    assert by_uid(db, "007").display_name == "someone@example.org"


def test_second_import_is_a_no_op(db: Session) -> None:
    import_vcard_text(db, FIXTURE)
    db.commit()
    report = import_vcard_text(db, FIXTURE)
    db.commit()
    assert (report.created, report.updated, report.unchanged) == (0, 0, 7)
    assert count(db) == 7
    assert db.scalar(select(func.count()).select_from(Handle)) == 12


def test_edits_in_orbit_are_never_overwritten(db: Session) -> None:
    import_vcard_text(db, FIXTURE)
    pato = by_uid(db, "001")
    people.update_person(db, pato, people.PersonUpdate(display_name="Paťo", city="Praha"))
    db.commit()

    report = import_vcard_text(db, FIXTURE)
    db.commit()
    assert (report.created, report.updated) == (0, 0)
    assert (pato.display_name, pato.city) == ("Paťo", "Praha")


def test_matches_existing_person_by_phone_in_another_format(db: Session) -> None:
    pato = people.create_person(db, people.PersonCreate(display_name="Paťo", country="SK"))
    people.add_handle(db, pato, HandleType.PHONE, "0905123456")
    people.add_note(db, pato, "Met at Uni, loves climbing.\nHas a dog.")
    db.commit()

    report = import_vcard_text(db, FIXTURE)
    db.commit()

    assert report.created == 6 and report.updated == 1
    assert pato.vcard_uid is not None and pato.vcard_uid.endswith("001")
    assert pato.display_name == "Paťo"  # kept
    assert pato.last_name == "Veselý"  # filled
    assert len(pato.notes) == 1  # identical note not duplicated


def test_matches_manually_added_person_by_unique_name(db: Session) -> None:
    people.create_person(db, people.PersonCreate(display_name="Martin Horváth"))
    db.commit()
    report = import_vcard_text(db, FIXTURE)
    assert (report.created, report.updated) == (6, 1)


def test_name_match_skipped_when_ambiguous(db: Session) -> None:
    for _ in range(2):
        people.create_person(db, people.PersonCreate(display_name="Martin Horváth"))
    db.commit()
    report = import_vcard_text(db, FIXTURE)
    assert report.created == 7


def test_phone_and_email_on_different_people_is_a_conflict(db: Session) -> None:
    a = people.create_person(db, people.PersonCreate(display_name="A", country="SK"))
    b = people.create_person(db, people.PersonCreate(display_name="B"))
    people.add_handle(db, a, HandleType.PHONE, "+421905123456")
    people.add_handle(db, b, HandleType.EMAIL, "patrik.vesely@example.com")
    db.commit()

    report = import_vcard_text(db, FIXTURE)
    assert report.created == 6
    assert len(report.conflicts) == 1
    assert "Patrik Veselý" in report.conflicts[0]


def test_two_contacts_sharing_a_phone(db: Session) -> None:
    cards = [
        VCard(uid="u1", formatted_name="Home Landline", phones=[LabeledValue("+421 41 555 1234")]),
        VCard(uid="u2", formatted_name="Other Person", phones=[LabeledValue("+421 41 555 1234")]),
    ]
    report = import_vcards(db, cards)
    # Both people are imported; only the shared number is left off the second one.
    assert report.created == 2
    assert report.conflicts == [
        "Other Person: phone +421 41 555 1234 already belongs to Home Landline."
    ]
    assert handles(by_uid(db, "u1")) == {HandleType.PHONE: ["+421415551234"]}
    assert by_uid(db, "u2").handles == []
    # Re-running is stable: nothing new, the conflict is reported again.
    again = import_vcards(db, cards)
    assert (again.created, again.updated, again.unchanged) == (0, 0, 2)
    assert len(again.conflicts) == 1


def test_empty_card_skipped(db: Session) -> None:
    report = import_vcards(db, [VCard(uid="x")])
    assert report.skipped == 1 and report.created == 0


def test_bad_birthday_does_not_block_import(db: Session) -> None:
    card = VCard(uid="b", formatted_name="Odd", given_name="Odd", birthday=Birthday(2, 30))
    report = import_vcards(db, [card])
    assert report.created == 1
    person = by_uid(db, "b")
    assert person.birthday_month is None and person.first_name == "Odd"
    # The rejected birthday is not reported as an update on every later run.
    again = import_vcards(db, [card])
    assert (again.updated, again.unchanged) == (0, 1)


class FakeSource:
    """CardDAV returns one vCard per resource."""

    def fetch_cards(self) -> list[RawCard]:
        return [RawCard(chunk + "END:VCARD\n") for chunk in FIXTURE.split("END:VCARD\n")[:2]]


def test_import_from_source(db: Session) -> None:
    report = import_from_source(db, FakeSource())
    assert report.created == 2


def test_summary_text() -> None:
    report = ImportReport(created=3, updated=1, unchanged=10, skipped=1, conflicts=["x"])
    assert report.summary == "3 new, 1 updated, 10 unchanged, 1 skipped, 1 conflicts"


@pytest.mark.parametrize("default_country", ["CZ"])
def test_default_country_parameter(db: Session, default_country: str) -> None:
    card = VCard(uid="c", formatted_name="Prague Friend", phones=[LabeledValue("601 234 567")])
    import_vcards(db, [card], default_country=default_country)
    person = by_uid(db, "c")
    assert person.country == "CZ"
    assert handles(person)[HandleType.PHONE] == ["+420601234567"]
