"""Promises: what I promised and what people promised me, open ones only.

Soonest due first, undated last. With a person focus, that person's.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from orbit.domain.enums import PromiseDirection, PromiseStatus
from orbit.domain.ledger import Promise
from orbit.services import promises
from orbit.services.context.base import (
    NAME_CHARS,
    ContextFocus,
    ContextWindow,
    ModuleContext,
    text,
)

MAX_PROMISES = 500
MAX_CLOSED = 100
CLOSED_DAYS = 30  # done or dropped this recently is history worth reading


class PromiseContext(BaseModel):
    id: int
    person_id: int
    person: str
    direction: PromiseDirection
    text: str
    due: date | None
    overdue: bool  # due before the window starts
    due_in_window: bool
    status: PromiseStatus = PromiseStatus.OPEN
    closed_on: date | None = None


class PromisesContext(ModuleContext):
    trimmable: ClassVar[tuple[str, ...]] = ("promises", "recently_closed")
    module: Literal["promises"] = "promises"
    promises: list[PromiseContext]  # open
    recently_closed: list[PromiseContext]  # done or dropped in the last 30 days, newest first


def _promise_context(promise: Promise, window: ContextWindow) -> PromiseContext:
    return PromiseContext(
        id=promise.id,
        person_id=promise.person_id,
        person=text(promise.person.display_name, NAME_CHARS),
        direction=promise.direction,
        text=text(promise.text),
        due=promise.due,
        overdue=promise.due is not None and promise.due < window.start,
        due_in_window=promise.due is not None and window.contains(promise.due),
        status=promise.status,
        closed_on=promise.closed_at.astimezone(window.tz).date() if promise.closed_at else None,
    )


class PromisesProvider:
    name = "promises"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> PromisesContext:
        person_id = focus.person_id if focus is not None else None
        found = promises.list_promises(
            session, person_id=person_id, status=PromiseStatus.OPEN, limit=MAX_PROMISES + 1
        )
        closed = [
            p
            for p in promises.list_promises(
                session, person_id=person_id, status=None, limit=MAX_PROMISES + MAX_CLOSED + 1
            )
            if p.closed_at is not None
            and p.closed_at >= window.start_utc - timedelta(days=CLOSED_DAYS)
        ]
        closed.sort(key=lambda p: (p.closed_at or window.start_utc, p.id), reverse=True)
        return PromisesContext(
            promises=[_promise_context(p, window) for p in found[:MAX_PROMISES]],
            recently_closed=[_promise_context(p, window) for p in closed[:MAX_CLOSED]],
            truncated=len(found) > MAX_PROMISES or len(closed) > MAX_CLOSED,
        )
