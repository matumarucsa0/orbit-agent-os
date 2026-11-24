"""Promises between me and a person (either direction), with a due date and a status.
Callers commit."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import PromiseDirection, PromiseStatus, ProposalKind
from orbit.domain.ledger import Promise
from orbit.services import goals
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.people import get_person
from orbit.services.proposal_kinds import Applied, ApplyContext, ProposalHandler

DEFAULT_DUE_DAYS = 7


class PromiseUpdate(BaseModel):
    """PATCH semantics: only fields that were set are applied."""

    model_config = ConfigDict(str_strip_whitespace=True)

    direction: PromiseDirection | None = None
    text: str | None = Field(default=None, max_length=2000)
    due: date | None = None
    status: PromiseStatus | None = None
    goal_id: int | None = None  # set to None to clear

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class PromiseCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    person_id: int
    direction: PromiseDirection
    text: str = Field(min_length=1, max_length=2000)
    # Left out (or blank) -> today + 7 days.
    due: date | None = None
    goal_id: int | None = None  # the goal it serves, if any

    @field_validator("due", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


def get_promise(session: Session, promise_id: int) -> Promise:
    """The promise with this id; raises NotFoundError."""
    promise = session.get(Promise, promise_id)
    if promise is None:
        raise NotFoundError(f"Promise {promise_id} not found.")
    return promise


def create_promise(session: Session, data: PromiseCreate, today: date) -> Promise:
    """Create a promise between me and a person (either direction), optionally with a due date and a
    goal."""
    person = get_person(session, data.person_id)
    if data.goal_id is not None:
        goals.get_goal(session, data.goal_id)
    promise = Promise(
        goal_id=data.goal_id,
        direction=data.direction,
        text=data.text,
        due=data.due or today + timedelta(days=DEFAULT_DUE_DAYS),
        status=PromiseStatus.OPEN,
    )
    person.promises.append(promise)
    session.flush()
    return promise


def set_status(promise: Promise, status: PromiseStatus, now: datetime | None = None) -> None:
    """open -> done / dropped stamps closed_at; back to open (reopen) clears it."""
    if status is promise.status:
        return
    promise.status = status
    promise.closed_at = None if status is PromiseStatus.OPEN else (now or utcnow())


def update_promise(
    session: Session, promise: Promise, data: PromiseUpdate, now: datetime | None = None
) -> Promise:
    """Edit a promise (PATCH semantics); only the fields that were set change."""
    fields = data.model_fields_set
    if "direction" in fields:
        if data.direction is None:
            raise InvalidInputError("A promise needs a direction.")
        promise.direction = data.direction
    if "text" in fields:
        if not data.text:
            raise InvalidInputError("A promise needs some text.")
        promise.text = data.text
    if "due" in fields:
        promise.due = data.due
    if "status" in fields:
        if data.status is None:
            raise InvalidInputError("A promise needs a status.")
        set_status(promise, data.status, now)
    if "goal_id" in fields:
        goals.set_promise_goal(session, promise, data.goal_id)
    session.flush()
    return promise


def delete_promise(session: Session, promise: Promise) -> None:
    """Delete a promise."""
    promise.person.promises.remove(promise)  # delete-orphan cascade deletes the row
    session.flush()


def list_promises(
    session: Session,
    person_id: int | None = None,
    status: PromiseStatus | None = PromiseStatus.OPEN,
    direction: PromiseDirection | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[Promise]:
    """Open promises by due date (undated last); closed ones most recently closed first;
    all of them (status=None) newest first."""
    query = select(Promise)
    if person_id is not None:
        query = query.where(Promise.person_id == person_id)
    if status is not None:
        query = query.where(Promise.status == status)
    if direction is not None:
        query = query.where(Promise.direction == direction)
    if status is PromiseStatus.OPEN:
        query = query.order_by(Promise.due.is_(None), Promise.due, Promise.id)
    elif status is not None:
        query = query.order_by(Promise.closed_at.desc(), Promise.id.desc())
    else:
        query = query.order_by(Promise.created_at.desc(), Promise.id.desc())
    return list(session.scalars(query.limit(limit).offset(offset)))


def is_overdue(promise: Promise, today: date) -> bool:
    """True for an open promise whose due date is before `today`."""
    return promise.status is PromiseStatus.OPEN and promise.due is not None and promise.due < today


# --- Proposals from the weekly review (SPEC §2.17) ---


class FollowUpPayload(BaseModel):
    """A to-do about a person: confirming adds an open promise "I promised"."""

    person_id: int
    text: str = Field(min_length=1, max_length=2000)
    due: date | None = None  # default: a week out
    goal_id: int | None = None
    reason: str | None = Field(default=None, max_length=1000)


class PromiseDuePayload(BaseModel):
    promise_id: int
    due: date
    reason: str | None = Field(default=None, max_length=1000)


def _confirm_follow_up(session: Session, payload: FollowUpPayload, ctx: ApplyContext) -> Applied:
    try:
        person = get_person(session, payload.person_id)
        promise = create_promise(
            session,
            PromiseCreate(
                person_id=payload.person_id,
                direction=PromiseDirection.I_PROMISED,
                text=payload.text,
                due=payload.due,
                goal_id=payload.goal_id,
            ),
            ctx.today,
        )
    except NotFoundError as exc:
        raise InvalidInputError(str(exc)) from exc
    return Applied(f"Added a promise for {person.display_name}: {promise.text}.", person.id)


def _confirm_promise_due(
    session: Session, payload: PromiseDuePayload, ctx: ApplyContext
) -> Applied:
    promise = session.get(Promise, payload.promise_id)
    if promise is None:
        raise InvalidInputError("That promise no longer exists.")
    if promise.status is not PromiseStatus.OPEN:
        raise InvalidInputError(f"That promise is already {promise.status.value}.")
    promise.due = payload.due
    session.flush()
    return Applied(
        f"“{promise.text}” is now due {payload.due.day} {payload.due:%b}.", promise.person_id
    )


FOLLOW_UP_HANDLER = ProposalHandler(ProposalKind.FOLLOW_UP, FollowUpPayload, _confirm_follow_up)
PROMISE_DUE_HANDLER = ProposalHandler(
    ProposalKind.PROMISE_DUE, PromiseDuePayload, _confirm_promise_due
)
