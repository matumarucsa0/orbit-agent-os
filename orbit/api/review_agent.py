"""The weekly review made by an external Claude session (SPEC §2.18).

Three calls: `prepare` claims the due week and hands out what Claude should read, `submit` takes
its answer (checked exactly like an API answer), `fail` reports a run that did not work. They
answer to the review token, which opens nothing else; the main API token works here too.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from orbit.api.reviews import review_out
from orbit.api.schemas import ReviewOut
from orbit.deps import DbSession, PushSenderDep, SettingsDep
from orbit.domain.enums import ReviewStatus
from orbit.integrations.claude import ReviewOutput, build_review_prompt, review_system, with_owner
from orbit.services import notifications, review

_bearer = HTTPBearer(auto_error=False)


def require_review_token(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    allowed = [settings.orbit_api_token.get_secret_value()]
    if settings.orbit_review_token is not None:
        allowed.append(settings.orbit_review_token.get_secret_value())
    given = credentials.credentials.encode() if credentials is not None else b""
    # Compare against every allowed token (no early exit on the first match).
    matches = [secrets.compare_digest(given, token.encode()) for token in allowed]
    if credentials is None or not any(matches):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix="/api/v1/review-agent",
    tags=["review-agent"],
    dependencies=[Depends(require_review_token)],
)


class PreparedOut(BaseModel):
    review_id: int
    plan_start: str
    system: str
    prompt: str
    schema_: dict[str, Any] = Field(serialization_alias="schema")


class SubmitIn(BaseModel):
    output: ReviewOutput
    model: str | None = Field(default=None, max_length=100)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class FailIn(BaseModel):
    error: str = Field(min_length=1, max_length=1000)


@router.post("/prepare", response_model=PreparedOut, response_model_by_alias=True)
def prepare(
    db: DbSession, settings: SettingsDep, response: Response, force: bool = False
) -> PreparedOut | Response:
    """Claim the planned week that is due (or this week's with `force`) and return what Claude
    should read. 204 when nothing is due, or the week is ready, being made, or out of attempts."""
    now = datetime.now(UTC)
    tz = settings.timezone
    if force:
        plan_start = review.weeks_around(now.astimezone(tz).date()).plan_start
    else:
        due = review.due_plan_start(now, tz, review.timing_from(settings))
        if due is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        plan_start = due
    claimed = review.claim(db, plan_start, now, retry=force)
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    db.commit()  # the claim is stored before anyone is asked
    request = review.prepare(
        db, claimed, tz=tz, budget_chars=settings.context_budget_chars, now=now
    )
    db.commit()
    request = with_owner(request, settings.owner_name)
    return PreparedOut(
        review_id=claimed.id,
        plan_start=claimed.plan_start.isoformat(),
        system=review_system(request),
        prompt=build_review_prompt(request),
        schema_=ReviewOutput.model_json_schema(),
    )


def _pending(db: DbSession, review_id: int) -> Any:
    row = review.get_review(db, review_id)
    if row.status is not ReviewStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"This review is {row.status.value}."
        )
    return row


@router.post("/{review_id}/submit", response_model=ReviewOut)
def submit(
    review_id: int,
    body: SubmitIn,
    db: DbSession,
    settings: SettingsDep,
    sender: PushSenderDep,
) -> ReviewOut:
    """Store Claude's answer (checked against the stored input), write the proposals, push once."""
    row = _pending(db, review_id)
    review.finish(
        db,
        row,
        body.output,
        model=body.model,
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
    )
    db.commit()
    review.send_push(
        db,
        row,
        sender,
        base_url=settings.orbit_url,
        quiet=notifications.quiet_hours(settings),
    )
    db.commit()
    return review_out(db, row)


@router.post("/{review_id}/fail", response_model=ReviewOut)
def fail(review_id: int, body: FailIn, db: DbSession) -> ReviewOut:
    """The external run did not work: the review is marked failed (the timer retries it)."""
    row = _pending(db, review_id)
    row.status = ReviewStatus.FAILED
    row.error = body.error
    db.commit()
    return review_out(db, row)
