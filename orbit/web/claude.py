"""The Claude worker's page: is it running, logged in, rate limited; the queue and recent jobs."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from orbit.deps import DbSession, SettingsDep
from orbit.integrations.pricing import format_eur
from orbit.services import budget, claude_jobs, usage
from orbit.services.claude_jobs import QueueLimits
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/ops/claude")
def claude_page(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    today = datetime.now(UTC).astimezone(settings.timezone).date()
    rows = usage.totals(db, usage.months_back(today, 2), today, settings.timezone)
    month_costs: dict[str, int] = {}
    for row in rows:
        key = row.month.strftime("%Y-%m")
        month_costs[key] = month_costs.get(key, 0) + row.cost_micro_eur
    return templates.TemplateResponse(
        request,
        "ops/claude.html",
        {
            "backend": settings.resolved_claude_backend,
            "usage": rows,
            "month_costs": month_costs,
            "budget": budget.state(db, settings.claude_monthly_budget_eur, settings.timezone),
            "format_eur": format_eur,
            "format_tokens": usage.format_tokens,
            "status": claude_jobs.status_json(db),
            "jobs": claude_jobs.recent_jobs(db),
        },
    )


@router.post("/ops/claude/jobs/{job_id:int}/rerun")
def claude_rerun(request: Request, db: DbSession, settings: SettingsDep, job_id: int) -> Response:
    limits = QueueLimits(
        wait_interactive=settings.claude_wait_interactive_seconds,
        wait_background=settings.claude_wait_background_seconds,
    )
    try:
        claude_jobs.rerun(db, job_id, limits)
        db.commit()
        flash(request, f"Job {job_id} is queued again.", "success")
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/ops/claude", status_code=303)
