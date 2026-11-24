"""Report pages: the list, the new-report form, one report (SPEC §2.22)."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from orbit.deps import DbSession, PushSenderDep, ReportWriterDep, SettingsDep
from orbit.domain.enums import ReviewStatus
from orbit.domain.people import Person
from orbit.services import goals, report_run, reports, school
from orbit.services.context import ContextSnapshot
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])


def _int(value: str) -> int | None:
    return int(value) if value.strip().isdigit() else None


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@router.get("/reports")
def reports_page(request: Request, db: DbSession) -> Response:
    return templates.TemplateResponse(
        request, "reports/list.html", {"reports": reports.list_reports(db), "title": reports.title}
    )


@router.get("/reports/new")
def report_form(request: Request, db: DbSession, preset: str = "") -> Response:
    today = today_local(request)
    start, end = (
        reports.preset_range(preset, today) if preset in reports.PRESETS else (today, today)
    )
    people = db.scalars(
        select(Person).where(Person.archived.is_(False)).order_by(Person.search_name_norm)
    )
    return templates.TemplateResponse(
        request,
        "reports/new.html",
        {
            "start": start,
            "end": end,
            "presets": [(p, reports.PRESET_LABELS[p]) for p in reports.PRESETS],
            "goals": goals.active_goals(db),
            "people": list(people),
            "courses": school.current_courses(db),
            "max_days": reports.MAX_DAYS,
            "max_ahead": reports.MAX_AHEAD_DAYS,
        },
    )


@router.post("/reports")
def report_make(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    writer: ReportWriterDep,
    sender: PushSenderDep,
    background: BackgroundTasks,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    focus: Annotated[str, Form()] = "",
    goal_id: Annotated[str, Form()] = "",
    person_id: Annotated[str, Form()] = "",
    course_id: Annotated[str, Form()] = "",
) -> Response:
    first, last = _date(start), _date(end)
    if first is None or last is None:
        flash(request, "Pick a start and an end date.", "error")
        return RedirectResponse("/reports/new", status_code=303)
    data = reports.ReportInput(first, last, focus, _int(goal_id), _int(person_id), _int(course_id))
    try:
        started = report_run.start(db, data, settings=settings, writer=writer)
        db.commit()
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/reports/new", status_code=303)
    if started.mode == "api" and writer is not None:
        background.add_task(
            report_run.run_api,
            request.app.state.sessionmaker,
            started.report_id,
            writer,
            sender,
            settings,
        )
    return RedirectResponse(f"/reports/{started.report_id}", status_code=303)


@router.get("/reports/{report_id:int}")
def report_page(request: Request, db: DbSession, report_id: int) -> Response:
    row = reports.get_report(db, report_id)
    output = row.output_json or {}
    plain = []
    if row.status is ReviewStatus.READY and not row.claude_used and row.input_json:
        snapshot = ContextSnapshot.model_validate(row.input_json["snapshot"])
        plain = reports.plain_sections(snapshot)
    names = reports.lookup_names(db, output)
    return templates.TemplateResponse(
        request,
        "reports/detail.html",
        {
            "report": row,
            "output": output,
            "names": names,
            "plain": plain,
            "title": reports.title(row),
            "markdown": reports.to_markdown(row, names) if row.status is ReviewStatus.READY else "",
            "input_json": json.dumps(row.input_json, ensure_ascii=False, indent=2)
            if row.input_json
            else "",
            "pending": row.status is ReviewStatus.PENDING,
        },
    )


@router.post("/reports/{report_id:int}/retry")
def report_retry(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    writer: ReportWriterDep,
    sender: PushSenderDep,
    background: BackgroundTasks,
    report_id: int,
) -> Response:
    try:
        started = report_run.retry(db, report_id, settings=settings, writer=writer)
        db.commit()
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/reports/{report_id}", status_code=303)
    if started.mode == "api" and writer is not None:
        background.add_task(
            report_run.run_api,
            request.app.state.sessionmaker,
            started.report_id,
            writer,
            sender,
            settings,
        )
    return RedirectResponse(f"/reports/{report_id}", status_code=303)


@router.post("/reports/{report_id:int}/delete")
def report_delete(request: Request, db: DbSession, report_id: int) -> Response:
    reports.delete_report(db, report_id)
    db.commit()
    flash(request, "Report deleted.", "success")
    return RedirectResponse("/reports", status_code=303)
