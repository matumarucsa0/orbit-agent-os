"""Goals: what I want, in order. The spine the other modules are weighed against.

Active goals by rank, with their linked people and open promises. With a person focus, only
goals linked to that person. The budget layer never trims this module.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from orbit.domain.enums import GoalHorizon
from orbit.domain.goals import Goal
from orbit.services import goals
from orbit.services.context.base import (
    NAME_CHARS,
    ContextFocus,
    ContextWindow,
    ModuleContext,
    text,
)

MAX_GOALS = 50
MAX_LINKED_PEOPLE = 20
MAX_LINKED_PROMISES = 50


class GoalPersonContext(BaseModel):
    id: int
    name: str


class GoalPromiseContext(BaseModel):
    id: int
    person_id: int
    person: str
    text: str
    due: date | None
    overdue: bool


class GoalContext(BaseModel):
    id: int
    rank: int
    title: str
    why: str | None
    horizon: GoalHorizon | None
    target: str | None
    target_date: date | None
    overdue: bool  # the target date is before the window starts
    due_in_window: bool
    days_since_review: int
    people: list[GoalPersonContext]
    open_promises: list[GoalPromiseContext]


class GoalsContext(ModuleContext):
    module: Literal["goals"] = "goals"
    goals: list[GoalContext]


def _goal_context(goal: Goal, window: ContextWindow) -> GoalContext:
    reviewed = (goal.reviewed_at or goal.created_at).astimezone(window.tz).date()
    return GoalContext(
        id=goal.id,
        rank=goal.rank or 0,
        title=text(goal.title, NAME_CHARS),
        why=text(goal.why) if goal.why else None,
        horizon=goal.horizon,
        target=text(goal.target) if goal.target else None,
        target_date=goal.target_date,
        overdue=goal.target_date is not None and goal.target_date < window.start,
        due_in_window=goal.target_date is not None and window.contains(goal.target_date),
        days_since_review=max((window.start - reviewed).days, 0),
        people=[
            GoalPersonContext(id=p.id, name=text(p.display_name, NAME_CHARS))
            for p in goal.people[:MAX_LINKED_PEOPLE]
        ],
        open_promises=[
            GoalPromiseContext(
                id=p.id,
                person_id=p.person_id,
                person=text(p.person.display_name, NAME_CHARS),
                text=text(p.text),
                due=p.due,
                overdue=p.due is not None and p.due < window.start,
            )
            for p in goals.open_promises(goal)[:MAX_LINKED_PROMISES]
        ],
    )


class GoalsProvider:
    name = "goals"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> GoalsContext:
        found = goals.active_goals(session)
        if focus is not None and focus.person_id is not None:
            found = [g for g in found if any(p.id == focus.person_id for p in g.people)]
        return GoalsContext(
            goals=[_goal_context(g, window) for g in found[:MAX_GOALS]],
            truncated=len(found) > MAX_GOALS,
        )
