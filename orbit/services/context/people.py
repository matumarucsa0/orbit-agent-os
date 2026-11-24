"""People: who matters in the window, and what is known about them.

Without a focus: people who matter to the window: a birthday or name day in it, a keep-in-touch
due by its end, an interaction in it, a meeting in it, or something open between us (a debt or a
promise). With a person focus: that person only, whatever the reason.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.enums import PromiseStatus
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.people import Interaction, Person, interaction_person
from orbit.services import cadence, dates
from orbit.services.context.base import (
    NAME_CHARS,
    ContextFocus,
    ContextWindow,
    ModuleContext,
    text,
)

MAX_PEOPLE = 300
MAX_NOTES = 50  # newest first
MAX_INTERACTIONS = 20  # newest first, from any time


class DateContext(BaseModel):
    kind: Literal["birthday", "nameday"]
    on: date
    detail: str  # "turns 32"; the celebrated name for a name day; "" for a birthday


class LastContactContext(BaseModel):
    on: date
    kind: str
    summary: str | None


class InteractionContext(BaseModel):
    on: date
    kind: str
    summary: str | None


class NoteContext(BaseModel):
    on: date  # the day it was written (local)
    text: str


class BirthdayContext(BaseModel):
    month: int
    day: int
    year: int | None


class PersonContext(BaseModel):
    id: int
    name: str
    aliases: list[str]
    tags: list[str]
    city: str | None
    country: str | None
    how_met: str | None
    birthday: BirthdayContext | None
    handle_types: list[str]  # which channels exist ("instagram", "phone"), never their values
    last_contact: LastContactContext | None
    interactions: list[InteractionContext]  # newest first
    notes: list[NoteContext]  # newest first
    upcoming_dates: list[DateContext]
    # When keep-in-touch runs out, if a cadence applies and that is by the window's end.
    keep_in_touch_due_on: date | None


class PeopleContext(ModuleContext):
    trimmable: ClassVar[tuple[str, ...]] = ("people",)
    module: Literal["people"] = "people"
    people: list[PersonContext]


def _interacted(session: Session, window: ContextWindow) -> set[int]:
    rows = session.scalars(
        select(interaction_person.c.person_id)
        .join(Interaction, Interaction.id == interaction_person.c.interaction_id)
        .where(Interaction.occurred_on.between(window.start, window.end))
    )
    return set(rows)


def _open_between_us(session: Session) -> set[int]:
    owed = select(Debt.person_id).where(Debt.settled_at.is_(None))
    promised = select(Promise.person_id).where(Promise.status == PromiseStatus.OPEN)
    return set(session.scalars(owed)) | set(session.scalars(promised))


def _meeting_people(session: Session, window: ContextWindow) -> set[int]:
    return set(
        session.scalars(
            select(MeetingPerson.person_id)
            .join(Meeting, Meeting.id == MeetingPerson.meeting_id)
            .where(
                Meeting.cancelled.is_(False),
                Meeting.start_utc >= window.start_utc,
                Meeting.start_utc < window.end_utc,
            )
        )
    )


def _candidates(session: Session, window: ContextWindow) -> set[int]:
    found = {u.person.id for u in dates.upcoming(session, window.start, window.days)}
    found |= {s.person.id for s in cadence.due(session, window.end, window.tz)}
    found |= _interacted(session, window) | _meeting_people(session, window)
    return found | _open_between_us(session)


def _person_context(session: Session, person: Person, window: ContextWindow) -> PersonContext:
    # The interactions are already loaded for every person; no query per person.
    last = max(person.interactions, key=lambda i: (i.occurred_on, i.id), default=None)
    standing = cadence.status(person, last.occurred_on if last else None, window.tz)
    due_on = standing.due_on if standing and standing.due_on <= window.end else None
    return PersonContext(
        id=person.id,
        name=text(person.display_name, NAME_CHARS),
        aliases=[text(a.value, NAME_CHARS) for a in person.aliases],
        city=text(person.city, NAME_CHARS) if person.city else None,
        country=person.country,
        how_met=text(person.how_met) if person.how_met else None,
        birthday=BirthdayContext(
            month=person.birthday_month, day=person.birthday_day, year=person.birthday_year
        )
        if person.birthday_month is not None and person.birthday_day is not None
        else None,
        tags=[text(t.name, NAME_CHARS) for t in person.tags],
        handle_types=sorted({h.type.value for h in person.handles}),
        last_contact=LastContactContext(
            on=last.occurred_on,
            kind=last.kind.value,
            summary=text(last.summary) if last.summary else None,
        )
        if last
        else None,
        interactions=[
            InteractionContext(
                on=i.occurred_on, kind=i.kind.value, summary=text(i.summary) if i.summary else None
            )
            for i in person.interactions[:MAX_INTERACTIONS]
        ],
        notes=[
            NoteContext(on=n.created_at.astimezone(window.tz).date(), text=text(n.text))
            for n in person.notes[:MAX_NOTES]
        ],
        upcoming_dates=[
            DateContext(kind=u.kind, on=u.date, detail=text(u.detail, NAME_CHARS))
            for u in dates.person_upcoming(person, window.start, window.days)
        ],
        keep_in_touch_due_on=due_on,
    )


class PeopleProvider:
    name = "people"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> PeopleContext:
        query = select(Person).options(
            selectinload(Person.tags),
            selectinload(Person.handles),
            selectinload(Person.notes),
            selectinload(Person.aliases),
            selectinload(Person.interactions),
        )
        if focus is not None and focus.person_id is not None:
            query = query.where(Person.id == focus.person_id)
        else:
            if focus is not None and focus.activity_only:
                ids = _interacted(session, window) | _meeting_people(session, window)
            else:
                ids = _candidates(session, window)
            query = query.where(Person.id.in_(ids), Person.archived.is_(False))
        found = sorted(session.scalars(query), key=lambda p: (p.display_name.casefold(), p.id))
        return PeopleContext(
            people=[_person_context(session, p, window) for p in found[:MAX_PEOPLE]],
            truncated=len(found) > MAX_PEOPLE,
        )
