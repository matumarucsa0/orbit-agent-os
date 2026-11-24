"""Goals: what I want, ranked. Callers commit.

Only active goals have a rank: 1 = most important, contiguous, never shared. Every change to
status or order goes through here so that holds. A goal links to people (many to many) and to
promises (a promise serves at most one goal). Goals are written only by me; automation may
propose a link (a capture's promise draft), never change a goal.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from orbit.domain.base import utcnow
from orbit.domain.enums import GoalHorizon, GoalStatus, PromiseStatus, ProposalKind
from orbit.domain.goals import Goal
from orbit.domain.ledger import Promise
from orbit.domain.normalize import normalize_text
from orbit.domain.people import Person
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.people import get_person
from orbit.services.proposal_kinds import Applied, ApplyContext, ProposalHandler

REVIEW_EVERY_DAYS = 7  # nudge when nothing was reviewed for this long
Move = Literal["up", "down", "top"]


class GoalUpdate(BaseModel):
    """PATCH semantics: only fields that were set are applied."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=200)
    why: str | None = Field(default=None, max_length=5000)
    horizon: GoalHorizon | None = None
    target: str | None = Field(default=None, max_length=1000)
    target_date: date | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class GoalCreate(GoalUpdate):
    title: str = Field(min_length=1, max_length=200)


def get_goal(session: Session, goal_id: int) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise NotFoundError(f"Goal {goal_id} not found.")
    return goal


def active_goals(session: Session) -> list[Goal]:
    """Active goals, most important first."""
    return list(
        session.scalars(
            select(Goal)
            .where(Goal.status == GoalStatus.ACTIVE)
            .options(selectinload(Goal.people), selectinload(Goal.promises))
            .order_by(Goal.rank, Goal.id)
        )
    )


def list_goals(session: Session, status: GoalStatus | None = None) -> list[Goal]:
    """Active goals by rank; with a status, goals of that status (closed ones newest first)."""
    if status is None or status is GoalStatus.ACTIVE:
        return active_goals(session)
    query = select(Goal).where(Goal.status == status)
    if status is GoalStatus.PAUSED:
        query = query.order_by(Goal.updated_at.desc(), Goal.id.desc())
    else:
        query = query.order_by(Goal.closed_at.desc(), Goal.id.desc())
    return list(session.scalars(query))


def inactive_goals(session: Session) -> list[Goal]:
    """Paused, then done, then dropped goals (for the collapsed list)."""
    order = {GoalStatus.PAUSED: 0, GoalStatus.DONE: 1, GoalStatus.DROPPED: 2}
    found = list(session.scalars(select(Goal).where(Goal.status != GoalStatus.ACTIVE)))
    found.sort(key=lambda g: (order[g.status], -(g.updated_at.timestamp()), -g.id))
    return found


def _check_title(session: Session, title: str, ignore_id: int | None = None) -> str:
    norm = normalize_text(title)
    if not norm:
        raise InvalidInputError("A goal needs a title.")
    query = select(Goal.id).where(Goal.status == GoalStatus.ACTIVE, Goal.title_norm == norm)
    if ignore_id is not None:
        query = query.where(Goal.id != ignore_id)
    if session.scalar(query.limit(1)) is not None:
        raise InvalidInputError(f"You already have an active goal called “{title}”.")
    return norm


def _next_rank(session: Session) -> int:
    return (session.scalar(select(func.max(Goal.rank))) or 0) + 1


def _renumber(session: Session) -> None:
    """Close gaps in the active ranks, keeping their order."""
    for rank, goal in enumerate(active_goals(session), start=1):
        if goal.rank != rank:
            goal.rank = rank
    session.flush()


def create_goal(session: Session, data: GoalCreate, now: datetime | None = None) -> Goal:
    norm = _check_title(session, data.title)
    goal = Goal(
        title=data.title,
        title_norm=norm,
        why=data.why,
        horizon=data.horizon,
        target=data.target,
        target_date=data.target_date,
        status=GoalStatus.ACTIVE,
        rank=_next_rank(session),
        reviewed_at=now or utcnow(),  # a new goal counts as looked at
    )
    session.add(goal)
    session.flush()
    return goal


def update_goal(session: Session, goal: Goal, data: GoalUpdate) -> Goal:
    fields = data.model_fields_set
    if "title" in fields:
        if not data.title:
            raise InvalidInputError("A goal needs a title.")
        if goal.status is GoalStatus.ACTIVE:
            goal.title_norm = _check_title(session, data.title, ignore_id=goal.id)
        else:
            goal.title_norm = normalize_text(data.title)
        goal.title = data.title
    for name in ("why", "horizon", "target", "target_date"):
        if name in fields:
            setattr(goal, name, getattr(data, name))
    session.flush()
    return goal


def set_status(
    session: Session, goal: Goal, status: GoalStatus, now: datetime | None = None
) -> Goal:
    """Pause, finish, drop or reactivate. A goal that becomes active goes last."""
    if status is goal.status:
        return goal
    moment = now or utcnow()
    if status is GoalStatus.ACTIVE:
        _check_title(session, goal.title, ignore_id=goal.id)
        rank = _next_rank(session)  # before the status changes: it queries (and autoflushes)
        goal.status = status
        goal.rank = rank
        goal.closed_at = None
    else:
        goal.status = status
        goal.rank = None
        goal.closed_at = moment if status in (GoalStatus.DONE, GoalStatus.DROPPED) else None
    session.flush()
    _renumber(session)
    return goal


def rank_goals(session: Session, ids: list[int], now: datetime | None = None) -> list[Goal]:
    """Set the order of the active goals. `ids` must be every active goal exactly once."""
    goals = {g.id: g for g in active_goals(session)}
    if len(ids) != len(set(ids)) or set(ids) != set(goals):
        raise InvalidInputError("Send every active goal exactly once.")
    moment = now or utcnow()
    for rank, goal_id in enumerate(ids, start=1):
        goal = goals[goal_id]
        if goal.rank != rank:
            goal.rank = rank
            goal.reviewed_at = moment
    session.flush()
    return active_goals(session)


def move(session: Session, goal: Goal, direction: Move, now: datetime | None = None) -> Goal:
    """One place up or down, or to the top. Counts as reviewing it."""
    if goal.status is not GoalStatus.ACTIVE:
        raise InvalidInputError("Only active goals have an order.")
    order = [g.id for g in active_goals(session)]
    index = order.index(goal.id)
    if direction == "top":
        target = 0
    elif direction == "up":
        target = max(index - 1, 0)
    else:
        target = min(index + 1, len(order) - 1)
    order.insert(target, order.pop(index))
    rank_goals(session, order, now)
    goal.reviewed_at = now or utcnow()
    session.flush()
    return goal


def review(session: Session, goal: Goal, now: datetime | None = None) -> Goal:
    """ "Still right": I looked at it and kept it."""
    goal.reviewed_at = now or utcnow()
    session.flush()
    return goal


def delete_goal(session: Session, goal: Goal) -> None:
    """Remove it; its promises stay, without a goal."""
    session.execute(update(Promise).where(Promise.goal_id == goal.id).values(goal_id=None))
    session.expire(goal, ["promises"])
    session.delete(goal)
    session.flush()
    _renumber(session)


# --- Links ---


def add_person(session: Session, goal: Goal, person_id: int) -> Goal:
    person = get_person(session, person_id)
    if person not in goal.people:
        goal.people.append(person)
        session.flush()
    return goal


def remove_person(session: Session, goal: Goal, person_id: int) -> Goal:
    goal.people[:] = [p for p in goal.people if p.id != person_id]
    session.flush()
    return goal


def attach_promise(session: Session, goal: Goal, promise_id: int) -> Promise:
    promise = session.get(Promise, promise_id)
    if promise is None:
        raise NotFoundError(f"Promise {promise_id} not found.")
    set_promise_goal(session, promise, goal.id)
    return promise


def detach_promise(session: Session, goal: Goal, promise_id: int) -> Promise:
    promise = session.get(Promise, promise_id)
    if promise is None or promise.goal_id != goal.id:
        raise NotFoundError(f"Promise {promise_id} isn't linked to this goal.")
    set_promise_goal(session, promise, None)
    return promise


def set_promise_goal(session: Session, promise: Promise, goal_id: int | None) -> None:
    """Point a promise at a goal (any status; it has to exist) or at none."""
    if goal_id is not None:
        get_goal(session, goal_id)
    promise.goal_id = goal_id
    session.flush()
    session.expire(promise, ["goal"])


def open_promises(goal: Goal) -> list[Promise]:
    found = [p for p in goal.promises if p.status is PromiseStatus.OPEN]
    found.sort(key=lambda p: (p.due is None, p.due, p.id))
    return found


def goals_of_person(session: Session, person_id: int) -> list[Goal]:
    """Active goals linked to a person, most important first."""
    return [g for g in active_goals(session) if any(p.id == person_id for p in g.people)]


# --- Review nudge ---


def days_since_review(goal: Goal, now: datetime) -> int:
    since = goal.reviewed_at or goal.created_at
    return max((now - since).days, 0)


def review_overdue_days(session: Session, now: datetime | None = None) -> int | None:
    """Days since any active goal was last reviewed, once that is a week or more; else None."""
    moment = now or utcnow()
    goals = active_goals(session)
    if not goals:
        return None
    days = min(days_since_review(g, moment) for g in goals)
    return days if days >= REVIEW_EVERY_DAYS else None


def goal_people_names(goal: Goal) -> list[str]:
    return [p.display_name for p in goal.people]


def people_for_picker(session: Session, goal: Goal) -> list[Person]:
    linked = {p.id for p in goal.people}
    everyone = session.scalars(
        select(Person).where(Person.archived.is_(False)).order_by(Person.search_name_norm)
    )
    return [p for p in everyone if p.id not in linked]


# --- Proposals from the weekly review (SPEC §2.17) ---


class GoalRerankPayload(BaseModel):
    """A new order for the active goals, made for the set of goals active at the time."""

    goal_ids: list[int] = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=1000)


class GoalLinkPayload(BaseModel):
    """Link an open promise (it then serves the goal) or a person to a goal."""

    goal_id: int
    promise_id: int | None = None
    person_id: int | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _one_target(self) -> GoalLinkPayload:
        if (self.promise_id is None) == (self.person_id is None):
            raise ValueError("Link either a promise or a person.")
        return self


def _confirm_rerank(session: Session, payload: GoalRerankPayload, ctx: ApplyContext) -> Applied:
    try:
        ranked = rank_goals(session, payload.goal_ids, ctx.now)
    except InvalidInputError as exc:
        raise InvalidInputError("Your active goals changed since this was suggested.") from exc
    return Applied("New order: " + "; ".join(f"{g.rank}. {g.title}" for g in ranked) + ".")


def _confirm_link(session: Session, payload: GoalLinkPayload, ctx: ApplyContext) -> Applied:
    try:
        goal = get_goal(session, payload.goal_id)
        if goal.status is not GoalStatus.ACTIVE:
            raise InvalidInputError(f"“{goal.title}” isn't an active goal any more.")
        if payload.promise_id is not None:
            promise = session.get(Promise, payload.promise_id)
            if promise is None or promise.status is not PromiseStatus.OPEN:
                raise InvalidInputError("That promise isn't open any more.")
            set_promise_goal(session, promise, goal.id)
            return Applied(f"“{promise.text}” now serves “{goal.title}”.", promise.person_id)
        assert payload.person_id is not None
        add_person(session, goal, payload.person_id)
        name = get_person(session, payload.person_id).display_name
    except NotFoundError as exc:
        raise InvalidInputError(str(exc)) from exc
    return Applied(f"{name} is now linked to “{goal.title}”.", payload.person_id)


GOAL_RERANK_HANDLER = ProposalHandler(ProposalKind.GOAL_RERANK, GoalRerankPayload, _confirm_rerank)
GOAL_LINK_HANDLER = ProposalHandler(ProposalKind.GOAL_LINK, GoalLinkPayload, _confirm_link)
