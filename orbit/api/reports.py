"""Custom review reports over a date range (SPEC §2.22)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from pydantic import BaseModel, Field

from orbit.deps import DbSession, PushSenderDep, ReportWriterDep, SettingsDep
from orbit.domain.enums import ReviewStatus
from orbit.domain.report import ReviewReport
from orbit.services import report_run, reports

router = APIRouter(tags=["reports"])


class ReportIn(BaseModel):
    start: date
    end: date
    focus: str | None = Field(default=None, max_length=2000)
    goal_id: int | None = None
    person_id: int | None = None
    course_id: int | None = None


class ReportOut(BaseModel):
    id: int
    range_start: date
    range_end: date
    focus_text: str | None
    focus_goal_id: int | None
    focus_person_id: int | None
    focus_course_id: int | None
    status: ReviewStatus
    output: dict[str, Any] | None
    numbers: list[dict[str, Any]] | None
    error: str | None
    attempts: int
    claude_used: bool
    plain_reason: str | None
    model: str | None
    input_chars: int | None
    input_tokens: int
    output_tokens: int
    created_at: datetime


def report_out(row: ReviewReport) -> ReportOut:
    return ReportOut(
        id=row.id,
        range_start=row.range_start,
        range_end=row.range_end,
        focus_text=row.focus_text,
        focus_goal_id=row.focus_goal_id,
        focus_person_id=row.focus_person_id,
        focus_course_id=row.focus_course_id,
        status=row.status,
        output=row.output_json,
        numbers=row.numbers_json,
        error=row.error,
        attempts=row.attempts,
        claude_used=row.claude_used,
        plain_reason=row.plain_reason,
        model=row.model,
        input_chars=row.input_chars,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        created_at=row.created_at,
    )


@router.post("/reports", response_model=ReportOut, status_code=status.HTTP_202_ACCEPTED)
def make_report(
    body: ReportIn,
    db: DbSession,
    settings: SettingsDep,
    writer: ReportWriterDep,
    sender: PushSenderDep,
    request: Request,
    background: BackgroundTasks,
) -> ReportOut:
    """Start a report. 202 with the report: poll `GET /reports/{id}` until it is ready or failed.
    422 for a range that is not allowed or when another report is being made."""
    data = reports.ReportInput(
        body.start, body.end, body.focus, body.goal_id, body.person_id, body.course_id
    )
    started = report_run.start(db, data, settings=settings, writer=writer)
    db.commit()
    if started.mode == "api" and writer is not None:
        background.add_task(
            report_run.run_api,
            request.app.state.sessionmaker,
            started.report_id,
            writer,
            sender,
            settings,
        )
    return report_out(reports.get_report(db, started.report_id))


@router.get("/reports", response_model=list[ReportOut])
def list_reports(db: DbSession) -> list[ReportOut]:
    return [report_out(r) for r in reports.list_reports(db)]


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: int, db: DbSession) -> ReportOut:
    return report_out(reports.get_report(db, report_id))


@router.get("/reports/{report_id}/input")
def report_input(report_id: int, db: DbSession) -> dict[str, Any]:
    """What Claude was given for this report (empty while it has not been built)."""
    return reports.get_report(db, report_id).input_json or {}


@router.post("/reports/{report_id}/retry", response_model=ReportOut, status_code=202)
def retry_report(
    report_id: int,
    db: DbSession,
    settings: SettingsDep,
    writer: ReportWriterDep,
    sender: PushSenderDep,
    request: Request,
    background: BackgroundTasks,
) -> ReportOut:
    started = report_run.retry(db, report_id, settings=settings, writer=writer)
    db.commit()
    if started.mode == "api" and writer is not None:
        background.add_task(
            report_run.run_api,
            request.app.state.sessionmaker,
            started.report_id,
            writer,
            sender,
            settings,
        )
    return report_out(reports.get_report(db, started.report_id))


@router.delete("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(report_id: int, db: DbSession) -> Response:
    reports.delete_report(db, report_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
