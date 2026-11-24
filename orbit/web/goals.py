"""Goals pages: the ranked list, a goal's detail, and the weekly review."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from orbit.deps import DbSession
from orbit.domain.enums import GoalHorizon, GoalStatus
from orbit.services import goals, promises
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

PICKER_PROMISES = 200
REVIEW_ACTIONS = {"keep", "up", "down", "pause", "done"}
STATUS_FLASH = {
    GoalStatus.ACTIVE: "Back in your goals, last in the order.",
    GoalStatus.PAUSED: "Paused.",
    GoalStatus.DONE: "Marked done.",
    GoalStatus.DROPPED: "Dropped.",
}


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _horizon(value: str) -> GoalHorizon | None:
    return GoalHorizon(value) if value else None


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise InvalidInputError(f"Not a date: {value}") from None


@router.get("/goals")
def goals_page(request: Request, db: DbSession) -> Response:
    now = datetime.now(UTC)
    return templates.TemplateResponse(
        request,
        "goals/list.html",
        {
            "active": goals.active_goals(db),
            "inactive": goals.inactive_goals(db),
            "horizons": list(GoalHorizon),
            "review_days": goals.review_overdue_days(db, now),
            "today": today_local(request),
            "open_promises": goals.open_promises,
        },
    )


@router.post("/goals")
def goal_add(
    request: Request,
    db: DbSession,
    title: Annotated[str, Form()],
    horizon: Annotated[str, Form()] = "",
) -> Response:
    try:
        goal = goals.create_goal(db, goals.GoalCreate(title=title, horizon=_horizon(horizon)))
    except (InvalidInputError, ValidationError, ValueError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, InvalidInputError) else "A goal needs a title."
        flash(request, message, "error")
        return _back("/goals")
    db.commit()
    flash(request, f"Added “{goal.title}” as number {goal.rank}.", link=f"/goals/{goal.id}")
    return _back("/goals")


@router.get("/goals/review")
def goals_review(request: Request, db: DbSession) -> Response:
    now = datetime.now(UTC)
    active = goals.active_goals(db)
    return templates.TemplateResponse(
        request,
        "goals/review.html",
        {
            "active": active,
            "days": {g.id: goals.days_since_review(g, now) for g in active},
            "today": today_local(request),
            "open_promises": goals.open_promises,
        },
    )


@router.post("/goals/review/{goal_id:int}")
def goals_review_step(
    request: Request, db: DbSession, goal_id: int, action: Annotated[str, Form()]
) -> Response:
    goal = goals.get_goal(db, goal_id)
    if action not in REVIEW_ACTIONS:
        flash(request, "Unknown action.", "error")
        return _back("/goals/review")
    try:
        if action == "keep":
            goals.review(db, goal)
        elif action in ("up", "down"):
            goals.move(db, goal, action)  # type: ignore[arg-type]
        elif action == "pause":
            goals.set_status(db, goal, GoalStatus.PAUSED)
        else:
            goals.set_status(db, goal, GoalStatus.DONE)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back("/goals/review")
    db.commit()
    return _back(
        f"/goals/review#goal-{goal_id}" if action in ("keep", "up", "down") else "/goals/review"
    )


@router.get("/goals/{goal_id:int}")
def goal_detail(request: Request, db: DbSession, goal_id: int) -> Response:
    goal = goals.get_goal(db, goal_id)
    offer = [p for p in promises.list_promises(db, limit=PICKER_PROMISES) if p.goal_id != goal.id]
    return templates.TemplateResponse(
        request,
        "goals/detail.html",
        {
            "goal": goal,
            "horizons": list(GoalHorizon),
            "statuses": [s for s in GoalStatus if s is not goal.status],
            "people_options": goals.people_for_picker(db, goal),
            "promise_options": offer,
            "open": goals.open_promises(goal),
            "closed": [p for p in goal.promises if p.status.value != "open"],
            "today": today_local(request),
            "days": goals.days_since_review(goal, datetime.now(UTC)),
        },
    )


@router.post("/goals/{goal_id:int}/move")
def goal_move(
    request: Request, db: DbSession, goal_id: int, direction: Annotated[str, Form()]
) -> Response:
    goal = goals.get_goal(db, goal_id)
    if direction not in ("up", "down", "top"):
        flash(request, "Unknown direction.", "error")
        return _back("/goals")
    try:
        goals.move(db, goal, direction)  # type: ignore[arg-type]
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back("/goals")
    db.commit()
    return _back(f"/goals#goal-{goal_id}")


@router.post("/goals/{goal_id:int}/edit")
def goal_edit(
    request: Request,
    db: DbSession,
    goal_id: int,
    title: Annotated[str, Form()],
    why: Annotated[str, Form()] = "",
    horizon: Annotated[str, Form()] = "",
    target: Annotated[str, Form()] = "",
    target_date: Annotated[str, Form()] = "",
) -> Response:
    goal = goals.get_goal(db, goal_id)
    url = f"/goals/{goal.id}"
    try:
        goals.update_goal(
            db,
            goal,
            goals.GoalUpdate(
                title=title,
                why=why,
                horizon=_horizon(horizon),
                target=target,
                target_date=_date(target_date),
            ),
        )
    except (InvalidInputError, ValidationError, ValueError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, InvalidInputError) else "Please check the fields."
        flash(request, message, "error")
        return _back(url)
    db.commit()
    flash(request, "Saved.")
    return _back(url)


@router.post("/goals/{goal_id:int}/status")
def goal_status(
    request: Request, db: DbSession, goal_id: int, status: Annotated[GoalStatus, Form()]
) -> Response:
    goal = goals.get_goal(db, goal_id)
    try:
        goals.set_status(db, goal, status)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(f"/goals/{goal.id}")
    db.commit()
    flash(request, STATUS_FLASH[status])
    return _back(f"/goals/{goal.id}")


@router.post("/goals/{goal_id:int}/review")
def goal_reviewed(request: Request, db: DbSession, goal_id: int) -> Response:
    goals.review(db, goals.get_goal(db, goal_id))
    db.commit()
    flash(request, "Marked as reviewed.")
    return _back(f"/goals/{goal_id}")


@router.post("/goals/{goal_id:int}/delete")
def goal_delete(request: Request, db: DbSession, goal_id: int) -> Response:
    goal = goals.get_goal(db, goal_id)
    title = goal.title
    goals.delete_goal(db, goal)
    db.commit()
    flash(request, f"Deleted “{title}”. Its promises and people stay.")
    return _back("/goals")


@router.post("/goals/{goal_id:int}/people")
def goal_person_add(
    request: Request, db: DbSession, goal_id: int, person_id: Annotated[int, Form()]
) -> Response:
    goal = goals.get_goal(db, goal_id)
    try:
        goals.add_person(db, goal, person_id)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(f"/goals/{goal_id}")
    db.commit()
    return _back(f"/goals/{goal_id}#people")


@router.post("/goals/{goal_id:int}/people/{person_id:int}/remove")
def goal_person_remove(db: DbSession, goal_id: int, person_id: int) -> Response:
    goals.remove_person(db, goals.get_goal(db, goal_id), person_id)
    db.commit()
    return _back(f"/goals/{goal_id}#people")


@router.post("/goals/{goal_id:int}/promises")
def goal_promise_add(
    request: Request, db: DbSession, goal_id: int, promise_id: Annotated[int, Form()]
) -> Response:
    goal = goals.get_goal(db, goal_id)
    goals.attach_promise(db, goal, promise_id)
    db.commit()
    return _back(f"/goals/{goal_id}#promises")


@router.post("/goals/{goal_id:int}/promises/{promise_id:int}/remove")
def goal_promise_remove(db: DbSession, goal_id: int, promise_id: int) -> Response:
    goals.detach_promise(db, goals.get_goal(db, goal_id), promise_id)
    db.commit()
    return _back(f"/goals/{goal_id}#promises")
