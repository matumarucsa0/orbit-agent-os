from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, HandleType, MeetingLinkSource
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.people import MergeRecord, Person, interaction_person, not_duplicate
from orbit.services import duplicates, interactions, ledger, people, promises
from orbit.services.errors import InvalidInputError, NotFoundError


def make(db: Session, name: str, **fields: Any) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name, **fields))


def meeting(db: Session, uid: str) -> Meeting:
    start = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)
    found = Meeting(
        calendar_uid=uid,
        calendar_name="Personal",
        title=uid,
        start_utc=start,
        end_utc=start + timedelta(hours=1),
        cancelled=False,
    )
    db.add(found)
    db.flush()
    return found


def link(db: Session, m: Meeting, who: Person, source: MeetingLinkSource) -> None:
    db.add(MeetingPerson(meeting_id=m.id, person_id=who.id, link_source=source))
    db.flush()


def test_undo_brings_everything_back(db: Session) -> None:
    keep = make(db, "Patrik Veselý", country="SK")
    other = make(
        db,
        "pato",
        first_name="Patrik",
        city="Košice",
        birthday_month=3,
        birthday_day=17,
        how_met="gym",
    )
    other.vcard_uid = "uid-1"  # set by contact import only
    terka = make(db, "Terka", met_through_id=other.id)
    people.add_alias(db, keep, "Paťo")
    people.add_alias(db, other, "PATO")  # dropped by the merge: keep already has it
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
    shared_meeting, own_meeting = meeting(db, "shared"), meeting(db, "own")
    link(db, shared_meeting, keep, MeetingLinkSource.TITLE)
    link(db, shared_meeting, other, MeetingLinkSource.MANUAL)
    link(db, own_meeting, other, MeetingLinkSource.ATTENDEE)
    pizza = ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=other.id, direction=DebtDirection.THEY_OWE_ME, amount_minor=1200
        ),
        "EUR",
    )
    promise = promises.create_promise(
        db,
        promises.PromiseCreate(person_id=other.id, direction="i_promised", text="photos"),
        today=date(2026, 9, 1),
    )
    duplicates.dismiss(db, other.id, terka.id)
    db.commit()
    other_id = other.id

    duplicates.merge(db, keep.id, other_id)
    db.commit()
    (record,) = duplicates.list_merges(db)
    assert (record.keep_id, record.merged_id, record.merged_name) == (keep.id, other_id, "pato")

    restored = duplicates.undo_merge(db, record.id)
    db.commit()
    db.expire_all()

    assert restored.id == other_id
    assert (restored.display_name, restored.first_name, restored.city, restored.vcard_uid) == (
        "pato",
        "Patrik",
        "Košice",
        "uid-1",
    )
    assert (restored.birthday_month, restored.birthday_day) == (3, 17)
    assert sorted(a.value for a in restored.aliases) == ["PATO", "Veselko"]
    assert [h.value for h in restored.handles] == ["@pato"]
    assert [n.text for n in restored.notes] == ["from the IG person"]
    assert sorted(t.name for t in restored.tags) == ["Uni", "gym"]
    assert [d.id for d in restored.debts] == [pizza.id]
    assert [p.id for p in restored.promises] == [promise.id]

    keep = people.get_person(db, keep.id)
    assert (keep.first_name, keep.city, keep.birthday_month, keep.vcard_uid) == (
        None,
        None,
        None,
        None,
    )
    assert [a.value for a in keep.aliases] == ["Paťo"]  # "pato" added by the merge is gone
    assert [h.value for h in keep.handles] == ["+421905123456"]
    assert [t.name for t in keep.tags] == ["Uni"]
    assert keep.notes == [] and keep.debts == [] and keep.promises == []
    links = db.execute(select(interaction_person)).all()
    assert sorted((r.interaction_id, r.person_id) for r in links) == sorted(
        [(both.id, keep.id), (both.id, other_id), (only_other.id, other_id)]
    )
    meeting_links = db.execute(
        select(MeetingPerson.meeting_id, MeetingPerson.person_id, MeetingPerson.link_source)
    ).all()
    assert sorted(meeting_links) == sorted(
        [
            (shared_meeting.id, keep.id, MeetingLinkSource.TITLE),
            (shared_meeting.id, other_id, MeetingLinkSource.MANUAL),
            (own_meeting.id, other_id, MeetingLinkSource.ATTENDEE),
        ]
    )
    assert people.get_person(db, terka.id).met_through_id == other_id
    assert db.scalar(select(func.count()).select_from(not_duplicate)) == 1
    assert db.get(MergeRecord, record.id).undone_at is not None  # type: ignore[union-attr]
    assert duplicates.list_merges(db) == []
    with pytest.raises(InvalidInputError, match="already undone"):
        duplicates.undo_merge(db, record.id)


def test_changes_since_the_merge_are_kept(db: Session) -> None:
    keep = make(db, "Patrik")
    other = make(db, "Pato", city="Košice")
    people.add_note(db, other, "keep me")
    gone = people.add_note(db, other, "deleted later")
    debt = ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=other.id, direction=DebtDirection.THEY_OWE_ME, amount_minor=1000
        ),
        "EUR",
    )
    db.commit()
    other_id = other.id
    duplicates.merge(db, keep.id, other_id)
    # After the merge: an edit, a deletion, a new note, a partial settlement.
    people.update_person(db, keep, people.PersonUpdate(city="Brno"))
    people.delete_note(db, gone)
    people.add_note(db, keep, "written after the merge")
    remainder = ledger.settle(db, debt, 400).remainder
    assert remainder is not None
    db.commit()

    (record,) = duplicates.list_merges(db)
    restored = duplicates.undo_merge(db, record.id)
    db.commit()
    db.expire_all()

    assert people.get_person(db, keep.id).city == "Brno"  # edited since: not reverted
    assert [n.text for n in people.get_person(db, keep.id).notes] == ["written after the merge"]
    assert [n.text for n in restored.notes] == ["keep me"]
    assert sorted(d.id for d in restored.debts) == sorted([debt.id, remainder.id])


def test_undo_under_a_new_id_when_the_old_one_was_taken(db: Session) -> None:
    keep = make(db, "Patrik")
    other = make(db, "Pato")
    db.commit()
    other_id = other.id
    duplicates.merge(db, keep.id, other_id)
    db.commit()
    newcomer = make(db, "Newcomer")  # SQLite may reuse the freed highest id
    db.commit()
    (record,) = duplicates.list_merges(db)
    restored = duplicates.undo_merge(db, record.id)
    db.commit()
    assert restored.display_name == "Pato"
    assert people.get_person(db, newcomer.id).display_name == "Newcomer"
    if newcomer.id == other_id:
        assert restored.id != other_id


def test_undo_errors_and_old_merges(db: Session) -> None:
    with pytest.raises(NotFoundError):
        duplicates.undo_merge(db, 42)
    keep, other = make(db, "Patrik"), make(db, "Pato")
    db.commit()
    duplicates.merge(db, keep.id, other.id)
    db.commit()
    later = datetime.now(UTC) + timedelta(days=duplicates.UNDO_DAYS + 1)
    assert duplicates.list_merges(db, now=later) == []


def test_deleting_the_kept_person_drops_the_record(db: Session) -> None:
    keep, other = make(db, "Patrik"), make(db, "Pato")
    db.commit()
    duplicates.merge(db, keep.id, other.id)
    db.commit()
    people.delete_person(db, keep)
    db.commit()
    assert db.scalar(select(func.count()).select_from(MergeRecord)) == 0


# --- Web / API ---


def _merged(app: FastAPI) -> tuple[int, int]:
    with app.state.sessionmaker() as session:
        keep, other = make(session, "Patrik"), make(session, "Pato")
        session.commit()
        ids = keep.id, other.id
        duplicates.merge(session, *ids)
        session.commit()
        return ids


def test_web_undo(web: TestClient, app: FastAPI) -> None:
    with app.state.sessionmaker() as session:
        keep, other = make(session, "Patrik"), make(session, "Pato")
        session.commit()
        keep_id, other_id = keep.id, other.id
    page = web.post("/people/merge", data={"keep_id": keep_id, "merge_id": other_id}).text
    assert "Undo…" in page
    listing = web.get("/people/duplicates").text
    assert "Recent merges" in listing and "Pato →" in listing
    with app.state.sessionmaker() as session:
        (record,) = duplicates.list_merges(session)
    response = web.post(f"/people/merges/{record.id}/undo", follow_redirects=False)
    assert response.headers["location"] == f"/people/{other_id}"
    again = web.post(f"/people/merges/{record.id}/undo")
    assert "already undone" in again.text


def test_api_undo(api: TestClient, app: FastAPI) -> None:
    keep_id, other_id = _merged(app)
    (record,) = api.get("/api/v1/people/merges").json()
    assert (record["keep_id"], record["merged_id"], record["merged_name"]) == (
        keep_id,
        other_id,
        "Pato",
    )
    restored = api.post(f"/api/v1/people/merges/{record['id']}/undo").json()
    assert restored["display_name"] == "Pato"
    assert api.post(f"/api/v1/people/merges/{record['id']}/undo").status_code == 422
    assert api.get("/api/v1/people/merges").json() == []
