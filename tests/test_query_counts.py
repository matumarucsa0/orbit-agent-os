"""Query counts must not grow with the number of people."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, DebtType
from orbit.services import interactions, ledger, people
from orbit.services.context import ContextWindow, build_context

WINDOW = ContextWindow(date(2026, 9, 24), date(2026, 9, 30), ZoneInfo("Europe/Prague"))


@contextmanager
def count_queries(engine: Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def seed(db: Session, count: int) -> None:
    for n in range(count):
        who = people.create_person(db, people.PersonCreate(display_name=f"Person {n}"))
        people.add_tag(db, who, "friend")
        interactions.create_interaction(
            db,
            interactions.InteractionCreate(occurred_on=date(2026, 9, 20), person_ids=[who.id]),
        )
        ledger.create_debt(
            db,
            ledger.DebtCreate(
                person_id=who.id,
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.MONEY,
                amount_minor=1000,
                currency="EUR",
            ),
            "EUR",
        )
    db.commit()
    db.expire_all()


def context_queries(db: Session) -> int:
    with count_queries(db.get_bind()) as seen:  # type: ignore[arg-type]
        build_context(db, WINDOW)
    db.expire_all()
    return len(seen)


def test_build_context_does_not_query_per_person(db: Session) -> None:
    seed(db, 5)
    few = context_queries(db)
    seed(db, 30)
    many = context_queries(db)
    assert many == few
    assert many <= 60


def test_the_people_page_does_not_query_per_person(web: TestClient, app: FastAPI) -> None:
    def page_queries() -> int:
        with count_queries(app.state.engine) as seen:
            assert web.get("/people").status_code == 200
        return len(seen)

    with app.state.sessionmaker() as db:
        seed(db, 3)
    few = page_queries()
    with app.state.sessionmaker() as db:
        seed(db, 30)
    many = page_queries()
    assert many == few
    assert many <= 10
