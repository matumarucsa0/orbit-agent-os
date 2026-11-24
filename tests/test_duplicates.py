from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, DebtType, HandleType, PromiseDirection
from orbit.domain.people import Person, interaction_person, not_duplicate
from orbit.services import duplicates, interactions, ledger, people, promises
from orbit.services.errors import InvalidInputError, NotFoundError


def make(db: Session, name: str, **fields: object) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name, **fields))


def pairs(db: Session) -> set[tuple[str, str, str]]:
    return {
        (*sorted((c.a.display_name, c.b.display_name)), c.reason)  # type: ignore[misc]
        for c in duplicates.find_candidates(db)
    }


def test_same_normalized_name(db: Session) -> None:
    make(db, "Terézia Nováková")
    make(db, "terezia novakova")
    assert pairs(db) == {("Terézia Nováková", "terezia novakova", "Same name")}


def test_instagram_only_person_matches_alias(db: Session) -> None:
    pato = make(db, "Patrik Veselý", first_name="Patrik", last_name="Veselý")
    people.add_alias(db, pato, "Paťo")
    make(db, "pato")
    assert pairs(db) == {("Patrik Veselý", "pato", "Name matches an alias")}


def test_first_name_match_only_when_unique(db: Session) -> None:
    make(db, "Martin Horváth")
    make(db, "martin")
    assert pairs(db) == {("Martin Horváth", "martin", "Name matches the first name")}
    make(db, "Martin Kováč")
    assert pairs(db) == set()  # "martin" could be either


def test_same_last_name_similar_first(db: Session) -> None:
    make(db, "Tomáš Kráľ")
    make(db, "Tom Kráľ")
    make(db, "Tomas Kral")
    found = pairs(db)
    assert ("Tom Kráľ", "Tomáš Kráľ", "Similar first name") in found
    assert ("Tomas Kral", "Tomáš Kráľ", "Same name") in found


def test_different_people_same_last_name_not_flagged(db: Session) -> None:
    make(db, "Jana Nováková")
    make(db, "Terézia Nováková")
    assert pairs(db) == set()


def test_archived_excluded(db: Session) -> None:
    make(db, "Pato")
    make(db, "pato", archived=True)
    assert pairs(db) == set()


def test_dismiss(db: Session) -> None:
    a = make(db, "Martin Horváth")
    b = make(db, "Martin Horváth")
    duplicates.dismiss(db, b.id, a.id)
    duplicates.dismiss(db, a.id, b.id)  # idempotent
    assert pairs(db) == set()
    assert db.scalar(select(func.count()).select_from(not_duplicate)) == 1
    with pytest.raises(InvalidInputError):
        duplicates.dismiss(db, a.id, a.id)
    with pytest.raises(NotFoundError):
        duplicates.dismiss(db, a.id, 999)


def test_merge_moves_everything(db: Session) -> None:
    keep = make(db, "Patrik Veselý", country="SK")
    other = make(
        db,
        "pato",
        first_name="Patrik",
        last_name="Veselý",
        city="Košice",
        birthday_month=3,
        birthday_day=17,
    )
    terka = make(db, "Terka", met_through_id=other.id)
    people.add_alias(db, keep, "Paťo")
    people.add_alias(db, other, "PATO")  # same as keep's alias once normalized
    people.add_alias(db, other, "Veselko")
    people.add_handle(db, keep, HandleType.PHONE, "+421905123456")
    people.add_handle(db, other, HandleType.INSTAGRAM, "@pato")
    people.add_note(db, other, "from the IG person")
    people.add_tag(db, keep, "Uni")
    people.add_tag(db, other, "uni")
    people.add_tag(db, other, "gym")
    both = interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 1), person_ids=[keep.id, other.id]
        ),
    )
    only_other = interactions.create_interaction(
        db, interactions.InteractionCreate(occurred_on=date(2026, 9, 2), person_ids=[other.id])
    )
    duplicates.dismiss(db, other.id, terka.id)
    db.commit()
    other_id = other.id

    merged = duplicates.merge(db, keep.id, other_id)
    db.commit()

    assert merged is keep
    assert db.get(Person, other_id) is None
    assert (keep.display_name, keep.first_name, keep.last_name, keep.city) == (
        "Patrik Veselý",
        "Patrik",
        "Veselý",
        "Košice",
    )
    assert (keep.birthday_month, keep.birthday_day) == (3, 17)
    assert keep.search_name_norm == "patrik vesely patrik vesely"
    # "pato" (the merged name) and "PATO" both equal the existing alias "Paťo" normalized.
    assert sorted(a.value for a in keep.aliases) == ["Paťo", "Veselko"]
    assert sorted(h.value_norm for h in keep.handles) == ["+421905123456", "pato"]
    assert [n.text for n in keep.notes] == ["from the IG person"]
    assert sorted(t.name for t in keep.tags) == ["Uni", "gym"]
    db.refresh(terka)
    assert terka.met_through_id == keep.id
    links = db.execute(select(interaction_person)).all()
    assert sorted((row.interaction_id, row.person_id) for row in links) == [
        (both.id, keep.id),
        (only_other.id, keep.id),
    ]
    assert db.scalar(select(func.count()).select_from(not_duplicate)) == 0


def test_merge_keeps_existing_fields_and_moves_vcard_uid(db: Session) -> None:
    keep = make(db, "Pato", city="Praha")
    other = make(db, "Patrik", city="Košice")
    other.vcard_uid = "uid-1"
    db.commit()
    duplicates.merge(db, keep.id, other.id)
    db.commit()
    assert keep.city == "Praha"
    assert keep.vcard_uid == "uid-1"


def test_merge_validation(db: Session) -> None:
    a = make(db, "A")
    with pytest.raises(InvalidInputError):
        duplicates.merge(db, a.id, a.id)
    with pytest.raises(NotFoundError):
        duplicates.merge(db, a.id, 999)


def test_merge_person_introduced_by_the_other(db: Session) -> None:
    other = make(db, "Other")
    keep = make(db, "Keep", met_through_id=other.id)
    db.commit()
    duplicates.merge(db, keep.id, other.id)
    db.commit()
    assert keep.met_through_id is None


def test_merge_moves_debts_and_promises(db: Session) -> None:
    keep, other = make(db, "Patrik Veselý"), make(db, "pato")
    mine = ledger.create_debt(
        db,
        ledger.DebtCreate(person_id=keep.id, direction=DebtDirection.THEY_OWE_ME, amount_minor=500),
        "EUR",
    )
    pizza = ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=other.id, direction=DebtDirection.THEY_OWE_ME, amount_minor=1200
        ),
        "EUR",
    )
    rest = ledger.settle(db, pizza, 500).remainder
    assert rest is not None
    charger = ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=other.id,
            direction=DebtDirection.THEY_OWE_ME,
            type=DebtType.ITEM,
            description="charger",
        ),
        "EUR",
    )
    promise = promises.create_promise(
        db,
        promises.PromiseCreate(
            person_id=other.id, direction=PromiseDirection.I_PROMISED, text="send photos"
        ),
        date(2026, 9, 23),
    )
    db.commit()

    duplicates.merge(db, keep.id, other.id)
    db.commit()

    assert {d.id for d in keep.debts} == {mine.id, pizza.id, rest.id, charger.id}
    assert all(d.person_id == keep.id for d in keep.debts)
    assert rest.parent is pizza  # partial-settlement chain survives
    assert [p.id for p in keep.promises] == [promise.id]
    assert {(b.currency, b.amount_minor) for b in ledger.balances(db, keep.id)} == {("EUR", 1200)}
