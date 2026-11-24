"""School JSON API (SPEC §2.20): lessons, courses, exams and deadlines, sync and its status.
Lessons and feed items are read-only here; only manual items can be added, changed or deleted."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status

from orbit.api.schemas import (
    CourseDetailOut,
    CourseOut,
    CourseRefOut,
    CourseScoreOut,
    FeedStatusOut,
    FetchOut,
    LessonOut,
    SchoolEventIn,
    SchoolEventOut,
    SchoolEventPatch,
    SchoolStatusOut,
    SchoolSyncOut,
)
from orbit.deps import DbSession, PushSenderDep, SchoolSourcesDep, SettingsDep
from orbit.domain.enums import SchoolEventKind
from orbit.domain.school import Course, CourseScore, Lesson, SchoolEvent
from orbit.integrations.sis import course_page_url
from orbit.services import school, school_sync
from orbit.services.errors import InvalidInputError
from orbit.settings import Settings

router = APIRouter(prefix="/school", tags=["school"])


def _ref(course: Course | None) -> CourseRefOut | None:
    return CourseRefOut.model_validate(course) if course is not None else None


def lesson_out(lesson: Lesson) -> LessonOut:
    return LessonOut.model_validate(lesson)


def event_out(event: SchoolEvent) -> SchoolEventOut:
    return SchoolEventOut(
        id=event.id,
        source=event.source,
        kind=event.kind,
        title=event.title,
        description=event.description,
        location=event.location,
        url=event.url,
        course=_ref(event.course),
        start_utc=event.start_utc,
        end_utc=event.end_utc,
        second_deadline_utc=event.second_deadline_utc,
        points=float(event.points) if event.points is not None else None,
        max_points=float(event.max_points) if event.max_points is not None else None,
        all_day=event.all_day,
        cancelled=event.cancelled,
    )


def course_out(
    course: Course,
    settings: Settings,
    year: int,
    *,
    next_lesson: Lesson | None = None,
    scores: list[CourseScore] | None = None,
) -> CourseOut:
    return CourseOut(
        id=course.id,
        code=course.code,
        name=course.name,
        name_en=course.name_en,
        credits=course.credits,
        hours_lecture=course.hours_lecture,
        hours_seminar=course.hours_seminar,
        completion=course.completion,
        semester=course.semester,
        department=course.department,
        guarantor=course.guarantor,
        language=course.language,
        web_url=course.web_url,
        sis_url=course_page_url(settings.sis_base_url, course.code, year),
        detail_fetched_at=course.detail_fetched_at,
        next_lesson_utc=next_lesson.start_utc if next_lesson else None,
        scores=[
            CourseScoreOut(
                source=s.source,
                gained=float(s.gained),
                total=float(s.total),
                pass_threshold=float(s.pass_threshold) if s.pass_threshold is not None else None,
                passes=s.passes,
                fetched_at=s.fetched_at,
            )
            for s in scores or []
        ],
    )


def _today(settings: Settings) -> date:
    return datetime.now(UTC).astimezone(settings.timezone).date()


@router.get("/lessons", response_model=list[LessonOut])
def list_lessons(
    db: DbSession,
    settings: SettingsDep,
    start: datetime | None = None,
    end: datetime | None = None,
    course: str | None = None,
) -> list[LessonOut]:
    """Lessons starting in [start, end); default: this week (Monday to Sunday)."""
    monday = school.week_start(_today(settings))
    lo, hi = school.local_bounds(monday, monday + timedelta(days=6), settings.timezone)
    course_id = school.get_course(db, course).id if course else None
    found = school.list_lessons(
        db,
        start.astimezone(UTC) if start else lo,
        end.astimezone(UTC) if end else hi,
        course_id=course_id,
    )
    return [lesson_out(lesson) for lesson in found]


@router.get("/courses", response_model=list[CourseOut])
def list_courses(db: DbSession, settings: SettingsDep) -> list[CourseOut]:
    """This semester's courses (a lesson in the newest semester of the feed)."""
    courses = school.current_courses(db)
    upcoming = school.next_lessons(db, datetime.now(UTC))
    scores = school.scores(db, [c.id for c in courses])
    today = _today(settings)
    return [
        course_out(
            c,
            settings,
            school.course_year(db, c, today),
            next_lesson=upcoming.get(c.id),
            scores=scores.get(c.id, []),
        )
        for c in courses
    ]


@router.get("/courses/{code}", response_model=CourseDetailOut)
def get_course(code: str, db: DbSession, settings: SettingsDep) -> CourseDetailOut:
    course = school.get_course(db, code)
    now = datetime.now(UTC)
    lessons = school.list_lessons(
        db,
        now - timedelta(hours=3),
        now + timedelta(days=school.UPCOMING_DAYS),
        course_id=course.id,
    )
    events = school.list_events(
        db, now - timedelta(days=14), now + timedelta(days=365), course_id=course.id
    )
    upcoming = school.next_lessons(db, now)
    return CourseDetailOut(
        course=course_out(
            course,
            settings,
            school.course_year(db, course, _today(settings)),
            next_lesson=upcoming.get(course.id),
            scores=school.scores(db, [course.id]).get(course.id, []),
        ),
        lessons=[lesson_out(lesson) for lesson in lessons],
        events=[event_out(e) for e in events],
    )


@router.get("/events", response_model=list[SchoolEventOut])
def list_events(
    db: DbSession,
    start: datetime | None = None,
    end: datetime | None = None,
    kind: SchoolEventKind | None = None,
    include_cancelled: bool = False,
) -> list[SchoolEventOut]:
    """Exams, deadlines, terms and other items starting in [start, end); default: from a day
    ago for the next 60 days."""
    now = datetime.now(UTC)
    found = school.list_events(
        db,
        start.astimezone(UTC) if start else now - timedelta(days=1),
        end.astimezone(UTC) if end else now + timedelta(days=school.UPCOMING_DAYS),
        kinds=(kind,) if kind else None,
        include_cancelled=include_cancelled,
    )
    return [event_out(e) for e in found]


def _clock(value: str | None) -> time | None:
    if value is None:
        return None
    hours, minutes = (int(part) for part in value.split(":"))
    if hours > 23 or minutes > 59:
        raise InvalidInputError("Time must be HH:MM.")
    return time(hours, minutes)


def _course_id(db: DbSession, code: str | None) -> int | None:
    if not code or not code.strip():
        return None
    return school.get_course(db, code).id


@router.post("/events", response_model=SchoolEventOut, status_code=status.HTTP_201_CREATED)
def create_event(body: SchoolEventIn, db: DbSession, settings: SettingsDep) -> SchoolEventOut:
    """Add an exam, deadline or other item by hand (SIS doesn't know assignments)."""
    data = school.ManualEventInput(
        title=body.title,
        kind=body.kind,
        day=body.day,
        at=_clock(body.at),
        course_id=_course_id(db, body.course_code),
        note=body.note,
    )
    event = school.create_event(db, data, settings.timezone)
    db.commit()
    db.refresh(event)
    return event_out(event)


@router.patch("/events/{event_id}", response_model=SchoolEventOut)
def update_event(
    event_id: int, body: SchoolEventPatch, db: DbSession, settings: SettingsDep
) -> SchoolEventOut:
    """Change a manual item; fields left out stay as they are (`at: null` makes it all day)."""
    event = school.get_event(db, event_id)
    tz = settings.timezone
    fields = body.model_fields_set
    local = event.start_utc.astimezone(tz)
    current: dict[str, Any] = {
        "title": event.title,
        "kind": event.kind,
        "day": local.date(),
        "at": None if event.all_day else local.time().replace(second=0, microsecond=0),
        "course_id": event.course_id,
        "note": event.description,
    }
    if "title" in fields and body.title is not None:
        current["title"] = body.title
    if "kind" in fields and body.kind is not None:
        current["kind"] = body.kind
    if "day" in fields and body.day is not None:
        current["day"] = body.day
    if "at" in fields:
        current["at"] = _clock(body.at)
    if "course_code" in fields:
        current["course_id"] = _course_id(db, body.course_code)
    if "note" in fields:
        current["note"] = body.note
    event = school.update_event(db, event_id, school.ManualEventInput(**current), tz)
    db.commit()
    db.refresh(event)
    return event_out(event)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(event_id: int, db: DbSession) -> Response:
    school.delete_event(db, event_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sync", response_model=SchoolSyncOut)
def sync(
    db: DbSession, sources: SchoolSourcesDep, sender: PushSenderDep, settings: SettingsDep
) -> SchoolSyncOut:
    """Read SIS, ReCodEx and Owl now (the scheduler does it every SIS_POLL_MINUTES)."""
    if not sources.configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No school source is set up (SIS_ICAL_TOKEN, SIS_TIMETABLE_KEY, RECODEX_*, "
            "OWL_SESSION_COOKIE).",
        )
    report = school_sync.sync_school(db, sources, school_sync.config_from(settings), sender=sender)
    db.commit()
    return SchoolSyncOut(
        summary=report.summary,
        results=[
            {
                "source": r.source.value,
                "status": r.status.value,
                "seen": r.seen,
                "changed": r.changed,
                "error": r.error,
            }
            for r in report.results
        ],
    )


@router.get("/status", response_model=SchoolStatusOut)
def sync_status(db: DbSession, sources: SchoolSourcesDep) -> SchoolStatusOut:
    """The last fetch of each source. Never shows a token."""
    claims = sources.recodex.claims if sources.recodex is not None else None
    feeds = school.fetch_status(db, school_sync.lesson_sources(sources))
    return SchoolStatusOut(
        configured=sources.configured,
        sis=sources.sis is not None and bool(sources.sis.feeds),
        recodex_feed=sources.recodex is not None and sources.recodex.has_feed,
        recodex_api=claims is not None,
        owl=sources.owl is not None,
        recodex_token_expires=claims.expires if claims else None,
        feeds=[
            FeedStatusOut(
                source=f.source,
                last=FetchOut.model_validate(f.last) if f.last else None,
                last_ok_at=f.last_ok.started_at if f.last_ok else None,
            )
            for f in feeds
        ],
    )
