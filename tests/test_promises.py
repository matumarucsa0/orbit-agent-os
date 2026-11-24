from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbit.domain.enums import PromiseDirection, PromiseStatus
from orbit.domain.ledger import Promise
from orbit.domain.people import Person
from orbit.services import people, promises
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.promises import PromiseCreate, PromiseUpdate

TODAY = date(2026, 9, 23)
I_PROMISED = PromiseDirection.I_PROMISED
THEY_PROMISED = PromiseDirection.THEY_PROMISED


def make(db: Session, name: str) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name))


def promise(
    db: Session,
    person: Person,
    text: str,
    direction: PromiseDirection = I_PROMISED,
    due: date | None = None,
) -> Promise:
    data = PromiseCreate(person_id=person.id, direction=direction, text=text, due=due)
    return promises.create_promise(db, data, TODAY)


def test_create_defaults_due_to_a_week_out(db: Session) -> None:
    pato = make(db, "Pato")
    p = promise(db, pato, "send the photos")
    assert p.due == date(2026, 9, 30)
    assert p.status is PromiseStatus.OPEN
    assert p.closed_at is None
    assert pato.promises == [p]


def test_create_with_due_and_direction(db: Session) -> None:
    p = promise(db, make(db, "Terka"), "return the book", THEY_PROMISED, date(2026, 10, 5))
    assert (p.direction, p.due) == (THEY_PROMISED, date(2026, 10, 5))


def test_create_blank_due_means_default() -> None:
    data = PromiseCreate(person_id=1, direction=I_PROMISED, text="x", due="")  # type: ignore[arg-type]
    assert data.due is None


def test_create_needs_text() -> None:
    with pytest.raises(ValueError):
        PromiseCreate(person_id=1, direction=I_PROMISED, text="   ")


def test_create_for_missing_person(db: Session) -> None:
    with pytest.raises(NotFoundError):
        promises.create_promise(
            db, PromiseCreate(person_id=999, direction=I_PROMISED, text="x"), TODAY
        )


def test_get_promise(db: Session) -> None:
    p = promise(db, make(db, "Pato"), "x")
    assert promises.get_promise(db, p.id) is p
    with pytest.raises(NotFoundError):
        promises.get_promise(db, 999)


def test_update_fields(db: Session) -> None:
    p = promise(db, make(db, "Pato"), "send photos")
    promises.update_promise(
        db, p, PromiseUpdate(text="send the trip photos", direction=THEY_PROMISED, due=None)
    )
    assert (p.text, p.direction, p.due) == ("send the trip photos", THEY_PROMISED, None)


def test_update_only_touches_given_fields(db: Session) -> None:
    p = promise(db, make(db, "Pato"), "send photos")
    promises.update_promise(db, p, PromiseUpdate(due=date(2026, 12, 24)))
    assert (p.text, p.due, p.status) == ("send photos", date(2026, 12, 24), PromiseStatus.OPEN)


@pytest.mark.parametrize(
    "data",
    [PromiseUpdate(text=""), PromiseUpdate(direction=None), PromiseUpdate(status=None)],
)
def test_update_rejects_clearing_required_fields(db: Session, data: PromiseUpdate) -> None:
    p = promise(db, make(db, "Pato"), "send photos")
    with pytest.raises(InvalidInputError):
        promises.update_promise(db, p, data)
    assert (p.text, p.status) == ("send photos", PromiseStatus.OPEN)


def test_status_transitions_stamp_and_clear_closed_at(db: Session) -> None:
    p = promise(db, make(db, "Pato"), "send photos")
    done_at = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    promises.update_promise(db, p, PromiseUpdate(status=PromiseStatus.DONE), now=done_at)
    assert (p.status, p.closed_at) == (PromiseStatus.DONE, done_at)

    # done -> dropped keeps the original close time
    later = datetime(2026, 9, 26, tzinfo=UTC)
    promises.update_promise(db, p, PromiseUpdate(status=PromiseStatus.DONE), now=later)
    assert p.closed_at == done_at
    promises.update_promise(db, p, PromiseUpdate(status=PromiseStatus.DROPPED), now=later)
    assert (p.status, p.closed_at) == (PromiseStatus.DROPPED, later)

    promises.update_promise(db, p, PromiseUpdate(status=PromiseStatus.OPEN))
    assert (p.status, p.closed_at) == (PromiseStatus.OPEN, None)


def test_database_keeps_status_and_closed_at_consistent(db: Session) -> None:
    p = promise(db, make(db, "Pato"), "x")
    p.status = PromiseStatus.DONE  # without closed_at
    with pytest.raises(IntegrityError):
        db.flush()


def test_delete(db: Session) -> None:
    pato = make(db, "Pato")
    p = promise(db, pato, "x")
    promises.delete_promise(db, p)
    assert pato.promises == []
    assert db.scalar(select(func.count(Promise.id))) == 0


def test_list_open_by_due_date_undated_last(db: Session) -> None:
    pato, terka = make(db, "Pato"), make(db, "Terka")
    late = promise(db, pato, "late", due=date(2026, 12, 1))
    soon = promise(db, terka, "soon", THEY_PROMISED, due=date(2026, 9, 24))
    undated = promise(db, pato, "undated")
    promises.update_promise(db, undated, PromiseUpdate(due=None))
    done = promise(db, pato, "done")
    promises.update_promise(db, done, PromiseUpdate(status=PromiseStatus.DONE))

    assert promises.list_promises(db) == [soon, late, undated]
    assert promises.list_promises(db, person_id=pato.id) == [late, undated]
    assert promises.list_promises(db, direction=THEY_PROMISED) == [soon]
    assert promises.list_promises(db, status=PromiseStatus.DONE) == [done]
    assert set(promises.list_promises(db, status=None)) == {late, soon, undated, done}


def test_list_closed_most_recent_first(db: Session) -> None:
    pato = make(db, "Pato")
    first, second = promise(db, pato, "a"), promise(db, pato, "b")
    promises.update_promise(
        db, first, PromiseUpdate(status=PromiseStatus.DONE), now=datetime(2026, 9, 1, tzinfo=UTC)
    )
    promises.update_promise(
        db, second, PromiseUpdate(status=PromiseStatus.DONE), now=datetime(2026, 9, 2, tzinfo=UTC)
    )
    assert promises.list_promises(db, status=PromiseStatus.DONE) == [second, first]


def test_is_overdue(db: Session) -> None:
    pato = make(db, "Pato")
    past = promise(db, pato, "past", due=date(2026, 9, 22))
    today = promise(db, pato, "today", due=TODAY)
    undated = promise(db, pato, "undated")
    undated.due = None
    closed = promise(db, pato, "closed", due=date(2026, 9, 1))
    promises.update_promise(db, closed, PromiseUpdate(status=PromiseStatus.DROPPED))
    assert promises.is_overdue(past, TODAY)
    assert not promises.is_overdue(today, TODAY)
    assert not promises.is_overdue(undated, TODAY)
    assert not promises.is_overdue(closed, TODAY)
