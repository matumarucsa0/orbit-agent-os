"""Meetings pages: upcoming meetings, the brief (`/meetings/{id}`), linking people by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from orbit.deps import CalendarSourceDep, DbSession, MeetingLinkerDep, SettingsDep
from orbit.domain.ledger import Promise
from orbit.domain.meetings import Meeting
from orbit.domain.people import Interaction, Note
from orbit.integrations.caldav_client import CalDavError
from orbit.services import meetings, search
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login
from orbit.web.pages import day_label
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

PAST_DAYS = 7
SEARCH_LIMIT = 8
SOURCE_LABELS = {
    "attendee": "invited",
    "title": "from the title",
    "claude": "picked by Claude",
    "manual": "linked by you",
}


def _url(meeting_id: int) -> str:
    return f"/meetings/{meeting_id}"


def _by_day(found: list[Meeting], request: Request, today: date) -> list[tuple[str, list[Meeting]]]:
    tz = request.app.state.settings.timezone
    return [
        (day_label(day, today), list(entries))
        for day, entries in groupby(found, key=lambda m: m.start_utc.astimezone(tz).date())
    ]


@router.get("/meetings")
def meetings_page(request: Request, db: DbSession, source: CalendarSourceDep) -> Response:
    now = datetime.now(UTC)
    today = today_local(request)
    upcoming = meetings.list_meetings(db, now, now + timedelta(days=meetings.SYNC_DAYS))
    earlier = meetings.list_meetings(db, now - timedelta(days=PAST_DAYS), now)
    earlier = [m for m in earlier if m not in upcoming]  # in progress: listed as upcoming
    return templates.TemplateResponse(
        request,
        "meetings/list.html",
        {
            "groups": _by_day(upcoming, request, today),
            "earlier": list(reversed(earlier)),
            "configured": source is not None,
        },
    )


@router.post("/meetings/sync")
def meetings_sync(
    request: Request, db: DbSession, source: CalendarSourceDep, linker: MeetingLinkerDep
) -> Response:
    if source is None:
        flash(request, "The calendar isn't configured.", "error")
        return RedirectResponse("/meetings", status_code=303)
    try:
        report = meetings.sync_meetings(db, source, linker=linker)
    except CalDavError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/meetings", status_code=303)
    db.commit()
    flash(request, f"Synced: {report.summary}.")
    return RedirectResponse("/meetings", status_code=303)


def _from_meeting(db: DbSession, meeting_id: int) -> dict[str, Any]:
    """What the post-meeting captures of this meeting created."""
    return {
        "meeting_notes": list(
            db.scalars(select(Note).where(Note.meeting_id == meeting_id).order_by(Note.id))
        ),
        "meeting_promises": list(
            db.scalars(select(Promise).where(Promise.meeting_id == meeting_id).order_by(Promise.id))
        ),
        "meeting_interaction": db.scalars(
            select(Interaction).where(Interaction.meeting_id == meeting_id).limit(1)
        ).first(),
    }


@router.get("/meetings/{meeting_id:int}")
def meeting_page(
    request: Request, db: DbSession, settings: SettingsDep, meeting_id: int
) -> Response:
    meeting = meetings.get_meeting(db, meeting_id)
    brief = meetings.build_brief(db, meeting, settings.timezone)
    sources = {link.person_id: SOURCE_LABELS[link.link_source.value] for link in meeting.links}
    context: dict[str, Any] = {
        "meeting": meeting,
        "brief": brief,
        "sources": sources,
        "today": today_local(request),
        "ended": meeting.end_utc <= datetime.now(UTC),
        "thing_line": meetings.thing_line,
        "date_line": meetings.date_line,
    }
    context.update(_from_meeting(db, meeting.id))
    return templates.TemplateResponse(request, "meetings/detail.html", context)


@router.get("/meetings/{meeting_id:int}/people-search")
def people_search(request: Request, db: DbSession, meeting_id: int, q: str = "") -> Response:
    """HTMX fragment: people matching `q` who aren't linked yet."""
    meeting = meetings.get_meeting(db, meeting_id)
    linked = {link.person_id for link in meeting.links}
    found = search.search_people(db, q, limit=SEARCH_LIMIT + len(linked)) if q.strip() else []
    return templates.TemplateResponse(
        request,
        "meetings/_search.html",
        {
            "meeting": meeting,
            "people": [p for p in found if p.id not in linked][:SEARCH_LIMIT],
            "q": q,
        },
    )


@router.post("/meetings/{meeting_id:int}/cancel")
def appointment_cancel(request: Request, db: DbSession, meeting_id: int) -> Response:
    try:
        meetings.cancel_appointment(db, meetings.get_meeting(db, meeting_id))
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(_url(meeting_id), status_code=303)
    db.commit()
    flash(request, "Appointment cancelled.")
    return RedirectResponse("/meetings", status_code=303)


@router.post("/meetings/{meeting_id:int}/people")
def person_link(
    request: Request, db: DbSession, meeting_id: int, person_id: Annotated[int, Form()]
) -> Response:
    meeting = meetings.link_person(db, meetings.get_meeting(db, meeting_id), person_id)
    db.commit()
    return RedirectResponse(_url(meeting.id), status_code=303)


@router.post("/meetings/{meeting_id:int}/people/{person_id:int}/delete")
def person_unlink(request: Request, db: DbSession, meeting_id: int, person_id: int) -> Response:
    try:
        meetings.unlink_person(db, meetings.get_meeting(db, meeting_id), person_id)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(_url(meeting_id), status_code=303)
    db.commit()
    return RedirectResponse(_url(meeting_id), status_code=303)
