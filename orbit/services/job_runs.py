"""The last run of each scheduled job (SPEC §2.21). Callers commit."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.app_settings import JobRun

MESSAGE_CHARS = 500


def record(
    session: Session,
    job_id: str,
    *,
    started: datetime,
    finished: datetime,
    ok: bool,
    message: str,
    manual: bool,
) -> JobRun:
    row = session.get(JobRun, job_id)
    if row is None:
        row = JobRun(job_id=job_id, last_started_at=started, last_finished_at=finished, ok=ok)
        session.add(row)
    row.last_started_at = started
    row.last_finished_at = finished
    row.ok = ok
    row.message = message[:MESSAGE_CHARS]
    row.manual = manual
    session.flush()
    return row


def last_runs(session: Session) -> dict[str, JobRun]:
    return {row.job_id: row for row in session.scalars(select(JobRun))}
