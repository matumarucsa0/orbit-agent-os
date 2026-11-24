from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status

from orbit.api.schemas import GoalOut, GoalPersonIn, GoalPromiseIn, GoalRankIn
from orbit.deps import DbSession
from orbit.domain.enums import GoalStatus
from orbit.domain.goals import Goal
from orbit.services import goals
from orbit.services.errors import InvalidInputError

router = APIRouter(tags=["goals"])


class GoalPatch(goals.GoalUpdate):
    status: GoalStatus | None = None


@router.get("/goals", response_model=list[GoalOut])
def list_goals(
    db: DbSession, status: GoalStatus | Literal["all"] = GoalStatus.ACTIVE
) -> list[Goal]:
    """Active goals by rank (default); a status lists goals of that status; `all`: active first."""
    if status == "all":
        return goals.active_goals(db) + goals.inactive_goals(db)
    return goals.list_goals(db, GoalStatus(status))


@router.post("/goals", response_model=GoalOut, status_code=status.HTTP_201_CREATED)
def create_goal(body: goals.GoalCreate, db: DbSession) -> Goal:
    goal = goals.create_goal(db, body)
    db.commit()
    return goal


@router.post("/goals/rank", response_model=list[GoalOut])
def rank_goals(body: GoalRankIn, db: DbSession) -> list[Goal]:
    ranked = goals.rank_goals(db, body.ids)
    db.commit()
    return ranked


@router.get("/goals/{goal_id}", response_model=GoalOut)
def get_goal(goal_id: int, db: DbSession) -> Goal:
    return goals.get_goal(db, goal_id)


@router.patch("/goals/{goal_id}", response_model=GoalOut)
def update_goal(goal_id: int, body: GoalPatch, db: DbSession) -> Goal:
    goal = goals.get_goal(db, goal_id)
    fields = body.model_fields_set
    goals.update_goal(db, goal, goals.GoalUpdate(**body.model_dump(include=fields - {"status"})))
    if "status" in fields:
        if body.status is None:
            raise InvalidInputError("A goal needs a status.")
        goals.set_status(db, goal, body.status)
    db.commit()
    return goal


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(goal_id: int, db: DbSession) -> None:
    goals.delete_goal(db, goals.get_goal(db, goal_id))
    db.commit()


@router.post("/goals/{goal_id}/review", response_model=GoalOut)
def review_goal(goal_id: int, db: DbSession) -> Goal:
    goal = goals.review(db, goals.get_goal(db, goal_id))
    db.commit()
    return goal


@router.post("/goals/{goal_id}/people", response_model=GoalOut)
def add_person(goal_id: int, body: GoalPersonIn, db: DbSession) -> Goal:
    goal = goals.add_person(db, goals.get_goal(db, goal_id), body.person_id)
    db.commit()
    return goal


@router.delete("/goals/{goal_id}/people/{person_id}", response_model=GoalOut)
def remove_person(goal_id: int, person_id: int, db: DbSession) -> Goal:
    goal = goals.remove_person(db, goals.get_goal(db, goal_id), person_id)
    db.commit()
    return goal


@router.post("/goals/{goal_id}/promises", response_model=GoalOut)
def attach_promise(goal_id: int, body: GoalPromiseIn, db: DbSession) -> Goal:
    goal = goals.get_goal(db, goal_id)
    goals.attach_promise(db, goal, body.promise_id)
    db.commit()
    return goal


@router.delete("/goals/{goal_id}/promises/{promise_id}", response_model=GoalOut)
def detach_promise(goal_id: int, promise_id: int, db: DbSession) -> Goal:
    goal = goals.get_goal(db, goal_id)
    goals.detach_promise(db, goal, promise_id)
    db.commit()
    return goal
