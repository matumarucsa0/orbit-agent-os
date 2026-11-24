from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from orbit.api.schemas import ReviewOut
from orbit.deps import DbSession, PushSenderDep, SettingsDep, WeeklyReviewerDep
from orbit.domain.review import WeeklyReview
from orbit.services import notifications, review
from orbit.services.errors import InvalidInputError

router = APIRouter(tags=["reviews"])


def review_out(db: DbSession, row: WeeklyReview) -> ReviewOut:
    return ReviewOut(
        id=row.id,
        plan_start=row.plan_start,
        plan_end=row.plan_end,
        review_start=row.review_start,
        review_end=row.review_end,
        status=row.status,
        output=row.output_json,
        error=row.error,
        attempts=row.attempts,
        model=row.model,
        input_chars=row.input_chars,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        pushed_at=row.pushed_at,
        created_at=row.created_at,
        proposal_ids=[p.id for p in review.review_proposals(db, row)],
    )


@router.get("/reviews", response_model=list[ReviewOut])
def list_reviews(db: DbSession) -> list[ReviewOut]:
    return [review_out(db, r) for r in review.list_reviews(db)]


@router.post("/reviews/run", response_model=ReviewOut)
def run_review(
    db: DbSession,
    settings: SettingsDep,
    reviewer: WeeklyReviewerDep,
    sender: PushSenderDep,
) -> ReviewOut:
    """Make this week's review now (it commits as it goes: the claim is stored before Claude is
    asked). A failed week is retried; a ready or running one is refused with 422."""
    if reviewer is None:
        raise InvalidInputError("Claude isn't configured, so there is no weekly review.")
    row = review.run_now(
        db,
        reviewer,
        sender,
        tz=settings.timezone,
        budget_chars=settings.context_budget_chars,
        base_url=settings.orbit_url,
        quiet=notifications.quiet_hours(settings),
    )
    return review_out(db, row)


@router.get("/reviews/{review_id}", response_model=ReviewOut)
def get_review(review_id: int, db: DbSession) -> ReviewOut:
    return review_out(db, review.get_review(db, review_id))


@router.get("/reviews/{review_id}/input")
def get_review_input(review_id: int, db: DbSession) -> dict[str, Any]:
    """What Claude was given for this review."""
    return review.get_review(db, review_id).input_json or {}
