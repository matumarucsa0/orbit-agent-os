from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    DebtDirection,
    HandleType,
    InteractionKind,
    PromiseDirection,
    PromiseStatus,
)
from orbit.domain.ledger import Debt, Promise
from orbit.domain.people import Alias, Handle, Interaction, Note, Person, Tag, person_tag
from orbit.services import interactions, ledger, people, promises
from orbit.services.errors import HandleConflictError, InvalidInputError, NotFoundError


def make(db: Session, name: str, **fields: object) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name, **fields))


def test_create_name_only(db: Session) -> None:
    person = make(db, "  @someone ")
    db.commit()
    assert person.id is not None
    assert person.display_name == "@someone"
    assert person.nameday_country == "SK"  # default
    assert person.archived is False
    assert person.search_name_norm == "@someone"


def test_create_full_and_search_name(db: Session) -> None:
    person = make(
        db,
        "Paťo",
        first_name="Patrik",
        last_name="Veselý",
        birthday_month=3,
        birthday_day=17,
        country="sk",
        city="",
    )
    assert person.search_name_norm == "pato patrik vesely"
    assert person.country == "SK"
    assert person.city is None  # blank -> None


@pytest.mark.parametrize(
    "fields",
    [
        {"display_name": ""},
        {"display_name": "X", "birthday_month": 13, "birthday_day": 1},
        {"display_name": "X", "country": "SVK"},
        {"display_name": "X", "nameday_country": "PL"},
    ],
)
def test_create_validation(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        people.PersonCreate(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields",
    [
        {"birthday_month": 3},
        {"birthday_year": 1990},
        {"birthday_month": 2, "birthday_day": 30},
        {"birthday_month": 2, "birthday_day": 29, "birthday_year": 2023},
    ],
)
def test_birthday_rules(db: Session, fields: dict[str, int]) -> None:
    with pytest.raises(InvalidInputError):
        make(db, "X", **fields)


def test_feb_29_without_year_is_fine(db: Session) -> None:
    assert make(db, "Leap", birthday_month=2, birthday_day=29).birthday_day == 29


def test_update_is_partial(db: Session) -> None:
    person = make(db, "Pato", city="Košice", first_name="Patrik")
    people.update_person(db, person, people.PersonUpdate(last_name="Veselý"))
    assert (person.first_name, person.last_name, person.city) == ("Patrik", "Veselý", "Košice")
    people.update_person(db, person, people.PersonUpdate(city=None))
    assert person.city is None
    with pytest.raises(InvalidInputError):
        people.update_person(db, person, people.PersonUpdate(display_name=None))


def test_met_through(db: Session) -> None:
    a = make(db, "A")
    b = make(db, "B", met_through_id=a.id)
    assert b.met_through is a
    with pytest.raises(InvalidInputError):
        people.update_person(db, a, people.PersonUpdate(met_through_id=a.id))
    with pytest.raises(NotFoundError):
        people.update_person(db, a, people.PersonUpdate(met_through_id=999))
    people.delete_person(db, a)
    db.commit()
    db.refresh(b)
    assert b.met_through_id is None


def test_handles_normalized_and_unique(db: Session) -> None:
    pato = make(db, "Pato", country="SK")
    terka = make(db, "Terka")
    handle = people.add_handle(db, pato, HandleType.PHONE, "0905 123 456", "mobile")
    assert handle.value_norm == "+421905123456"
    # Same number written differently -> same handle, returned as-is.
    assert people.add_handle(db, pato, HandleType.PHONE, "+421905123456") is handle
    with pytest.raises(HandleConflictError) as excinfo:
        people.add_handle(db, terka, HandleType.PHONE, "+421 905 123 456")
    assert excinfo.value.owner_id == pato.id
    with pytest.raises(InvalidInputError):
        people.add_handle(db, terka, HandleType.INSTAGRAM, "@")
    # Same value, different type is fine.
    people.add_handle(db, terka, HandleType.WHATSAPP, "+421 905 123 456")


def test_country_change_renormalizes_phones(db: Session) -> None:
    person = make(db, "Jiří")
    handle = people.add_handle(db, person, HandleType.PHONE, "601 234 567")
    assert handle.value_norm == "601234567"  # no region yet
    people.update_person(db, person, people.PersonUpdate(country="CZ"))
    assert handle.value_norm == "+420601234567"


def test_remove_handle_and_find_by_handle(db: Session) -> None:
    person = make(db, "Pato")
    handle = people.add_handle(db, person, HandleType.INSTAGRAM, "@pato")
    found = people.find_person_by_handle(db, HandleType.INSTAGRAM, "instagram.com/pato", None)
    assert found is person
    people.remove_handle(db, person, handle.id)
    assert person.handles == []
    with pytest.raises(NotFoundError):
        people.remove_handle(db, person, handle.id)


def test_aliases(db: Session) -> None:
    person = make(db, "Patrik")
    alias = people.add_alias(db, person, " Paťo ")
    assert (alias.value, alias.value_norm) == ("Paťo", "pato")
    assert people.add_alias(db, person, "PATO") is alias
    people.remove_alias(db, person, alias.id)
    assert person.aliases == []
    with pytest.raises(InvalidInputError):
        people.add_alias(db, person, "  ")


def test_tags_shared_and_counted(db: Session) -> None:
    a, b = make(db, "A"), make(db, "B")
    gym = people.add_tag(db, a, "Gym")
    assert people.add_tag(db, b, " gym ") is gym
    people.add_tag(db, a, "Košice")
    people.update_person(db, b, people.PersonUpdate(archived=True))
    counts = {t.name: n for t, n in people.list_tags(db)}
    assert counts == {"Gym": 1, "Košice": 1}
    people.remove_tag(db, a, gym.id)
    assert [t.name for t in a.tags] == ["Košice"]


def test_notes(db: Session) -> None:
    person = make(db, "Pato")
    note = people.add_note(db, person, "  Loves climbing  ")
    assert (note.text, note.text_norm) == ("Loves climbing", "loves climbing")
    people.update_note(db, note, "Loves bouldering")
    assert people.get_note(db, person, note.id).text_norm == "loves bouldering"
    other = make(db, "Other")
    with pytest.raises(NotFoundError):
        people.get_note(db, other, note.id)
    people.delete_note(db, note)
    assert person.notes == []


def test_delete_cascades_and_drops_orphan_interactions(db: Session) -> None:
    pato, terka = make(db, "Pato"), make(db, "Terka")
    people.add_handle(db, pato, HandleType.EMAIL, "pato@example.com")
    people.add_alias(db, pato, "Paťo")
    people.add_note(db, pato, "note")
    people.add_tag(db, pato, "gym")
    solo = interactions.create_interaction(
        db, interactions.InteractionCreate(occurred_on=date(2026, 9, 1), person_ids=[pato.id])
    )
    shared = interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 2), person_ids=[pato.id, terka.id]
        ),
    )
    db.commit()
    solo_id, shared_id = solo.id, shared.id

    people.delete_person(db, pato)
    db.commit()

    for model in (Handle, Alias, Note):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    assert db.scalar(select(func.count()).select_from(person_tag)) == 0
    assert db.scalar(select(func.count()).select_from(Tag)) == 1  # tags themselves stay
    assert db.get(Interaction, solo_id) is None
    remaining = db.get(Interaction, shared_id)
    assert remaining is not None
    assert [p.display_name for p in remaining.people] == ["Terka"]


def test_interactions_crud_and_last(db: Session) -> None:
    pato, terka = make(db, "Pato"), make(db, "Terka")
    first = interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 8, 1), kind=InteractionKind.CALL, person_ids=[pato.id]
        ),
    )
    latest = interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 1), summary="kafe", person_ids=[pato.id, terka.id, pato.id]
        ),
    )
    assert len(latest.people) == 2
    assert interactions.last_interaction(db, pato.id) is latest
    assert interactions.list_interactions(db, person_id=terka.id) == [latest]

    interactions.update_interaction(
        db, first, interactions.InteractionUpdate(occurred_on=date(2026, 9, 5), summary="")
    )
    assert first.summary is None
    assert interactions.last_interaction(db, pato.id) is first

    with pytest.raises(InvalidInputError):
        interactions.update_interaction(db, first, interactions.InteractionUpdate(person_ids=[]))
    with pytest.raises(NotFoundError):
        interactions.create_interaction(
            db, interactions.InteractionCreate(occurred_on=date(2026, 9, 1), person_ids=[999])
        )
    interactions.delete_interaction(db, first)
    with pytest.raises(NotFoundError):
        interactions.get_interaction(db, first.id)


def _debt(db: Session, person: Person, amount_minor: int) -> Debt:
    return ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=person.id, direction=DebtDirection.THEY_OWE_ME, amount_minor=amount_minor
        ),
        "EUR",
    )


def _promise(db: Session, person: Person) -> Promise:
    return promises.create_promise(
        db,
        promises.PromiseCreate(
            person_id=person.id, direction=PromiseDirection.I_PROMISED, text="call back"
        ),
        date(2026, 9, 23),
    )


def test_delete_refused_while_a_debt_is_open(db: Session) -> None:
    pato = make(db, "Pato")
    debt = _debt(db, pato, 1200)
    with pytest.raises(InvalidInputError, match="open debts or promises"):
        people.delete_person(db, pato)
    ledger.settle(db, debt)
    people.delete_person(db, pato)  # settled history goes with the person
    db.commit()
    assert db.scalar(select(func.count()).select_from(Debt)) == 0


def test_delete_refused_while_a_promise_is_open(db: Session) -> None:
    pato = make(db, "Pato")
    promise = _promise(db, pato)
    with pytest.raises(InvalidInputError, match="open debts or promises"):
        people.delete_person(db, pato)
    promises.update_promise(db, promise, promises.PromiseUpdate(status=PromiseStatus.DROPPED))
    people.delete_person(db, pato)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Promise)) == 0
