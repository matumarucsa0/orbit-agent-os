from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orbit.domain.people import Person
from orbit.services import cadence, interactions, people, reminders
from orbit.services.errors import InvalidInputError, NotFoundError

PRAGUE = ZoneInfo("Europe/Prague")
TODAY = date(2026, 9, 24)
ADDED = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


def person(db: Session, name: str, *tags: str, **fields: Any) -> Person:
    created = people.create_person(db, people.PersonCreate(display_name=name, **fields))
    created.created_at = ADDED
    for tag in tags:
        people.add_tag(db, created, tag)
    db.flush()
    return created


def tag_cadence(db: Session, name: str, days: int | None) -> None:
    cadence.set_tag_cadence(db, people.get_or_create_tag(db, name).id, days)


def talked(db: Session, who: Person, on: date) -> None:
    interactions.create_interaction(
        db, interactions.InteractionCreate(occurred_on=on, person_ids=[who.id])
    )


def test_effective_cadence(db: Session) -> None:
    tag_cadence(db, "close", 30)
    tag_cadence(db, "gym", 14)
    people.get_or_create_tag(db, "work")  # no cadence
    both = person(db, "Paťo", "close", "gym", "work")
    own = person(db, "Terka", "gym", cadence_days=60)
    none = person(db, "Anna", "work")
    assert cadence.effective_cadence(both) == 14  # the tightest tag
    assert cadence.effective_cadence(own) == 60  # the person's own wins, even if looser
    assert cadence.effective_cadence(none) is None


def test_due_after_the_last_interaction(db: Session) -> None:
    tag_cadence(db, "close", 30)
    pato = person(db, "Paťo", "close")
    talked(db, pato, date(2026, 8, 25))  # due 24 Sep
    talked(db, pato, date(2026, 8, 1))  # older: ignored
    terka = person(db, "Terka", "close")
    talked(db, terka, date(2026, 8, 26))  # due 25 Sep: not yet
    (entry,) = cadence.due(db, TODAY, PRAGUE)
    assert entry.person is pato
    assert (entry.cadence_days, entry.last_contact, entry.due_on) == (
        30,
        date(2026, 8, 25),
        TODAY,
    )
    assert entry.overdue_days(TODAY) == 0


def test_without_interactions_counts_from_when_they_were_added(db: Session) -> None:
    person(db, "Paťo", cadence_days=120)  # added 1 Jun: due 29 Sep
    anna = person(db, "Anna", cadence_days=100)  # due 9 Sep
    (entry,) = cadence.due(db, TODAY, PRAGUE)
    assert entry.person is anna
    assert entry.last_contact is None
    assert entry.overdue_days(TODAY) == 15


def test_most_overdue_first_and_archived_left_out(db: Session) -> None:
    tag_cadence(db, "close", 10)
    recent = person(db, "B recent", "close")
    talked(db, recent, date(2026, 9, 10))
    old = person(db, "A old", "close")
    talked(db, old, date(2026, 7, 1))
    person(db, "C archived", "close", archived=True)
    person(db, "D untagged")
    assert [e.person.display_name for e in cadence.due(db, TODAY, PRAGUE)] == [
        "A old",
        "B recent",
    ]


def test_set_tag_cadence_validation(db: Session) -> None:
    tag = people.get_or_create_tag(db, "close")
    assert cadence.set_tag_cadence(db, tag.id, 30).cadence_days == 30
    assert cadence.set_tag_cadence(db, tag.id, None).cadence_days is None
    with pytest.raises(InvalidInputError):
        cadence.set_tag_cadence(db, tag.id, 0)
    with pytest.raises(NotFoundError):
        cadence.set_tag_cadence(db, 999, 30)


def test_person_cadence_is_validated(db: Session) -> None:
    with pytest.raises(ValueError):
        people.PersonCreate(display_name="X", cadence_days=0)


def test_digest_lists_catch_ups(db: Session) -> None:
    tag_cadence(db, "close", 30)
    pato = person(db, "Paťo", "close")
    talked(db, pato, date(2026, 8, 1))
    person(db, "Anna", cadence_days=7)
    sunday = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
    digest = reminders.build_digest(db, sunday, PRAGUE)
    assert [k.person.display_name for k in digest.keep_in_touch] == ["Anna", "Paťo"]
    body = reminders.digest_push(digest, "https://orbit.example.com", PRAGUE).body
    assert "👋 Time to catch up (2)\n• Anna (nothing logged yet)\n• Paťo (last 1 Aug)" in body


# --- Web / API ---


def _seed(app: FastAPI) -> int:
    with app.state.sessionmaker() as session:
        tag_cadence(session, "close", 30)
        pato = person(session, "Paťo", "close")
        talked(session, pato, date(2020, 1, 1))
        session.commit()
        return pato.id


def test_home_and_keep_in_touch_page(web: TestClient, app: FastAPI) -> None:
    _seed(app)
    home = web.get("/").text
    assert "👋 Paťo" in home
    page = web.get("/keep-in-touch").text
    assert "👋 Paťo" in page
    assert 'name="days"' in page and 'value="30"' in page


def test_set_tag_cadence_from_the_page(web: TestClient, app: FastAPI) -> None:
    _seed(app)
    with app.state.sessionmaker() as session:
        tag_id = people.get_or_create_tag(session, "close").id
    response = web.post(f"/tags/{tag_id}/cadence", data={"days": ""}, follow_redirects=False)
    assert response.status_code == 303
    assert "Catch up with" not in web.get("/").text
    assert "between 1 and 3650" in web.post(f"/tags/{tag_id}/cadence", data={"days": "abc"}).text


def test_person_page_and_edit(web: TestClient, app: FastAPI) -> None:
    pato_id = _seed(app)
    assert "Every 30 days" in web.get(f"/people/{pato_id}").text
    web.post(f"/people/{pato_id}/edit", data={"display_name": "Paťo", "cadence_days": "90"})
    with app.state.sessionmaker() as session:
        assert people.get_person(session, pato_id).cadence_days == 90
    web.post(f"/people/{pato_id}/edit", data={"display_name": "Paťo", "cadence_days": ""})
    with app.state.sessionmaker() as session:
        assert people.get_person(session, pato_id).cadence_days is None


def test_api_cadence(api: TestClient, app: FastAPI) -> None:
    pato_id = _seed(app)
    tags = api.get("/api/v1/tags").json()
    assert tags[0]["cadence_days"] == 30
    response = api.patch(f"/api/v1/tags/{tags[0]['id']}", json={"cadence_days": 7})
    assert response.json()["cadence_days"] == 7
    assert api.patch(f"/api/v1/tags/{tags[0]['id']}", json={"cadence_days": 0}).status_code == 422
    person_out = api.patch(f"/api/v1/people/{pato_id}", json={"cadence_days": 45}).json()
    assert person_out["cadence_days"] == 45


def test_snooze_pushes_the_due_day_out(db: Session) -> None:
    tag_cadence(db, "close", 30)
    pato = person(db, "Paťo", "close")
    talked(db, pato, date(2026, 8, 1))
    assert [k.person for k in cadence.due(db, TODAY, PRAGUE)] == [pato]
    until = cadence.snooze(pato, TODAY, 7)
    assert until == date(2026, 10, 1)
    assert cadence.due(db, TODAY, PRAGUE) == []
    assert cadence.due(db, date(2026, 10, 1), PRAGUE)[0].due_on == until
    # Talking later moves the due day past the snooze: the snooze no longer matters.
    talked(db, pato, date(2026, 9, 30))
    status = cadence.status(pato, date(2026, 9, 30), PRAGUE)
    assert status is not None and status.due_on == date(2026, 10, 30)


@pytest.mark.parametrize("days", [0, -1, cadence.MAX_SNOOZE_DAYS + 1])
def test_snooze_days_are_checked(db: Session, days: int) -> None:
    pato = person(db, "Paťo")
    with pytest.raises(InvalidInputError):
        cadence.snooze(pato, TODAY, days)
    assert pato.snoozed_until is None


def test_one_tap_talked_and_snooze_from_the_page(web: TestClient, app: FastAPI) -> None:
    pato_id = _seed(app)
    page = web.get("/keep-in-touch").text
    assert f'action="/people/{pato_id}/snooze"' in page
    assert f'action="/people/{pato_id}/interactions"' in page

    response = web.post(
        f"/people/{pato_id}/snooze",
        data={"days": "7", "next": "/keep-in-touch"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/keep-in-touch"
    page = web.get("/keep-in-touch").text
    assert "👋 Paťo" not in page and "Snoozed Paťo until" in page
    assert f'action="/people/{pato_id}/snooze/clear"' in page  # Undo
    web.post(f"/people/{pato_id}/snooze/clear", data={"next": "/keep-in-touch"})
    assert "👋 Paťo" in web.get("/keep-in-touch").text

    response = web.post(
        f"/people/{pato_id}/interactions",
        data={"kind": "call", "next": "/"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"
    home = web.get("/").text
    assert "Logged: called Paťo today." in home and "👋 Paťo" not in home
    undo = re.search(rf'action="/people/{pato_id}/interactions/(\d+)/delete"', home)
    assert undo
    web.post(undo.group(0).split('"')[1], data={"next": "/"})
    assert "👋 Paťo" in web.get("/").text
