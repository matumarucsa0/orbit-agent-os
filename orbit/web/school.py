"""School pages (SPEC §2.20): the week, upcoming exams and deadlines (with the add form),
courses, a course page and the sync status. Plain forms and links, no JS."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from orbit.deps import DbSession, PushSenderDep, SchoolSourcesDep, SettingsDep
from orbit.domain.enums import FetchStatus, SchoolEventKind, SchoolEventSource
from orbit.domain.school import SchoolEvent
from orbit.integrations.sis import course_page_url
from orbit.services import school, school_sync
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.web.auth import require_login
from orbit.web.pages import day_label
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

SOURCE_LABELS = {
    SchoolEventSource.EXAM_FEED: "SIS exam",
    SchoolEventSource.EVENTS_FEED: "from SIS feed",
    SchoolEventSource.HARMONOGRAM: "SIS harmonogram",
    SchoolEventSource.RECODEX: "ReCodEx",
    SchoolEventSource.OWL: "Owl",
    SchoolEventSource.MANUAL: "added by you",
    SchoolEventSource.PROPOSAL: "from the Inbox",
    SchoolEventSource.OUTLOOK: "Outlook",
}
STATUS_LABELS = {
    FetchStatus.OK: "ok",
    FetchStatus.UNCHANGED: "no change",
    FetchStatus.EMPTY: "empty",
    FetchStatus.DENIED: "rejected",
    FetchStatus.ERROR: "failed",
}
FEED_LABELS = {
    "roz_externi": "Timetable (Rozvrh export)",
    "rozvrhng": "Timetable (ical.php)",
    "term_st2": "Exam terms",
    "stev": "SIS events",
    "course": "Course pages",
    "harmonogram": "Harmonogram",
    "recodex": "ReCodEx deadlines",
    "recodex_api": "ReCodEx points",
    "owl": "Owl",
}


def _common(request: Request, sources: school_sync.SchoolSources, tab: str) -> dict[str, object]:
    return {
        "tab": tab,
        "configured": sources.configured,
        "source_labels": SOURCE_LABELS,
        "score_labels": school.SCORE_LABELS,
        "today": today_local(request),
        "now": datetime.now(UTC),
    }


def _parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/school")
def week_page(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    sources: SchoolSourcesDep,
    week: str | None = None,
) -> Response:
    today = today_local(request)
    monday = school.week_start(_parse_day(week) or today)
    days = school.week(db, monday, settings.timezone)
    context = _common(request, sources, "week")
    context.update(
        {
            "days": [(day_label(d.day, today), d) for d in days],
            "monday": monday,
            "sunday": monday + timedelta(days=6),
            "prev": (monday - timedelta(days=7)).isoformat(),
            "next": (monday + timedelta(days=7)).isoformat(),
            "this_week": monday == school.week_start(today),
            "has_data": school.has_school_data(db),
            "empty": not any(d.lessons or d.events for d in days),
        }
    )
    return templates.TemplateResponse(request, "school/week.html", context)


@router.get("/school/upcoming")
def upcoming_page(request: Request, db: DbSession, sources: SchoolSourcesDep) -> Response:
    now = datetime.now(UTC)
    context = _common(request, sources, "upcoming")
    tz = request.app.state.settings.timezone
    items = [e for e in school.upcoming(db, now) if not school.is_past(e, now)]
    context.update(
        {
            "items": [(e, school.days_until(e.start_utc, now, tz)) for e in items],
            "courses": school.current_courses(db),
            "kinds": school.MANUAL_KINDS,
        }
    )
    return templates.TemplateResponse(request, "school/upcoming.html", context)


@router.post("/school/events")
def add_event(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    title: Annotated[str, Form()],
    day: Annotated[str, Form()],
    kind: Annotated[str, Form()] = "deadline",
    at: Annotated[str, Form()] = "",
    course_id: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Response:
    try:
        parsed_day = date.fromisoformat(day)
        clock = time.fromisoformat(at) if at.strip() else None
        data = school.ManualEventInput(
            title=title,
            kind=SchoolEventKind(kind),
            day=parsed_day,
            at=clock,
            course_id=int(course_id) if course_id.strip() else None,
            note=note,
        )
        event = school.create_event(db, data, settings.timezone)
    except (ValueError, InvalidInputError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, InvalidInputError) else "Check the date and time."
        flash(request, message, "error")
        return RedirectResponse("/school/upcoming#add", status_code=303)
    db.commit()
    flash(request, f"Added: {event.title}.")
    return RedirectResponse("/school/upcoming", status_code=303)


@router.post("/school/events/{event_id:int}/delete")
def delete_event(request: Request, db: DbSession, event_id: int) -> Response:
    try:
        school.delete_event(db, event_id)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/school/upcoming", status_code=303)
    db.commit()
    flash(request, "Deleted.")
    return RedirectResponse("/school/upcoming", status_code=303)


@router.get("/school/courses")
def courses_page(
    request: Request, db: DbSession, settings: SettingsDep, sources: SchoolSourcesDep
) -> Response:
    courses = school.current_courses(db)
    context = _common(request, sources, "courses")
    context.update(
        {
            "courses": courses,
            "scores": school.scores(db, [c.id for c in courses]),
            "next": school.next_lessons(db, datetime.now(UTC)),
            "credits": sum(c.credits or 0 for c in courses),
            "credits_missing": sum(c.credits is None for c in courses),
        }
    )
    return templates.TemplateResponse(request, "school/courses.html", context)


@router.get("/school/courses/{code}")
def course_page(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    sources: SchoolSourcesDep,
    code: str,
) -> Response:
    course = school.get_course(db, code)
    now = datetime.now(UTC)
    year = school.course_year(db, course, today_local(request))
    lessons = school.list_lessons(
        db,
        now - timedelta(hours=3),
        now + timedelta(days=school.UPCOMING_DAYS),
        course_id=course.id,
    )
    events: list[SchoolEvent] = school.list_events(
        db,
        now - timedelta(days=14),
        now + timedelta(days=365),
        kinds=(SchoolEventKind.EXAM, SchoolEventKind.DEADLINE, SchoolEventKind.OTHER),
        course_id=course.id,
    )
    owl = [o for o in school.owl_courses(db) if o.course_id == course.id]
    context = _common(request, sources, "courses")
    context.update(
        {
            "course": course,
            "lessons": lessons,
            "events": events,
            "scores": school.scores(db, [course.id]).get(course.id, []),
            "owl": owl,
            "sis_url": course_page_url(settings.sis_base_url, course.code, year),
        }
    )
    return templates.TemplateResponse(request, "school/course.html", context)


@router.get("/school/status")
def status_page(
    request: Request, db: DbSession, settings: SettingsDep, sources: SchoolSourcesDep
) -> Response:
    claims = sources.recodex.claims if sources.recodex is not None else None
    context = _common(request, sources, "status")
    context.update(
        {
            "feeds": school.fetch_status(db, school_sync.lesson_sources(sources)),
            "feed_labels": FEED_LABELS,
            "status_labels": STATUS_LABELS,
            "token_expires": claims.expires if claims else None,
            "owl_courses": school.owl_courses(db),
            "all_courses": school.current_courses(db),
            "sources": sources,
            "poll_minutes": settings.sis_poll_minutes,
        }
    )
    return templates.TemplateResponse(request, "school/status.html", context)


@router.post("/school/sync")
def sync_now(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    sources: SchoolSourcesDep,
    sender: PushSenderDep,
) -> Response:
    if not sources.configured:
        flash(request, "No school source is set up.", "error")
        return RedirectResponse("/school/status", status_code=303)
    report = school_sync.sync_school(db, sources, school_sync.config_from(settings), sender=sender)
    db.commit()
    kind = "error" if report.problems else "info"
    flash(request, f"Synced: {report.summary}.", kind)
    return RedirectResponse("/school/status", status_code=303)


@router.post("/school/owl/{owl_id:int}/link")
def link_owl(
    request: Request, db: DbSession, owl_id: int, course_id: Annotated[str, Form()] = ""
) -> Response:
    try:
        owl = school.link_owl_course(db, owl_id, int(course_id) if course_id.strip() else None)
    except (ValueError, InvalidInputError, NotFoundError) as exc:
        db.rollback()
        flash(request, str(exc) or "Pick a course.", "error")
        return RedirectResponse("/school/status#owl", status_code=303)
    db.commit()
    target = owl.course.code if owl.course else "no course"
    flash(request, f"{owl.name}: linked to {target}.")
    return RedirectResponse("/school/status#owl", status_code=303)
