"""Weekly review pages: the list, one review, and the "Generate now" button."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from orbit.deps import DbSession, PushSenderDep, SettingsDep, WeeklyReviewerDep
from orbit.domain.goals import Goal
from orbit.domain.ledger import Promise
from orbit.domain.meetings import Meeting
from orbit.domain.people import Person
from orbit.services import notifications, review
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login
from orbit.web.inbox import KIND_LABELS
from orbit.web.templating import flash, templates

router = APIRouter(dependencies=[Depends(require_login)])


def _ids(output: dict[str, Any], key: str) -> set[int]:
    """Every id under `key` (e.g. "goal_ids") in wins, slips and conflicts."""
    found: set[int] = set()
    for section in ("wins", "slips", "conflicts"):
        for item in output.get(section, []):
            found.update(item.get(key, []))
    return found


def _lookup(db: DbSession, output: dict[str, Any]) -> dict[str, dict[int, str]]:
    goal_ids = _ids(output, "goal_ids") | {
        p["goal_id"] for p in output.get("priorities", []) if p.get("goal_id")
    }
    person_ids = _ids(output, "person_ids")
    meeting_ids = _ids(output, "meeting_ids")
    promise_ids = _ids(output, "promise_ids")

    def names(id_col: Any, name_col: Any, ids: set[int]) -> dict[int, str]:
        rows = db.execute(select(id_col, name_col).where(id_col.in_(ids))).all()
        return {int(row[0]): str(row[1]) for row in rows}

    return {
        "goals": names(Goal.id, Goal.title, goal_ids),
        "people": names(Person.id, Person.display_name, person_ids),
        "meetings": names(Meeting.id, Meeting.title, meeting_ids),
        "promises": names(Promise.id, Promise.text, promise_ids),
    }


@router.get("/reviews")
def reviews_page(
    request: Request, db: DbSession, settings: SettingsDep, reviewer: WeeklyReviewerDep
) -> Response:
    return templates.TemplateResponse(
        request,
        "reviews/list.html",
        {
            "reviews": review.list_reviews(db),
            "configured": reviewer is not None,
            "enabled": settings.weekly_review_enabled,
            "external": request.app.state.settings.orbit_review_token is not None,
        },
    )


@router.post("/reviews/run")
def reviews_run(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    reviewer: WeeklyReviewerDep,
    sender: PushSenderDep,
) -> Response:
    if reviewer is None:
        flash(request, "Claude isn't configured, so there is no weekly review.", "error")
        return RedirectResponse("/reviews", status_code=303)
    try:
        row = review.run_now(
            db,
            reviewer,
            sender,
            tz=settings.timezone,
            budget_chars=settings.context_budget_chars,
            base_url=settings.orbit_url,
            quiet=notifications.quiet_hours(settings),
        )
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/reviews", status_code=303)
    if row.status.value == "failed":
        flash(request, f"The review failed: {row.error}", "error")
    return RedirectResponse(f"/reviews/{row.id}", status_code=303)


@router.get("/reviews/{review_id:int}")
def review_page(
    request: Request, db: DbSession, reviewer: WeeklyReviewerDep, review_id: int
) -> Response:
    row = review.get_review(db, review_id)
    output = row.output_json or {}
    proposals = review.review_proposals(db, row)
    return templates.TemplateResponse(
        request,
        "reviews/detail.html",
        {
            "review": row,
            "output": output,
            "names": _lookup(db, output),
            "proposals": proposals,
            "kind_labels": KIND_LABELS,
            "configured": reviewer is not None,
            "input_json": json.dumps(row.input_json, ensure_ascii=False, indent=2)
            if row.input_json
            else "",
        },
    )
