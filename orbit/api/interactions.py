from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from orbit.api.schemas import InteractionOut
from orbit.deps import DbSession
from orbit.domain.people import Interaction
from orbit.services import interactions

router = APIRouter(tags=["interactions"])


@router.get("/interactions", response_model=list[InteractionOut])
def list_interactions(
    db: DbSession,
    person_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Interaction]:
    return interactions.list_interactions(db, person_id=person_id, limit=limit, offset=offset)


@router.post("/interactions", response_model=InteractionOut, status_code=status.HTTP_201_CREATED)
def create_interaction(body: interactions.InteractionCreate, db: DbSession) -> Interaction:
    interaction = interactions.create_interaction(db, body)
    db.commit()
    return interaction


@router.patch("/interactions/{interaction_id}", response_model=InteractionOut)
def update_interaction(
    interaction_id: int, body: interactions.InteractionUpdate, db: DbSession
) -> Interaction:
    interaction = interactions.update_interaction(
        db, interactions.get_interaction(db, interaction_id), body
    )
    db.commit()
    return interaction


@router.delete("/interactions/{interaction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_interaction(interaction_id: int, db: DbSession) -> None:
    interactions.delete_interaction(db, interactions.get_interaction(db, interaction_id))
    db.commit()
