from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy.orm import Session

from orbit.api.schemas import (
    AliasIn,
    AliasOut,
    DuplicateOut,
    HandleIn,
    HandleOut,
    MergeIn,
    MergeRecordOut,
    NoteIn,
    NoteOut,
    PairIn,
    PersonOut,
    PersonSummaryOut,
    TagCadenceIn,
    TagIn,
    TagOut,
    TagWithCountOut,
)
from orbit.deps import DbSession
from orbit.domain.people import Person
from orbit.services import cadence, duplicates, people
from orbit.services.interactions import last_interaction
from orbit.services.search import search_people

router = APIRouter(tags=["people"])


def person_out(session: Session, person: Person) -> PersonOut:
    out = PersonOut.model_validate(person)
    last = last_interaction(session, person.id)
    out.last_interaction_on = last.occurred_on if last else None
    return out


# Fixed paths first, so they aren't captured by /people/{person_id}.


@router.get("/people/duplicates", response_model=list[DuplicateOut])
def list_duplicates(db: DbSession) -> list[DuplicateOut]:
    return [
        DuplicateOut(
            a=PersonSummaryOut.model_validate(c.a),
            b=PersonSummaryOut.model_validate(c.b),
            reason=c.reason,
        )
        for c in duplicates.find_candidates(db)
    ]


@router.post("/people/duplicates/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_duplicate(body: PairIn, db: DbSession) -> None:
    duplicates.dismiss(db, body.a_id, body.b_id)
    db.commit()


@router.post("/people/merge", response_model=PersonOut)
def merge_people(body: MergeIn, db: DbSession) -> PersonOut:
    person = duplicates.merge(db, body.keep_id, body.merge_id)
    db.commit()
    return person_out(db, person)


@router.get("/people/merges", response_model=list[MergeRecordOut])
def list_merges(db: DbSession) -> list[MergeRecordOut]:
    """Merges of the last 30 days that can be undone, newest first."""
    return [MergeRecordOut.model_validate(m) for m in duplicates.list_merges(db)]


@router.post("/people/merges/{record_id}/undo", response_model=PersonOut)
def undo_merge(record_id: int, db: DbSession) -> PersonOut:
    """Bring the merged person back; returns them (possibly under a new id)."""
    person = duplicates.undo_merge(db, record_id)
    db.commit()
    return person_out(db, people.get_person(db, person.id))


@router.get("/tags", response_model=list[TagWithCountOut])
def list_tags(db: DbSession) -> list[TagWithCountOut]:
    return [
        TagWithCountOut(id=t.id, name=t.name, cadence_days=t.cadence_days, people=n)
        for t, n in people.list_tags(db)
    ]


@router.patch("/tags/{tag_id}", response_model=TagOut)
def update_tag(tag_id: int, body: TagCadenceIn, db: DbSession) -> TagOut:
    """Set (or clear, with null) a tag's keep-in-touch cadence."""
    tag = cadence.set_tag_cadence(db, tag_id, body.cadence_days)
    db.commit()
    return TagOut.model_validate(tag)


@router.get("/people", response_model=list[PersonSummaryOut])
def list_people(
    db: DbSession,
    q: str = "",
    tag: str | None = None,
    archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Person]:
    return search_people(db, q, tag=tag, include_archived=archived, limit=limit, offset=offset)


@router.post("/people", response_model=PersonOut, status_code=status.HTTP_201_CREATED)
def create_person(body: people.PersonCreate, db: DbSession) -> PersonOut:
    person = people.create_person(db, body)
    db.commit()
    return person_out(db, person)


@router.get("/people/{person_id}", response_model=PersonOut)
def get_person(person_id: int, db: DbSession) -> PersonOut:
    return person_out(db, people.get_person(db, person_id))


@router.patch("/people/{person_id}", response_model=PersonOut)
def update_person(person_id: int, body: people.PersonUpdate, db: DbSession) -> PersonOut:
    person = people.update_person(db, people.get_person(db, person_id), body)
    db.commit()
    return person_out(db, person)


@router.delete("/people/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person(person_id: int, db: DbSession) -> None:
    people.delete_person(db, people.get_person(db, person_id))
    db.commit()


@router.post(
    "/people/{person_id}/handles", response_model=HandleOut, status_code=status.HTTP_201_CREATED
)
def add_handle(person_id: int, body: HandleIn, db: DbSession) -> HandleOut:
    handle = people.add_handle(
        db, people.get_person(db, person_id), body.type, body.value, body.label
    )
    db.commit()
    return HandleOut.model_validate(handle)


@router.delete("/people/{person_id}/handles/{handle_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_handle(person_id: int, handle_id: int, db: DbSession) -> None:
    people.remove_handle(db, people.get_person(db, person_id), handle_id)
    db.commit()


@router.post(
    "/people/{person_id}/aliases", response_model=AliasOut, status_code=status.HTTP_201_CREATED
)
def add_alias(person_id: int, body: AliasIn, db: DbSession) -> AliasOut:
    alias = people.add_alias(db, people.get_person(db, person_id), body.value)
    db.commit()
    return AliasOut.model_validate(alias)


@router.delete("/people/{person_id}/aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_alias(person_id: int, alias_id: int, db: DbSession) -> None:
    people.remove_alias(db, people.get_person(db, person_id), alias_id)
    db.commit()


@router.post("/people/{person_id}/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED)
def add_tag(person_id: int, body: TagIn, db: DbSession) -> TagOut:
    tag = people.add_tag(db, people.get_person(db, person_id), body.name)
    db.commit()
    return TagOut.model_validate(tag)


@router.delete("/people/{person_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tag(person_id: int, tag_id: int, db: DbSession) -> None:
    people.remove_tag(db, people.get_person(db, person_id), tag_id)
    db.commit()


@router.post(
    "/people/{person_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED
)
def add_note(person_id: int, body: NoteIn, db: DbSession) -> NoteOut:
    note = people.add_note(db, people.get_person(db, person_id), body.text)
    db.commit()
    return NoteOut.model_validate(note)


@router.patch("/people/{person_id}/notes/{note_id}", response_model=NoteOut)
def update_note(person_id: int, note_id: int, body: NoteIn, db: DbSession) -> NoteOut:
    note = people.get_note(db, people.get_person(db, person_id), note_id)
    people.update_note(db, note, body.text)
    db.commit()
    return NoteOut.model_validate(note)


@router.delete("/people/{person_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(person_id: int, note_id: int, db: DbSession) -> None:
    people.delete_note(db, people.get_note(db, people.get_person(db, person_id), note_id))
    db.commit()
