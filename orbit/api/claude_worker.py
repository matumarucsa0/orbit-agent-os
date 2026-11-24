"""The Claude Code worker's endpoints (SPEC §2.19): claim a job, send its answer, report trouble.

They answer to the review token (which opens nothing else); the main API token works too.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from orbit.api.review_agent import require_review_token
from orbit.deps import DbSession, PushSenderDep, SettingsDep
from orbit.domain.base import utcnow
from orbit.domain.report import ReviewReport
from orbit.services import claude_jobs, notifications, reports, review
from orbit.services.claude_jobs import FailureKind, JobOutputError, LateContext, QueueLimits
from orbit.services.errors import InvalidInputError

router = APIRouter(
    prefix="/api/v1/claude-worker",
    tags=["claude-worker"],
    dependencies=[Depends(require_review_token)],
)

MAX_WAIT_SECONDS = 30
POLL_SECONDS = 1.0


class JobOut(BaseModel):
    id: int
    kind: str
    system: str
    prompt: str
    schema_: dict[str, Any] = Field(serialization_alias="schema")


class SubmitIn(BaseModel):
    output: dict[str, Any]
    model: str | None = Field(default=None, max_length=100)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)


class FailIn(BaseModel):
    error: str = Field(min_length=1, max_length=2000)
    kind: FailureKind = "other"
    retry_after_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)


class HeartbeatIn(BaseModel):
    cli_version: str | None = Field(default=None, max_length=100)
    auth_ok: bool | None = None
    rate_limited_for_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)
    selftest_ok: bool = False


def limits(settings: SettingsDep) -> QueueLimits:
    return QueueLimits(
        wait_interactive=settings.claude_wait_interactive_seconds,
        wait_background=settings.claude_wait_background_seconds,
        cap_interactive=settings.claude_queue_cap_interactive,
        cap_background=settings.claude_queue_cap_background,
    )


def late_context(settings: SettingsDep, sender: PushSenderDep) -> LateContext:
    return LateContext(
        sender=sender,
        base_url=settings.orbit_url,
        quiet=notifications.quiet_hours(settings),
        tz=settings.timezone,
    )


@router.post("/claim", response_model=JobOut, response_model_by_alias=True)
def claim(
    db: DbSession,
    settings: SettingsDep,
    sender: PushSenderDep,
    wait: int = Query(default=0, ge=0, le=MAX_WAIT_SECONDS),
    interactive_only: bool = False,
) -> JobOut | Response:
    """The next job, or 204. With `wait`, holds the request until one arrives (a long poll), so
    an idle worker costs one request per `wait` seconds and a new job is picked up at once.
    `interactive_only`: the reserved slot, only jobs somebody waits for (SPEC §2.34); it never
    starts the weekly review."""
    deadline = time.monotonic() + wait
    ctx = late_context(settings, sender)
    while True:
        now = utcnow()
        claude_jobs.heartbeat(db, now=now)  # asking for work shows the worker is alive
        claude_jobs.apply_late(db, ctx, now)
        job = claude_jobs.claim(db, now, interactive_only=interactive_only)
        if job is None and settings.weekly_review_enabled and not interactive_only:
            job = claude_jobs.start_review_job(
                db,
                tz=settings.timezone,
                budget_chars=settings.context_budget_chars,
                now=now,
                timing=review.timing_from(settings),
                owner=settings.owner_name,
            )
        db.commit()
        if job is not None:
            return JobOut(
                id=job.id,
                kind=job.kind,
                system=job.system,
                prompt=job.prompt,
                schema_=job.schema_json,
            )
        if time.monotonic() >= deadline:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        time.sleep(POLL_SECONDS)


@router.post("/jobs/{job_id}/submit")
def submit(
    job_id: int, body: SubmitIn, db: DbSession, settings: SettingsDep, sender: PushSenderDep
) -> dict[str, Any]:
    """Store the answer. 422 with the schema errors when it does not fit (one retry is fair)."""
    try:
        job = claude_jobs.submit(
            db,
            job_id,
            body.output,
            model=body.model,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
            duration_ms=body.duration_ms,
        )
    except JobOutputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors
        ) from exc
    except InvalidInputError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    if job.review_id is not None:
        row = review.get_review(db, job.review_id)
        review.send_push(
            db,
            row,
            sender,
            base_url=settings.orbit_url,
            quiet=notifications.quiet_hours(settings),
        )
        db.commit()
    if job.report_id is not None:
        report = db.get(ReviewReport, job.report_id)
        if report is not None:
            reports.send_push(
                db,
                report,
                sender,
                base_url=settings.orbit_url,
                quiet=notifications.quiet_hours(settings),
            )
            db.commit()
    claude_jobs.apply_late(db, late_context(settings, sender))
    db.commit()
    return {"id": job.id, "status": job.status.value}


@router.post("/jobs/{job_id}/fail")
def fail(job_id: int, body: FailIn, db: DbSession) -> dict[str, Any]:
    retry_after = timedelta(seconds=body.retry_after_seconds) if body.retry_after_seconds else None
    try:
        job = claude_jobs.fail(db, job_id, body.error, kind=body.kind, retry_after=retry_after)
    except InvalidInputError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return {"id": job.id, "status": job.status.value}


@router.post("/jobs/{job_id}/rerun")
def rerun(job_id: int, db: DbSession, settings: SettingsDep) -> dict[str, Any]:
    try:
        job = claude_jobs.rerun(db, job_id, limits(settings))
    except InvalidInputError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    return {"id": job.id, "status": job.status.value}


@router.post("/heartbeat")
def heartbeat(body: HeartbeatIn, db: DbSession) -> dict[str, Any]:
    now = utcnow()
    limited = (
        now + timedelta(seconds=body.rate_limited_for_seconds)
        if body.rate_limited_for_seconds
        else None
    )
    claude_jobs.heartbeat(
        db,
        cli_version=body.cli_version,
        auth_ok=body.auth_ok,
        rate_limited_until=limited,
        selftest_ok=body.selftest_ok,
        now=now,
    )
    db.commit()
    return claude_jobs.status_json(db, now)


@router.get("/status")
def status_(db: DbSession) -> dict[str, Any]:
    return claude_jobs.status_json(db)
