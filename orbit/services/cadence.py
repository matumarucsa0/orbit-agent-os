"""Keep-in-touch cadence: who I haven't talked to for longer than I'd like.

A tag can say "every N days" (close = 30); a person's own cadence overrides their tags', and with
several tags the tightest one counts. Someone is due once N days have passed since the last
interaction (any kind), or since they were added when there's none. Snoozing someone pushes
their due day out to a chosen date. Shown on Home and in the Sunday digest; there's no push for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.people import Interaction, Person, Tag, interaction_person
from orbit.services.errors import InvalidInputError, NotFoundError

MAX_CADENCE_DAYS = 3650
MAX_SNOOZE_DAYS = 365


def effective_cadence(person: Person) -> int | None:
    """The person's own cadence, else the tightest of their tags', else None."""
    if person.cadence_days is not None:
        return person.cadence_days
    tagged = [t.cadence_days for t in person.tags if t.cadence_days is not None]
    return min(tagged) if tagged else None


@dataclass(frozen=True)
class KeepInTouch:
    person: Person
    cadence_days: int
    last_contact: date | None  # the last interaction; None if there has never been one
    due_on: date

    def overdue_days(self, today: date) -> int:
        return (today - self.due_on).days


def last_contacts(session: Session, person_ids: list[int]) -> dict[int, date]:
    """The date of each person's latest interaction (people without any are left out)."""
    if not person_ids:
        return {}
    rows = session.execute(
        select(interaction_person.c.person_id, func.max(Interaction.occurred_on))
        .join(Interaction, Interaction.id == interaction_person.c.interaction_id)
        .where(interaction_person.c.person_id.in_(person_ids))
        .group_by(interaction_person.c.person_id)
    ).all()
    return {person_id: last for person_id, last in rows}


def status(person: Person, last_contact: date | None, tz: ZoneInfo) -> KeepInTouch | None:
    """Where `person` stands, or None when no cadence applies to them."""
    cadence = effective_cadence(person)
    if cadence is None:
        return None
    since = last_contact or person.created_at.astimezone(tz).date()
    due_on = since + timedelta(days=cadence)
    if person.snoozed_until is not None and person.snoozed_until > due_on:
        due_on = person.snoozed_until
    return KeepInTouch(person, cadence, last_contact, due_on)


def due(session: Session, today: date, tz: ZoneInfo) -> list[KeepInTouch]:
    """Non-archived people whose cadence has run out by `today`, most overdue first."""
    tagged = select(Person.id).join(Person.tags).where(Tag.cadence_days.is_not(None))
    people = list(
        session.scalars(
            select(Person)
            .where(
                Person.archived.is_(False),
                Person.cadence_days.is_not(None) | Person.id.in_(tagged),
            )
            .options(selectinload(Person.tags))
        )
    )
    last = last_contacts(session, [p.id for p in people])
    found = [s for p in people if (s := status(p, last.get(p.id), tz)) and s.due_on <= today]
    found.sort(key=lambda s: (s.due_on, s.person.display_name.casefold(), s.person.id))
    return found


def snooze(person: Person, today: date, days: int) -> date:
    """Don't list `person` as due before `today + days`; returns that day."""
    if not 1 <= days <= MAX_SNOOZE_DAYS:
        raise InvalidInputError(f"Snooze for 1 to {MAX_SNOOZE_DAYS} days.")
    person.snoozed_until = today + timedelta(days=days)
    return person.snoozed_until


def set_tag_cadence(session: Session, tag_id: int, days: int | None) -> Tag:
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise NotFoundError(f"Tag {tag_id} not found.")
    if days is not None and not 1 <= days <= MAX_CADENCE_DAYS:
        raise InvalidInputError(f"Keep-in-touch days must be between 1 and {MAX_CADENCE_DAYS}.")
    tag.cadence_days = days
    session.flush()
    return tag
