"""Interactions (meet / call / message / other) with one or more people."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import InteractionKind
from orbit.domain.people import Interaction, Person, interaction_person
from orbit.services.errors import InvalidInputError, NotFoundError


class InteractionUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    occurred_on: date | None = None
    kind: InteractionKind | None = None
    summary: str | None = None
    person_ids: list[int] | None = None

    @field_validator("summary", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class InteractionCreate(InteractionUpdate):
    occurred_on: date
    kind: InteractionKind = InteractionKind.MEET
    person_ids: list[int] = Field(min_length=1)


def _load_people(session: Session, person_ids: list[int]) -> list[Person]:
    unique_ids = list(dict.fromkeys(person_ids))
    if not unique_ids:
        raise InvalidInputError("An interaction needs at least one person.")
    people = list(session.scalars(select(Person).where(Person.id.in_(unique_ids))))
    missing = set(unique_ids) - {p.id for p in people}
    if missing:
        raise NotFoundError(f"People not found: {sorted(missing)}")
    return people


def get_interaction(session: Session, interaction_id: int) -> Interaction:
    """The interaction with this id; raises NotFoundError."""
    interaction = session.get(Interaction, interaction_id)
    if interaction is None:
        raise NotFoundError(f"Interaction {interaction_id} not found.")
    return interaction


def create_interaction(session: Session, data: InteractionCreate) -> Interaction:
    """Record a meet, call, message or other interaction with one or more people."""
    interaction = Interaction(
        occurred_on=data.occurred_on,
        kind=data.kind,
        summary=data.summary,
        people=_load_people(session, data.person_ids),
    )
    session.add(interaction)
    session.flush()
    return interaction


def update_interaction(
    session: Session, interaction: Interaction, data: InteractionUpdate
) -> Interaction:
    """Edit an interaction (PATCH semantics)."""
    fields = data.model_fields_set
    if "occurred_on" in fields:
        if data.occurred_on is None:
            raise InvalidInputError("An interaction needs a date.")
        interaction.occurred_on = data.occurred_on
    if "kind" in fields:
        if data.kind is None:
            raise InvalidInputError("An interaction needs a kind.")
        interaction.kind = data.kind
    if "summary" in fields:
        interaction.summary = data.summary
    if "person_ids" in fields:
        interaction.people = _load_people(session, data.person_ids or [])
    session.flush()
    return interaction


def delete_interaction(session: Session, interaction: Interaction) -> None:
    """Delete an interaction."""
    linked = list(interaction.people)
    session.delete(interaction)
    session.flush()
    for person in linked:
        session.expire(person, ["interactions"])


def list_interactions(
    session: Session, person_id: int | None = None, limit: int = 100, offset: int = 0
) -> list[Interaction]:
    """Interactions, newest first, optionally only those with one person; paged by `limit` and
    `offset`."""
    query = select(Interaction).order_by(Interaction.occurred_on.desc(), Interaction.id.desc())
    if person_id is not None:
        query = query.join(
            interaction_person, interaction_person.c.interaction_id == Interaction.id
        ).where(interaction_person.c.person_id == person_id)
    return list(session.scalars(query.limit(limit).offset(offset)))


def last_interaction(session: Session, person_id: int) -> Interaction | None:
    """The most recent interaction with a person, or None."""
    found = list_interactions(session, person_id=person_id, limit=1)
    return found[0] if found else None
