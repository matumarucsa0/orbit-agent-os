"""Linking a person to an official calendar name (e.g. "Majo" -> Marián) for their name day."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orbit.domain.normalize import normalize_text
from orbit.domain.people import Person
from orbit.integrations import namedays
from orbit.services import duplicates, people
from orbit.services.dates import nameday_name, person_namedays
from orbit.services.errors import InvalidInputError


def make(db: Session, name: str, **fields: object) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name, **fields))


# --- Calendar search ---


def test_search_prefix_first_then_substring() -> None:
    results = namedays.get_calendar("SK").search("mari")
    normalized = [normalize_text(n) for n in results]
    assert "Marián" in results and "Mária" in results
    assert all("mari" in n for n in normalized)
    # Every prefix match comes before every substring-only match (e.g. "Rosmarína").
    starts = [n.startswith("mari") for n in normalized]
    assert starts == sorted(starts, reverse=True)


def test_search_is_diacritics_insensitive_and_limited() -> None:
    assert "Ľubica" in namedays.get_calendar("SK").search("lubica")
    assert len(namedays.get_calendar("SK").search("a", limit=5)) == 5
    assert namedays.get_calendar("SK").search("  ") == []
    assert namedays.get_calendar("SK").search("xyzzy") == []


def test_canonical_spelling() -> None:
    assert namedays.canonical("marian", "SK") == "Marián"
    assert namedays.canonical("SIMON", "SK") == "Šimon"  # the first spelling on the day wins
    assert namedays.canonical("Marián", "CZ") == "Marian"  # the CZ calendar's spelling
    assert namedays.canonical("Jozef", "CZ") is None  # CZ has Josef
    assert namedays.canonical("Marián", None) is None


# --- Service ---


def test_assign_uses_calendar_spelling_and_overrides_first_name(db: Session) -> None:
    majo = make(db, "Majo", first_name="Majo")
    assert person_namedays(majo) == []
    people.update_person(db, majo, people.PersonUpdate(nameday_name="marian"))
    assert majo.nameday_name == "Marián"
    assert nameday_name(majo) == "Marián"
    assert person_namedays(majo) == namedays.dates_for("Marián", "SK")
    people.update_person(db, majo, people.PersonUpdate(nameday_name=None))
    assert majo.nameday_name is None and person_namedays(majo) == []


def test_assign_rejects_names_outside_the_calendar(db: Session) -> None:
    person = make(db, "Majo")
    with pytest.raises(InvalidInputError, match="isn't in the SK name-day calendar"):
        people.update_person(db, person, people.PersonUpdate(nameday_name="Kevinator"))
    no_country = make(db, "Someone", nameday_country=None)
    with pytest.raises(InvalidInputError, match="Pick a name-day country"):
        people.update_person(db, no_country, people.PersonUpdate(nameday_name="Marián"))


def test_assign_with_country_in_one_update(db: Session) -> None:
    person = make(db, "Pepa", nameday_country=None)
    people.update_person(
        db, person, people.PersonUpdate(nameday_name="josef", nameday_country="CZ")
    )
    assert (person.nameday_name, person.nameday_country) == ("Josef", "CZ")
    assert person_namedays(person) == [(3, 19)]


def test_country_change_rechecks_the_linked_name(db: Session) -> None:
    person = make(db, "Jožko")
    people.update_person(db, person, people.PersonUpdate(nameday_name="Jozef"))
    with pytest.raises(InvalidInputError, match="CZ name-day calendar"):
        people.update_person(db, person, people.PersonUpdate(nameday_country="CZ"))
    assert person.nameday_country == "SK"  # nothing was applied

    # A name both calendars have is re-spelled for the new country.
    majo = make(db, "Majo")
    people.update_person(db, majo, people.PersonUpdate(nameday_name="Marián"))
    people.update_person(db, majo, people.PersonUpdate(nameday_country="CZ"))
    assert majo.nameday_name == "Marian"


def test_merge_fills_linked_name_only_within_the_same_country(db: Session) -> None:
    keep = make(db, "Marián Novák")
    other = make(db, "Majo")
    people.update_person(db, other, people.PersonUpdate(nameday_name="Marián"))
    duplicates.merge(db, keep.id, other.id)
    assert keep.nameday_name == "Marián"

    keep_cz = make(db, "Jozef CZ", nameday_country="CZ")
    other_sk = make(db, "Jožko")
    people.update_person(db, other_sk, people.PersonUpdate(nameday_name="Jozef"))
    duplicates.merge(db, keep_cz.id, other_sk.id)
    assert keep_cz.nameday_name is None


# --- Web ---


def add(api: TestClient, token: str, **body: object) -> int:
    response = api.post("/api/v1/people", json=body, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def test_web_search_assign_and_clear(web: TestClient, settings) -> None:
    token = settings.orbit_api_token.get_secret_value()
    pid = add(web, token, display_name="Majo", first_name="Majo")

    page = web.get(f"/people/{pid}").text
    assert "“Majo” isn't in the SK calendar" in page

    fragment = web.get(f"/people/{pid}/nameday-search", params={"q": "marian"}).text
    assert "<html" not in fragment
    assert "Marián" in fragment and "· SK" in fragment

    page = web.post(f"/people/{pid}/nameday", data={"name": "Marián", "country": "SK"}).text
    assert "Name day linked to Marián." in page
    assert "Linked to <strong>Marián</strong>" in page
    assert "· Marián (SK)" in page

    page = web.post(f"/people/{pid}/nameday/clear").text
    assert "matched from the first name again" in page
    assert "“Majo” isn't in the SK calendar" in page


def test_web_search_without_country_offers_both_and_sets_it(web: TestClient, settings) -> None:
    token = settings.orbit_api_token.get_secret_value()
    pid = add(web, token, display_name="Pepa", nameday_country=None)
    fragment = web.get(f"/people/{pid}/nameday-search", params={"q": "jozef"}).text
    assert "· SK" in fragment
    fragment = web.get(f"/people/{pid}/nameday-search", params={"q": "josef"}).text
    assert "· CZ" in fragment
    page = web.post(f"/people/{pid}/nameday", data={"name": "Josef", "country": "CZ"}).text
    assert "· Josef (CZ)" in page


def test_web_rejects_unknown_name(web: TestClient, settings) -> None:
    token = settings.orbit_api_token.get_secret_value()
    pid = add(web, token, display_name="X")
    page = web.post(f"/people/{pid}/nameday", data={"name": "Kevinator", "country": "SK"}).text
    assert "isn&#39;t in the SK name-day calendar" in page


def test_web_search_no_match(web: TestClient, settings) -> None:
    token = settings.orbit_api_token.get_secret_value()
    pid = add(web, token, display_name="X")
    assert (
        "No calendar name matches"
        in web.get(f"/people/{pid}/nameday-search", params={"q": "xyzzy"}).text
    )


def test_api_patch_nameday_name(api: TestClient) -> None:
    created = api.post("/api/v1/people", json={"display_name": "Majo"}).json()
    patched = api.patch(f"/api/v1/people/{created['id']}", json={"nameday_name": "marian"})
    assert patched.status_code == 200
    assert patched.json()["nameday_name"] == "Marián"
    bad = api.patch(f"/api/v1/people/{created['id']}", json={"nameday_name": "Kevinator"})
    assert bad.status_code == 422
