"""Schedule settings and Run now (SPEC §2.21)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from orbit.deps import DbSession
from orbit.jobs import scheduler as jobs
from orbit.services import schedule_config
from orbit.services.errors import NotFoundError

router = APIRouter(tags=["schedule"])


class SettingOut(BaseModel):
    key: str
    group: str
    label: str
    kind: str
    value: Any
    default: Any
    overridden: bool
    unit: str


class SettingsPatch(BaseModel):
    values: dict[str, Any]


class JobOut(BaseModel):
    id: str
    label: str
    every: str
    run_now: bool
    unavailable: str | None
    enabled: bool
    next_run: datetime | None
    last_finished_at: datetime | None
    last_ok: bool | None
    last_message: str | None
    last_manual: bool | None


class RunOut(BaseModel):
    job_id: str
    ran: bool
    ok: bool
    message: str


def _rows(db: DbSession, request: Request) -> list[SettingOut]:
    base = request.app.state.settings
    return [
        SettingOut(
            key=row.definition.key,
            group=row.definition.group,
            label=row.definition.label,
            kind=row.definition.kind.value,
            value=row.value,
            default=row.default,
            overridden=row.overridden,
            unit=row.definition.unit,
        )
        for row in schedule_config.rows(db, base)
    ]


@router.get("/settings/schedule", response_model=list[SettingOut])
def get_schedule(db: DbSession, request: Request) -> list[SettingOut]:
    return _rows(db, request)


@router.patch("/settings/schedule", response_model=list[SettingOut])
def patch_schedule(body: SettingsPatch, db: DbSession, request: Request) -> list[SettingOut]:
    """Partial update, all or nothing: one invalid value changes nothing (422)."""
    schedule_config.save(db, request.app.state.settings, body.values)
    db.commit()
    _applied(request)
    return _rows(db, request)


@router.delete("/settings/schedule/{key}", response_model=list[SettingOut])
def reset_schedule(key: str, db: DbSession, request: Request) -> list[SettingOut]:
    schedule_config.reset(db, key)
    db.commit()
    _applied(request)
    return _rows(db, request)


def _applied(request: Request) -> None:
    """Make the saved values count now: re-read them and move the jobs to their new intervals."""
    request.app.state.overrides.invalidate()
    jobs.sync_intervals(request.app.state.jobs)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(request: Request) -> list[JobOut]:
    return [
        JobOut(
            id=s.spec.id,
            label=s.spec.label,
            every=s.every,
            run_now=s.spec.run_now,
            unavailable=s.unavailable,
            enabled=s.enabled,
            next_run=s.next_run,
            last_finished_at=s.last.last_finished_at if s.last else None,
            last_ok=s.last.ok if s.last else None,
            last_message=s.last.message if s.last else None,
            last_manual=s.last.manual if s.last else None,
        )
        for s in jobs.job_statuses(request.app.state.jobs)
    ]


@router.post("/jobs/{job_id}/run", response_model=RunOut)
def run_job(job_id: str, request: Request) -> RunOut:
    """Run one job now (the same function the scheduler calls). 404 for a job without Run now."""
    spec = jobs.job_spec(job_id)
    if not spec.run_now:
        raise NotFoundError(f"Job {job_id!r} has no Run now.")
    result = jobs.execute(request.app.state.jobs, spec, manual=True)
    return RunOut(job_id=job_id, ran=result.ran, ok=result.ok, message=result.message)
