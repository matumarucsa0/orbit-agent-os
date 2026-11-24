"""The schedule page: edit when things run, and run a job now (SPEC §2.21)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from orbit.deps import DbSession
from orbit.jobs import scheduler as jobs
from orbit.services import schedule_config
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates

router = APIRouter(dependencies=[Depends(require_login)])


def _anchor(group: str) -> str:
    return "/settings/schedule#" + group.lower().replace(" ", "-")


def _applied(request: Request) -> None:
    request.app.state.overrides.invalidate()
    jobs.sync_intervals(request.app.state.jobs)


@router.get("/settings/schedule")
def schedule_page(request: Request, db: DbSession) -> Response:
    rows = schedule_config.rows(db, request.app.state.settings)
    groups = [(g, [r for r in rows if r.definition.group == g]) for g in schedule_config.GROUPS]
    changes = [
        (c, schedule_config.definition(c.key))
        for c in schedule_config.recent_changes(db)
        if c.key in {d.key for d in schedule_config.DEFINITIONS}
    ]
    return templates.TemplateResponse(
        request,
        "ops/schedule.html",
        {
            "groups": groups,
            "jobs": jobs.job_statuses(request.app.state.jobs),
            "external": schedule_config.EXTERNAL_ROWS,
            "changes": changes,
            "display": schedule_config.display,
            "slug": lambda group: group.lower().replace(" ", "-"),
            "scheduler_on": request.app.state.jobs.scheduler is not None,
        },
    )


@router.post("/settings/schedule")
async def schedule_save(request: Request, db: DbSession) -> Response:
    form = await request.form()
    group = str(form.get("group", ""))
    if group not in schedule_config.GROUPS:
        flash(request, "Unknown group.", "error")
        return RedirectResponse("/settings/schedule", status_code=303)
    values: dict[str, object] = {}
    for defn in schedule_config.DEFINITIONS:
        if defn.group == group and defn.key in form:
            # A checkbox sends "off" (hidden) and then "on" (ticked): the last one counts.
            values[defn.key] = str(form.getlist(defn.key)[-1])
    try:
        changed = schedule_config.save(db, request.app.state.settings, values)
        db.commit()
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(_anchor(group), status_code=303)
    _applied(request)
    flash(request, f"Saved {len(changed)} change(s)." if changed else "Nothing changed.", "success")
    return RedirectResponse(_anchor(group), status_code=303)


@router.post("/settings/schedule/{key}/reset")
def schedule_reset(request: Request, db: DbSession, key: str) -> Response:
    try:
        defn = schedule_config.definition(key)
        schedule_config.reset(db, key)
        db.commit()
    except NotFoundError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/settings/schedule", status_code=303)
    _applied(request)
    flash(request, f"{defn.label}: back to the value from .env.", "success")
    return RedirectResponse(_anchor(defn.group), status_code=303)


@router.post("/settings/schedule/jobs/{job_id}/run")
def schedule_run(request: Request, job_id: str) -> Response:
    try:
        spec = jobs.job_spec(job_id)
    except NotFoundError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/settings/schedule", status_code=303)
    if not spec.run_now:
        flash(request, f"{spec.label} has no Run now.", "error")
        return RedirectResponse("/settings/schedule#jobs", status_code=303)
    result = jobs.execute(request.app.state.jobs, spec, manual=True)
    kind = "success" if result.ok else "error"
    flash(request, f"{spec.label}: {result.message}", kind)
    return RedirectResponse("/settings/schedule#jobs", status_code=303)
