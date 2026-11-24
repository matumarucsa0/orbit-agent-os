from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from orbit.domain.enums import HandleType
from orbit.services import people
from orbit.services.search import search_people


@pytest.fixture
def seeded(db: Session) -> Session:
    pato = people.create_person(
        db,
        people.PersonCreate(
            display_name="Patrik Veselý", first_name="Patrik", last_name="Veselý", country="SK"
        ),
    )
    people.add_alias(db, pato, "Paťo")
    people.add_handle(db, pato, HandleType.PHONE, "0905 123 456")
    people.add_tag(db, pato, "Uni")
    people.add_note(db, pato, "Loves bouldering in Košice")

    ig = people.create_person(db, people.PersonCreate(display_name="someone"))
    people.add_handle(db, ig, HandleType.INSTAGRAM, "@someone.ig")

    terka = people.create_person(db, people.PersonCreate(display_name="Terézia Nováková"))
    people.add_alias(db, terka, "Terka")
    people.add_tag(db, terka, "Košice")

    people.create_person(db, people.PersonCreate(display_name="Paulína 100% Kováčová"))
    people.create_person(db, people.PersonCreate(display_name="Old Friend", archived=True))
    db.commit()
    return db


def names(db: Session, query: str, **kwargs: object) -> list[str]:
    return [p.display_name for p in search_people(db, query, **kwargs)]  # type: ignore[arg-type]


@pytest.mark.parametrize("query", ["pato", "paťo", "PAŤO", "Paťo", "patrik", "vesely", "VESELÝ"])
def test_diacritics_insensitive_name_and_alias(seeded: Session, query: str) -> None:
    assert names(seeded, query) == ["Patrik Veselý"]


def test_alias_only_match(seeded: Session) -> None:
    assert names(seeded, "terka") == ["Terézia Nováková"]


@pytest.mark.parametrize("query", ["@someone.ig", "someone.ig", "SOMEONE"])
def test_instagram_handle(seeded: Session, query: str) -> None:
    assert names(seeded, query) == ["someone"]


@pytest.mark.parametrize("query", ["0905", "0905 123", "+421905", "905123456"])
def test_phone(seeded: Session, query: str) -> None:
    assert names(seeded, query) == ["Patrik Veselý"]


def test_tag_and_note_text(seeded: Session) -> None:
    assert names(seeded, "uni") == ["Patrik Veselý"]
    assert names(seeded, "bouldering") == ["Patrik Veselý"]
    # "kosice" is Terka's tag and a word in Pato's note.
    assert sorted(names(seeded, "kosice")) == ["Patrik Veselý", "Terézia Nováková"]


def test_all_words_must_match(seeded: Session) -> None:
    assert names(seeded, "kosice uni") == ["Patrik Veselý"]
    assert names(seeded, "kosice nobody") == []


def test_tag_filter(seeded: Session) -> None:
    assert names(seeded, "", tag="košice") == ["Terézia Nováková"]
    assert names(seeded, "pato", tag="Košice") == []


def test_archived_excluded_by_default(seeded: Session) -> None:
    assert names(seeded, "old") == []
    assert names(seeded, "old", include_archived=True) == ["Old Friend"]


def test_like_wildcards_are_literal(seeded: Session) -> None:
    assert names(seeded, "100%") == ["Paulína 100% Kováčová"]
    assert names(seeded, "%") == ["Paulína 100% Kováčová"]
    assert names(seeded, "_") == []


def test_empty_query_lists_everyone_alphabetically(seeded: Session) -> None:
    assert names(seeded, "") == [
        "Patrik Veselý",
        "Paulína 100% Kováčová",
        "someone",
        "Terézia Nováková",
    ]


def test_name_prefix_ranks_first(seeded: Session) -> None:
    # "pa" is a prefix of both Patrik and Paulína; "Terka" doesn't match at all.
    assert names(seeded, "pa")[:2] == ["Patrik Veselý", "Paulína 100% Kováčová"]


def test_limit_and_offset(seeded: Session) -> None:
    assert len(names(seeded, "", limit=2)) == 2
    assert names(seeded, "", limit=2, offset=3) == ["Terézia Nováková"]
