from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, status

from orbit.api.schemas import (
    MeetingLinkIn,
    MeetingOut,
    MeetingPersonOut,
    SyncOut,
)
from orbit.deps import CalendarSourceDep, DbSession, MeetingLinkerDep
from orbit.domain.meetings import Meeting
from orbit.integrations.caldav_client import CalDavError
from orbit.services import meetings

router = APIRouter(tags=["meetings"])


def meeting_out(meeting: Meeting) -> MeetingOut:
    sources = {link.person_id: link.link_source for link in meeting.links}
    return MeetingOut(
        id=meeting.id,
        calendar_name=meeting.calendar_name,
        title=meeting.title,
        location=meeting.location,
        start_utc=meeting.start_utc,
        end_utc=meeting.end_utc,
        cancelled=meeting.cancelled,
        brief_sent_at=meeting.brief_sent_at,
        capture_sent_at=meeting.capture_sent_at,
        people=[
            MeetingPersonOut(id=p.id, display_name=p.display_name, link_source=sources[p.id])
            for p in meeting.people
        ],
    )


@router.get("/meetings", response_model=list[MeetingOut])
def list_meetings(
    db: DbSession,
    start: datetime | None = None,
    end: datetime | None = None,
    person_id: int | None = None,
    include_cancelled: bool = False,
) -> list[MeetingOut]:
    """Meetings overlapping [start, end); default: from now for the next 14 days."""
    start = (start or datetime.now(UTC)).astimezone(UTC)
    end = (end or start + timedelta(days=meetings.SYNC_DAYS)).astimezone(UTC)
    found = meetings.list_meetings(
        db, start, end, include_cancelled=include_cancelled, person_id=person_id
    )
    return [meeting_out(m) for m in found]


@router.get("/meetings/{meeting_id}", response_model=MeetingOut)
def get_meeting(meeting_id: int, db: DbSession) -> MeetingOut:
    return meeting_out(meetings.get_meeting(db, meeting_id))


@router.post("/meetings/{meeting_id}/people", response_model=MeetingOut)
def link_person(meeting_id: int, body: MeetingLinkIn, db: DbSession) -> MeetingOut:
    """Link a person by hand; kept on every sync."""
    meeting = meetings.link_person(db, meetings.get_meeting(db, meeting_id), body.person_id)
    db.commit()
    return meeting_out(meeting)


@router.delete("/meetings/{meeting_id}/people/{person_id}", response_model=MeetingOut)
def unlink_person(meeting_id: int, person_id: int, db: DbSession) -> MeetingOut:
    meeting = meetings.unlink_person(db, meetings.get_meeting(db, meeting_id), person_id)
    db.commit()
    return meeting_out(meeting)


@router.post("/meetings/sync", response_model=SyncOut)
def sync(db: DbSession, source: CalendarSourceDep, linker: MeetingLinkerDep) -> SyncOut:
    """Sync the calendar now (the scheduler does it every CALDAV_POLL_MINUTES)."""
    if source is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The calendar isn't configured (CALDAV_USERNAME, CALDAV_PASSWORD, CALDAV_CALENDARS).",
        )
    try:
        report = meetings.sync_meetings(db, source, linker=linker)
    except CalDavError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    db.commit()
    return SyncOut(
        new=report.new,
        updated=report.updated,
        cancelled=report.cancelled,
        calendars=report.calendars,
        missing=report.missing,
        summary=report.summary,
    )
