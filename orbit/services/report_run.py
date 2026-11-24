"""Starting a report and running it on whichever Claude path is available (SPEC §2.22).

Three paths, as for the other Claude features (`CLAUDE.md` rule 5): the API client (made in the
background after the request returns), the Claude worker (a queued job), or no Claude at all (a
plain report of numbers and facts, made at once). Callers commit after `start` and `retry`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from orbit.domain.base import utcnow
from orbit.domain.enums import ReviewStatus
from orbit.domain.report import ReviewReport
from orbit.integrations.claude import ReportWriter
from orbit.integrations.ntfy import PushSender
from orbit.services import claude_jobs, notifications, reports
from orbit.settings import Settings

log = logging.getLogger(__name__)

Mode = Literal["api", "worker", "plain"]


@dataclass(frozen=True)
class Started:
    report_id: int
    mode: Mode  # "api": the caller must run `run_api` after it commits


def _dispatch(
    session: Session,
    report: ReviewReport,
    settings: Settings,
    writer: ReportWriter | None,
    now: datetime,
) -> Mode:
    if writer is not None:
        return "api"  # `run_api` prepares and asks; the row stays pending until then
    reason = "Claude is not set up, so this report has no Claude text."
    if settings.resolved_claude_backend == "worker":
        worker = claude_jobs.worker_row(session)
        if worker.auth_ok and claude_jobs.is_alive(worker, now):
            request = reports.prepare(
                session,
                report,
                tz=settings.timezone,
                budget_chars=settings.context_budget_chars,
                now=now,
            )
            claude_jobs.enqueue_report(session, report, request, now, owner=settings.owner_name)
            return "worker"
        reason = (
            "Claude Code on the server is not running or not logged in, "
            "so this report has no Claude text."
        )
    reports.prepare(
        session,
        report,
        tz=settings.timezone,
        budget_chars=settings.context_budget_chars,
        now=now,
    )
    reports.finish_plain(session, report, reason)
    return "plain"


def start(
    session: Session,
    data: reports.ReportInput,
    *,
    settings: Settings,
    writer: ReportWriter | None,
    now: datetime | None = None,
) -> Started:
    """Make the report row and put it on the available path."""
    moment = now or utcnow()
    today = moment.astimezone(settings.timezone).date()
    report = reports.create(session, data, today, moment)
    mode = _dispatch(session, report, settings, writer, moment)
    return Started(report.id, mode)


def retry(
    session: Session,
    report_id: int,
    *,
    settings: Settings,
    writer: ReportWriter | None,
    now: datetime | None = None,
) -> Started:
    """Try a failed (or abandoned) report again, from zero."""
    moment = now or utcnow()
    report = reports.reopen(session, report_id, moment)
    return Started(report.id, _dispatch(session, report, settings, writer, moment))


def run_api(
    sessions: sessionmaker[Session],
    report_id: int,
    writer: ReportWriter,
    sender: PushSender | None,
    settings: Settings,
) -> None:
    """Ask the API client and store the answer; push when ready. Runs after the request that made
    the report returned, so it opens its own sessions and never raises."""
    try:
        with sessions() as session:
            report = reports.get_report(session, report_id)
            if report.status is not ReviewStatus.PENDING:
                return
            reports.generate(
                session,
                report,
                writer,
                tz=settings.timezone,
                budget_chars=settings.context_budget_chars,
            )
            session.commit()
            reports.send_push(
                session,
                report,
                sender,
                base_url=settings.orbit_url,
                quiet=notifications.quiet_hours(settings),
            )
            session.commit()
    except Exception as exc:  # a bug must not leave the report pending: record it
        log.exception("report %s failed", report_id)
        with sessions() as session:
            reports.fail(session, report_id, f"{exc.__class__.__name__}: {exc}")
            session.commit()
