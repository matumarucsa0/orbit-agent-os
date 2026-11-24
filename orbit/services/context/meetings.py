"""Meetings: calendar events in the window and the people they are with.

Cancelled meetings are left out. With a person focus, only meetings that person is linked to.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.services.context.base import (
    NAME_CHARS,
    ContextFocus,
    ContextWindow,
    ModuleContext,
    text,
)

MAX_MEETINGS = 300


class MeetingPersonContext(BaseModel):
    id: int
    name: str


class MeetingContext(BaseModel):
    id: int
    title: str
    start: datetime  # local time
    end: datetime
    location: str | None
    people: list[MeetingPersonContext]


class MeetingsContext(ModuleContext):
    trimmable: ClassVar[tuple[str, ...]] = ("meetings",)
    module: Literal["meetings"] = "meetings"
    meetings: list[MeetingContext]


class MeetingsProvider:
    name = "meetings"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> MeetingsContext:
        query = (
            select(Meeting)
            .where(
                Meeting.cancelled.is_(False),
                Meeting.start_utc >= window.start_utc,
                Meeting.start_utc < window.end_utc,
            )
            .options(selectinload(Meeting.links).selectinload(MeetingPerson.person))
            .order_by(Meeting.start_utc, Meeting.id)
        )
        if focus is not None and focus.person_id is not None:
            query = query.where(
                Meeting.id.in_(
                    select(MeetingPerson.meeting_id).where(
                        MeetingPerson.person_id == focus.person_id
                    )
                )
            )
        found = list(session.scalars(query.limit(MAX_MEETINGS + 1)))
        return MeetingsContext(
            meetings=[
                MeetingContext(
                    id=m.id,
                    title=text(m.title),
                    start=m.start_utc.astimezone(window.tz),
                    end=m.end_utc.astimezone(window.tz),
                    location=text(m.location) if m.location else None,
                    people=[
                        MeetingPersonContext(id=p.id, name=text(p.display_name, NAME_CHARS))
                        for p in m.people
                    ],
                )
                for m in found[:MAX_MEETINGS]
            ],
            truncated=len(found) > MAX_MEETINGS,
        )
