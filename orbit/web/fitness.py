"""Fitness pages: the Liftoff workouts, one workout, and "Read now" (SPEC §2.35)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from orbit.deps import DbSession
from orbit.jobs import scheduler as jobs
from orbit.services import fitness
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates

router = APIRouter(dependencies=[Depends(require_login)])


def kg(grams: int | None) -> str:
    return "" if grams is None else f"{grams / 1000:.1f}".removesuffix(".0")


def minutes(seconds: int) -> str:
    hours, rest = divmod(round(seconds / 60), 60)
    return f"{hours} h {rest:02d} min" if hours else f"{rest} min"


templates.env.filters["kg"] = kg
templates.env.filters["duration"] = minutes


@router.get("/fitness")
def fitness_page(request: Request, db: DbSession) -> Response:
    workouts = fitness.recent_workouts(db)
    spec_status = next(
        (s for s in jobs.job_statuses(request.app.state.jobs) if s.spec.id == "liftoff_sync"),
        None,
    )
    return templates.TemplateResponse(
        request,
        "fitness/list.html",
        {
            "workouts": workouts,
            "total": fitness.total_workouts(db),
            "status": spec_status,
        },
    )


@router.post("/fitness/sync")
def fitness_sync(request: Request) -> Response:
    spec = jobs.job_spec("liftoff_sync")
    result = jobs.execute(request.app.state.jobs, spec, manual=True)
    flash(request, f"{spec.label}: {result.message}", "success" if result.ok else "error")
    return RedirectResponse("/fitness", status_code=303)


@router.get("/fitness/{workout_id:int}")
def workout_page(request: Request, db: DbSession, workout_id: int) -> Response:
    return templates.TemplateResponse(
        request, "fitness/detail.html", {"workout": fitness.get_workout(db, workout_id)}
    )
