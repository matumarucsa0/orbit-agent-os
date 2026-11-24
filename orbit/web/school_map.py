"""The school map page (Phase 18c, SPEC §2.20): the data flow map, its JSON, the side panel and
the forms behind it (add a resource, keep or dismiss a suggestion, check now, remove)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from orbit.deps import DbSession, SchoolSourcesDep, SettingsDep
from orbit.domain.enums import ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.domain.school import Course, CourseFact
from orbit.integrations import resources as web_resources
from orbit.services import school, school_map, school_reader, school_resources
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

PROCESS_TEXT = {
    "proc:sync": (
        "Sync",
        "Reads SIS, ReCodEx and Owl every hour.",
        "/school/status",
        "Sync status",
    ),
    "proc:watcher": (
        "Page watcher",
        "Fetches each kept page or PDF once a day, keeps the old text and shows what changed.",
        "/settings/schedule#jobs",
        "Schedule",
    ),
    "proc:reader": (
        "Claude reader",
        "Reads new or changed text and proposes deadlines and course rules. "
        "It never fetches anything itself.",
        "/ops/claude",
        "Claude worker",
    ),
    "proc:inbox": (
        "Inbox",
        "What the reader found waits here until you confirm.",
        "/inbox",
        "Open the Inbox",
    ),
    "out:week": ("Week view", "Your lessons and deadlines.", "/school", "Open the week"),
    "out:deadlines": (
        "Deadlines and pushes",
        "Exam and deadline reminders.",
        "/school/upcoming",
        "Open upcoming",
    ),
    "out:credits": (
        "Credit progress",
        "Points toward credit from ReCodEx and Owl.",
        "/school/courses",
        "Open courses",
    ),
    "out:review": (
        "Weekly review",
        "The review reads the school module, rules and resources included.",
        "/reviews",
        "Open reviews",
    ),
}
SYSTEM_TEXT = {
    "sys:sis": ("SIS", "Timetable, exams and the harmonogram, through the calendar feeds."),
    "sys:recodex": ("ReCodEx", "Programming assignments: deadlines and points."),
    "sys:owl": ("Owl", "Course topics, deadlines, points and the course rules text."),
}


def _course(db: DbSession, code: str | None) -> Course | None:
    return school.get_course(db, code) if code else None


def _back(select_id: str | None = None) -> RedirectResponse:
    return RedirectResponse(
        "/school/map" + (f"?select={select_id}" if select_id else ""), status_code=303
    )


@router.get("/school/map")
def map_page(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    sources: SchoolSourcesDep,
    view: str = "",
) -> Response:
    resources = school_resources.list_resources(db)
    courses = school.current_courses(db)
    code_of = {c.id: c.code for c in courses}
    groups: dict[str | None, list[object]] = {}
    for r in resources:
        groups.setdefault(code_of.get(r.course_id) if r.course_id else None, []).append(r)
    now = datetime.now(UTC)
    return templates.TemplateResponse(
        request,
        "school/map.html",
        {
            "tab": "map",
            "select": request.query_params.get("select", ""),
            "view": view,
            "courses": courses,
            "groups": [(c, groups.get(c.code, [])) for c in courses]
            + [(None, groups.get(None, []))],
            "state": lambda r: school_map.resource_state(r, now, settings.timezone),
            "state_labels": school_map.STATE_LABELS,
            "kind_icons": school_map.KIND_ICONS,
            "today": today_local(request),
            "configured": sources.configured,
        },
    )


@router.get("/school/map.json")
def map_json(db: DbSession, settings: SettingsDep, sources: SchoolSourcesDep) -> JSONResponse:
    built = school_map.build_map(
        db, sources, datetime.now(UTC), settings.timezone, settings.school_reads_per_day
    )
    return JSONResponse(built.as_json())


@router.get("/school/map/panel")
@router.get("/school/map/panel/{node_id}")
def map_panel(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    node_id: str = "",
) -> Response:
    context: dict[str, object] = {
        "node_id": node_id,
        "state_labels": school_map.STATE_LABELS,
        "kind_icons": school_map.KIND_ICONS,
        "origin_labels": school_map.ORIGIN_LABELS,
        "reads": school_reader.reads_today(db, datetime.now(UTC), settings.timezone),
        "budget": settings.school_reads_per_day,
        "now": datetime.now(UTC),
        "tz": settings.timezone,
        "state": lambda r: school_map.resource_state(r, datetime.now(UTC), settings.timezone),
    }
    kind, _, key = node_id.partition(":")
    if kind == "course":
        course = _course(db, key)
        if course is None:
            raise NotFoundError("Course not found.")
        context.update(
            {
                "panel": "course",
                "course": course,
                "facts": list(
                    db.scalars(
                        select(CourseFact)
                        .where(CourseFact.course_id == course.id)
                        .order_by(CourseFact.id)
                    )
                ),
                "resources": school_resources.list_resources(db, course.id),
                "waiting": _pending(db, course.id),
            }
        )
    elif kind == "res":
        resource = school_resources.get_resource(db, int(key))
        context.update(
            {
                "panel": "resource",
                "resource": resource,
                "preview": school_resources.preview(resource),
            }
        )
    elif node_id in SYSTEM_TEXT:
        context.update({"panel": "system", "info": SYSTEM_TEXT[node_id]})
    elif node_id in PROCESS_TEXT:
        context.update({"panel": "process", "info": PROCESS_TEXT[node_id]})
    else:
        context.update(
            {
                "panel": "overview",
                "suggestions": [r for r in school_resources.list_resources(db) if not r.kept],
                "waiting": _pending(db, None, everything=True),
                "unsorted": school_resources.list_resources(db, None)
                and [
                    r for r in school_resources.list_resources(db) if r.course_id is None and r.kept
                ],
            }
        )
    return templates.TemplateResponse(request, "school/_map_panel.html", context)


def _pending(db: DbSession, course_id: int | None, *, everything: bool = False) -> int:
    count = 0
    for payload in db.scalars(
        select(Proposal.payload_json).where(
            Proposal.status == ProposalStatus.PENDING,
            Proposal.kind.in_((ProposalKind.SCHOOL_DEADLINE, ProposalKind.COURSE_FACT)),
        )
    ):
        if everything or payload.get("course_id") == course_id:
            count += 1
    return count


@router.post("/school/map/resources")
async def add_resource(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    course_code: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Add a link, a PDF or a note (the kind is detected from what was given)."""
    course = _course(db, course_code or None)
    course_id = course.id if course else None
    node = f"course:{course.code}" if course else None
    try:
        if file is not None and file.filename:
            data = await file.read(school_resources.MAX_FILE_BYTES + 1)
            row = school_resources.add_file(
                db, data, file.filename, course_id, settings.school_files_dir
            )
        elif url.strip():
            row = school_resources.add_url(db, url, course_id, label=label)
        elif note.strip():
            row = school_resources.add_note(db, note, course_id, label)
        else:
            raise InvalidInputError("Give a link, a PDF or a note.")
        db.commit()
    except (InvalidInputError, NotFoundError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(node)
    flash(request, f"Added: {row.label}. " + _next_step(row.kind.value), "success")
    return _back(f"res:{row.id}")


def _next_step(kind: str) -> str:
    if kind == "login":
        return "It is behind a login, so Orbit lists it and never fetches it."
    if kind in ("note", "file"):
        return "Claude reads it with the next daily run, or press Check now."
    return "It is fetched with the next daily run, or press Check now."


def _act(
    request: Request, db: DbSession, resource_id: int, fn: object, done: str, after: str | None
) -> Response:
    try:
        fn(db, resource_id)  # type: ignore[operator]
        db.commit()
        flash(request, done, "success")
    except (InvalidInputError, NotFoundError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back(after)


@router.post("/school/map/resources/{resource_id:int}/keep")
def keep(request: Request, db: DbSession, resource_id: int) -> Response:
    return _act(
        request,
        db,
        resource_id,
        school_resources.keep,
        "Kept: it is fetched from the next run on.",
        f"res:{resource_id}",
    )


@router.post("/school/map/resources/{resource_id:int}/dismiss")
def dismiss(request: Request, db: DbSession, resource_id: int) -> Response:
    return _act(request, db, resource_id, school_resources.dismiss, "Dismissed.", None)


@router.post("/school/map/resources/{resource_id:int}/remove")
def remove(request: Request, db: DbSession, settings: SettingsDep, resource_id: int) -> Response:
    try:
        school_resources.remove(db, resource_id, settings.school_files_dir)
        db.commit()
        flash(request, "Removed.", "success")
    except NotFoundError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back(None)


@router.post("/school/map/resources/{resource_id:int}/course")
def link(
    request: Request, db: DbSession, resource_id: int, course_code: Annotated[str, Form()] = ""
) -> Response:
    try:
        course = _course(db, course_code or None)
        school_resources.link_course(db, resource_id, course.id if course else None)
        db.commit()
        flash(request, "Linked.", "success")
    except (InvalidInputError, NotFoundError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back(f"res:{resource_id}")


@router.post("/school/map/resources/{resource_id:int}/check")
def check_now(request: Request, db: DbSession, settings: SettingsDep, resource_id: int) -> Response:
    jobs = request.app.state.jobs
    try:
        message = school_reader.check_now(
            db,
            resource_id,
            jobs.fetcher or web_resources.HttpFetcher(),
            jobs.claude,
            datetime.now(UTC),
            settings.timezone,
            settings.school_reads_per_day,
        )
        db.commit()
        flash(request, f"Checked: {message}.", "success")
    except (InvalidInputError, NotFoundError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back(f"res:{resource_id}")


@router.post("/school/map/facts/{fact_id:int}/delete")
def delete_fact(request: Request, db: DbSession, fact_id: int) -> Response:
    fact = db.get(CourseFact, fact_id)
    node = f"course:{fact.course.code}" if fact is not None else None
    try:
        school_resources.delete_fact(db, fact_id)
        db.commit()
        flash(request, "Rule removed.", "success")
    except NotFoundError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _back(node)
