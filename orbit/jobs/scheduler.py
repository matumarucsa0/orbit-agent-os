"""In-process APScheduler: periodic jobs run in a background thread of the app process."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session, sessionmaker

from orbit.domain.app_settings import JobRun
from orbit.domain.base import utcnow
from orbit.integrations.caldav_client import CalDavError, CalendarSource
from orbit.integrations.claude import (
    BriefSummarizer,
    ClaudeClient,
    MeetingLinker,
    WeeklyReviewer,
)
from orbit.integrations.ntfy import PushSender
from orbit.integrations.resources import ResourceFetcher
from orbit.services import (
    budget,
    claude_jobs,
    contact_sync,
    job_runs,
    meetings,
    notifications,
    reminders,
    review,
    school,
    school_reader,
    school_sync,
)
from orbit.services import fitness as fitness_service
from orbit.services import school_resources as school_resources_service
from orbit.services.claude_jobs import JobClaudeClient
from orbit.services.contact_sync import BACKUP_DAYS
from orbit.services.errors import NotFoundError
from orbit.services.usage import UsageLog
from orbit.settings import Settings

log = logging.getLogger(__name__)

FLUSH_SECONDS = 60
MEETING_PUSH_SECONDS = 60
REMINDER_SECONDS = 60
USAGE_SECONDS = 60
BACKUP_PRUNE_HOURS = 24
REVIEW_SECONDS = 60
LATE_ANSWER_SECONDS = 30
CLAUDE_PRUNE_HOURS = 1
ALERT_SECONDS = 300
SCHOOL_PUSH_SECONDS = 60
SCHOOL_RESOURCES_MINUTES = 15  # a tick; each resource is fetched once a day from the set hour


def flush_notifications(
    sessions: sessionmaker[Session],
    sender: PushSender | None,
    settings: Settings,
    force: bool = False,
) -> int:
    """Send pushes held back by quiet hours or a failed attempt, once they're due. `force` (Run
    now) also takes the ones waiting for a retry; quiet hours still hold."""
    with sessions() as session:
        sent = notifications.flush_due(
            session,
            sender,
            quiet=notifications.quiet_hours(settings),
            ignore_not_before=force,
        )
        session.commit()
    return sent


def flush_usage(
    sessions: sessionmaker[Session],
    usage_log: UsageLog,
    sender: PushSender | None = None,
    settings: Settings | None = None,
) -> int:
    """Write the Claude calls buffered since the last flush, then send the budget push if due."""
    try:
        with sessions() as session:
            count = usage_log.flush(session)
            if settings is not None and settings.claude_monthly_budget_eur > 0:
                budget.check_alerts(
                    session,
                    sender,
                    settings.claude_monthly_budget_eur,
                    settings.timezone,
                    notifications.quiet_hours(settings),
                )
            session.commit()
    except Exception:  # statistics only: never take the scheduler (or a shutdown) down
        log.exception("writing Claude usage failed")
        return 0
    return count


def prune_contact_backups(sessions: sessionmaker[Session], keep_days: int = BACKUP_DAYS) -> int:
    """Drop saved Apple cards older than the retention period."""
    with sessions() as session:
        count = contact_sync.prune_backups(session, keep_days=keep_days)
        session.commit()
    return count


def late_claude_answers(
    sessions: sessionmaker[Session], sender: PushSender | None, settings: Settings
) -> int:
    """Use Claude answers that came after their request had fallen back (SPEC §2.19)."""
    ctx = claude_jobs.LateContext(
        sender, settings.orbit_url, notifications.quiet_hours(settings), settings.timezone
    )
    with sessions() as session:
        count = claude_jobs.apply_late(session, ctx)
        session.commit()
    return count


def claude_worker_alerts(
    sessions: sessionmaker[Session], sender: PushSender | None, settings: Settings
) -> int:
    """Push when the Claude login expired or the worker is away with jobs waiting."""
    with sessions() as session:
        count = claude_jobs.send_alerts(
            session,
            sender,
            base_url=settings.orbit_url,
            quiet=notifications.quiet_hours(settings),
        )
        session.commit()
    return count


def prune_claude_jobs(sessions: sessionmaker[Session], settings: Settings) -> int:
    """Clear the prompts and answers of finished Claude jobs past the retention period."""
    with sessions() as session:
        count = claude_jobs.prune(session, settings.claude_job_retention_days)
        session.commit()
    return count


def sync_calendar(
    sessions: sessionmaker[Session], source: CalendarSource, linker: MeetingLinker | None
) -> meetings.SyncReport | None:
    """Pull the next two weeks of meetings from iCloud; None when the calendar can't be read."""
    with sessions() as session:
        try:
            report = meetings.sync_meetings(session, source, linker=linker)
        except CalDavError as exc:
            log.warning("calendar sync failed: %s", exc)
            return None
        session.commit()
    log.info("calendar sync: %s", report.summary)
    return report


def meeting_pushes(
    sessions: sessionmaker[Session],
    sender: PushSender | None,
    summarizer: BriefSummarizer | None,
    settings: Settings,
) -> int:
    """Briefs before linked meetings and capture prompts after them, when due."""
    with sessions() as session:
        count = meetings.send_meeting_pushes(
            session,
            sender,
            base_url=settings.orbit_url,
            tz=settings.timezone,
            quiet=notifications.quiet_hours(settings),
            minutes_before=settings.brief_minutes_before,
            minutes_after=settings.capture_minutes_after,
            summarizer=summarizer if settings.brief_use_claude else None,
        )
        session.commit()
    return count


def reminder_pushes(
    sessions: sessionmaker[Session], sender: PushSender | None, settings: Settings
) -> int:
    """Birthday / name-day, lent-item and promise reminders, and the Sunday digest, when due."""
    with sessions() as session:
        count = reminders.send_reminders(
            session,
            sender,
            base_url=settings.orbit_url,
            tz=settings.timezone,
            quiet=notifications.quiet_hours(settings),
            item_reminder_days=settings.item_reminder_days,
            reminder_time=settings.reminder_time,
            close_days_ahead=settings.reminder_close_days_ahead,
            digest_weekday=settings.digest_weekday,
            digest_time=settings.digest_time,
        )
        session.commit()
    return count


def sis_sync(
    sessions: sessionmaker[Session],
    sources: school_sync.SchoolSources,
    sender: PushSender | None,
    settings: Settings,
) -> school_sync.SyncReport | None:
    """Read SIS, ReCodEx and Owl (SPEC §2.20); a source problem is recorded and pushed, never
    raised. None when the run itself failed (a bug: logged, retried next poll)."""
    try:
        with sessions() as session:
            report = school_sync.sync_school(
                session, sources, school_sync.config_from(settings), sender=sender
            )
            session.commit()
    except Exception:
        log.exception("school sync failed")
        return None
    log.info("school sync: %s", report.summary)
    return report


def school_pushes(
    sessions: sessionmaker[Session],
    sender: PushSender | None,
    settings: Settings,
    sources: school_sync.SchoolSources,
) -> int:
    """Tomorrow's lessons, exam and deadline reminders, schedule changes, token expiry."""
    claims = sources.recodex.claims if sources.recodex is not None else None
    with sessions() as session:
        count = school.send_school_pushes(
            session,
            sender,
            base_url=settings.orbit_url,
            tz=settings.timezone,
            quiet=notifications.quiet_hours(settings),
            evening=settings.school_evening_time,
            recodex_expires=claims.expires if claims is not None else None,
            reminder_time=settings.school_reminder_time,
            exam_days=settings.school_exam_days,
            deadline_days=settings.school_deadline_days,
            change_gap=timedelta(minutes=settings.school_change_gap_minutes),
            token_warn_days=settings.school_token_warn_days,
        )
        session.commit()
    return count


def weekly_review(
    sessions: sessionmaker[Session],
    reviewer: WeeklyReviewer,
    sender: PushSender | None,
    settings: Settings,
    now: datetime | None = None,
    force: bool = False,
) -> int | None:
    """Make the weekly review when one is due (SPEC §2.17); returns its id, or None.

    The week's row is committed before Claude is asked, so a second tick (or a restart) cannot
    start the same week again; a ready review is then pushed once. `force` (Run now) makes the
    next week's review whatever the day, as `prepare?force=1` does (SPEC §2.18)."""
    moment = now or utcnow()
    if force:
        plan_start: date | None = review.weeks_around(
            moment.astimezone(settings.timezone).date()
        ).plan_start
    else:
        plan_start = review.due_plan_start(moment, settings.timezone, review.timing_from(settings))
    if plan_start is None:
        return None
    with sessions() as session:
        claimed = review.claim(session, plan_start, moment, retry=force)
        review_id = claimed.id if claimed is not None else None
        session.commit()
    if review_id is None:
        return None
    try:
        with sessions() as session:
            row = review.get_review(session, review_id)
            review.generate(
                session,
                row,
                reviewer,
                tz=settings.timezone,
                budget_chars=settings.context_budget_chars,
                now=moment,
            )
            session.commit()
            review.send_push(
                session,
                row,
                sender,
                base_url=settings.orbit_url,
                quiet=notifications.quiet_hours(settings),
                now=moment,
            )
            session.commit()
    except Exception as exc:  # a bug must not take the scheduler down: record it, retry later
        log.exception("weekly review failed")
        with sessions() as session:
            review.fail(session, review_id, f"{exc.__class__.__name__}: {exc}")
            session.commit()
    return review_id


# --- The job registry: one place that knows every job, for the scheduler and for Run now ---


@dataclass
class JobContext:
    """What the jobs need. `settings` returns the effective settings (env + overrides) each time,
    so an edit applies at the next run."""

    sessions: sessionmaker[Session]
    settings: Callable[[], Settings]
    sender: PushSender | None = None
    calendar: CalendarSource | None = None
    claude: ClaudeClient | JobClaudeClient | None = None
    reviewer: WeeklyReviewer | None = None
    usage_log: UsageLog | None = None
    school: school_sync.SchoolSources | None = None
    fetcher: ResourceFetcher | None = None  # course pages and PDFs (Phase 18c)
    workouts: fitness_service.WorkoutSource | None = None  # Liftoff (Phase 33)
    scheduler: BackgroundScheduler | None = None
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def lock(self, job_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(job_id, threading.Lock())


RunFn = Callable[[JobContext, Settings, bool], str]


def _always(_ctx: JobContext) -> str | None:
    return None


def _on(_settings: Settings) -> bool:
    return True


@dataclass(frozen=True)
class JobSpec:
    id: str
    label: str
    what: str
    run: RunFn
    interval: Callable[[Settings], dict[str, int]]
    run_now: bool = False  # offered as a Run now button
    first_now: bool = False  # also runs once at start
    available: Callable[[JobContext], str | None] = _always  # why it cannot run, or None
    enabled: Callable[[Settings], bool] = _on  # the settings switch


def _count(n: int, word: str) -> str:
    return f"{n} {word}"


def _run_flush(ctx: JobContext, s: Settings, manual: bool) -> str:
    sent = flush_notifications(ctx.sessions, ctx.sender, s, force=manual)
    note = "" if ctx.sender is not None else " (ntfy is not configured)"
    return _count(sent, "sent") + note


def _run_calendar(ctx: JobContext, s: Settings, manual: bool) -> str:
    assert ctx.calendar is not None
    report = sync_calendar(ctx.sessions, ctx.calendar, ctx.claude)
    return report.summary if report is not None else "the calendar could not be read"


def _run_meetings(ctx: JobContext, s: Settings, manual: bool) -> str:
    return _count(meeting_pushes(ctx.sessions, ctx.sender, ctx.claude, s), "recorded")


def _run_reminders(ctx: JobContext, s: Settings, manual: bool) -> str:
    return _count(reminder_pushes(ctx.sessions, ctx.sender, s), "recorded")


def _run_school_pushes(ctx: JobContext, s: Settings, manual: bool) -> str:
    return _count(
        school_pushes(ctx.sessions, ctx.sender, s, ctx.school or school_sync.SchoolSources()),
        "recorded",
    )


def school_resources(
    sessions: sessionmaker[Session],
    fetcher: ResourceFetcher,
    claude: object,
    sender: PushSender | None,
    settings: Settings,
    now: datetime | None = None,
    *,
    force: bool = False,
) -> str:
    """Check the kept course pages and PDFs, let Claude read what is new or changed, suggest the
    links it found, and push once a day about changed pages (Phase 18c). Returns a summary."""
    moment = now or utcnow()
    tz = settings.timezone
    with sessions() as session:
        added = school_resources_service.suggest_links(session)
        checked = school_resources_service.run_checks(
            session, fetcher, moment, tz, settings.school_sites_hour, force=force
        )
        session.commit()
    reader = school_reader.reader_of(claude)
    read = school_reader.ReadReport()
    if reader is not None and (force or moment.astimezone(tz).hour >= settings.school_sites_hour):
        with sessions() as session:
            read = school_reader.read_pending(
                session, reader, moment, tz, settings.school_reads_per_day
            )
            session.commit()
    with sessions() as session:
        pushed = school_reader.send_changed_push(
            session,
            sender,
            base_url=settings.orbit_url,
            tz=tz,
            quiet=notifications.quiet_hours(settings),
            now=moment,
        )
        session.commit()
    text = f"{checked.checked} checked, {checked.changed} changed, {checked.failed} failed"
    if read.reads:
        text += f"; {read.reads} reads: {read.deadlines} deadlines, {read.facts} rules"
    if added or read.links:
        text += f"; {added + read.links} suggestions"
    if checked.waiting or read.waiting:
        text += f"; {checked.waiting + read.waiting} waiting for tomorrow"
    return text + ("; pushed" if pushed else "")


def _run_sis(ctx: JobContext, s: Settings, manual: bool) -> str:
    assert ctx.school is not None
    report = sis_sync(ctx.sessions, ctx.school, ctx.sender, s)
    return report.summary if report is not None else "the run failed (see the log)"


def _run_review(ctx: JobContext, s: Settings, manual: bool) -> str:
    assert ctx.reviewer is not None
    review_id = weekly_review(ctx.sessions, ctx.reviewer, ctx.sender, s, force=manual)
    return f"review {review_id}" if review_id is not None else "nothing due"


def _run_prune_backups(ctx: JobContext, s: Settings, manual: bool) -> str:
    return _count(prune_contact_backups(ctx.sessions, s.contact_backup_keep_days), "cards removed")


def _run_prune_claude(ctx: JobContext, s: Settings, manual: bool) -> str:
    return _count(prune_claude_jobs(ctx.sessions, s), "jobs cleared")


def _run_usage(ctx: JobContext, s: Settings, manual: bool) -> str:
    assert ctx.usage_log is not None
    return _count(flush_usage(ctx.sessions, ctx.usage_log, ctx.sender, s), "calls written")


def _run_late(ctx: JobContext, s: Settings, manual: bool) -> str:
    return _count(late_claude_answers(ctx.sessions, ctx.sender, s), "used")


def _run_alerts(ctx: JobContext, s: Settings, manual: bool) -> str:
    return _count(claude_worker_alerts(ctx.sessions, ctx.sender, s), "sent")


def _run_school_resources(ctx: JobContext, s: Settings, manual: bool) -> str:
    assert ctx.fetcher is not None
    return school_resources(ctx.sessions, ctx.fetcher, ctx.claude, ctx.sender, s, force=manual)


def _run_liftoff(ctx: JobContext, s: Settings, manual: bool) -> str:
    assert ctx.workouts is not None
    with ctx.sessions() as session:
        report = fitness_service.sync_workouts(session, ctx.workouts)
        session.commit()
    return report.summary


def _need_workouts(ctx: JobContext) -> str | None:
    if ctx.workouts is not None:
        return None
    return "Liftoff is not set up (LIFTOFF_REFRESH_TOKEN in .env)."


def _need_fetcher(ctx: JobContext) -> str | None:
    return None if ctx.fetcher is not None else "Page fetching is off."


def _need_calendar(ctx: JobContext) -> str | None:
    return None if ctx.calendar is not None else "The calendar is not set up (CALDAV_* in .env)."


def _need_school(ctx: JobContext) -> str | None:
    if ctx.school is not None and ctx.school.configured:
        return None
    return "No school source is set up (SIS_* / RECODEX_* / OWL_* in .env)."


def _need_reviewer(ctx: JobContext) -> str | None:
    if ctx.reviewer is not None:
        return None
    return "Orbit does not make the review itself here: the Claude worker or the script does."


def _need_worker(ctx: JobContext) -> str | None:
    return None if isinstance(ctx.claude, JobClaudeClient) else "Only with the Claude worker."


def _need_usage(ctx: JobContext) -> str | None:
    return None if ctx.usage_log is not None else "Usage is not recorded."


def _every(**kwargs: int) -> Callable[[Settings], dict[str, int]]:
    return lambda _s: kwargs


JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        "flush_notifications",
        "Send held pushes",
        "Pushes held by quiet hours or a failed send, once due.",
        _run_flush,
        _every(seconds=FLUSH_SECONDS),
        run_now=True,
    ),
    JobSpec(
        "sync_calendar",
        "Read the calendar",
        "Pulls the next two weeks of meetings from iCloud.",
        _run_calendar,
        lambda s: {"minutes": s.caldav_poll_minutes},
        run_now=True,
        first_now=True,
        available=_need_calendar,
    ),
    JobSpec(
        "meeting_pushes",
        "Meeting pushes",
        "Briefs before and capture prompts after meetings.",
        _run_meetings,
        _every(seconds=MEETING_PUSH_SECONDS),
        run_now=True,
    ),
    JobSpec(
        "reminder_pushes",
        "Reminders and digest",
        "Birthdays, name days, lent items, promises and the weekly digest, when due.",
        _run_reminders,
        _every(seconds=REMINDER_SECONDS),
        run_now=True,
    ),
    JobSpec(
        "prune_contact_backups",
        "Clear old Apple cards",
        "Saved cards past their retention.",
        _run_prune_backups,
        lambda s: {"hours": s.backup_prune_hours},
        run_now=True,
    ),
    JobSpec(
        "weekly_review",
        "Weekly review",
        "Makes the review when due. Run now makes next week's, whatever the day.",
        _run_review,
        _every(seconds=REVIEW_SECONDS),
        run_now=True,
        available=_need_reviewer,
        enabled=lambda s: s.weekly_review_enabled,
    ),
    JobSpec(
        "late_claude_answers",
        "Late Claude answers",
        "Uses answers that came after a fallback.",
        _run_late,
        _every(seconds=LATE_ANSWER_SECONDS),
        available=_need_worker,
    ),
    JobSpec(
        "claude_worker_alerts",
        "Claude worker alerts",
        "Push when the login expired or the worker is away.",
        _run_alerts,
        _every(seconds=ALERT_SECONDS),
        available=_need_worker,
    ),
    JobSpec(
        "prune_claude_jobs",
        "Clear old Claude jobs",
        "Prompts and answers past their retention.",
        _run_prune_claude,
        lambda s: {"hours": s.claude_prune_hours},
        run_now=True,
        available=_need_worker,
    ),
    JobSpec(
        "sis_sync",
        "Read SIS, ReCodEx and Owl",
        "Lessons, exams, deadlines and points.",
        _run_sis,
        lambda s: {"minutes": s.sis_poll_minutes},
        run_now=True,
        first_now=True,
        available=_need_school,
    ),
    JobSpec(
        "school_pushes",
        "School pushes",
        "Tomorrow's lessons, exam and deadline reminders, schedule changes, token expiry.",
        _run_school_pushes,
        _every(seconds=SCHOOL_PUSH_SECONDS),
        run_now=True,
        enabled=lambda s: s.school_pushes_enabled,
    ),
    JobSpec(
        "school_resources",
        "Course pages and PDFs",
        "Fetches kept pages once a day, reads what changed with Claude, suggests links.",
        _run_school_resources,
        _every(minutes=SCHOOL_RESOURCES_MINUTES),
        run_now=True,
        available=_need_fetcher,
    ),
    JobSpec(
        "liftoff_sync",
        "Read Liftoff workouts",
        "Pulls every gym workout from Liftoff (read only).",
        _run_liftoff,
        lambda s: {"hours": s.liftoff_poll_hours},
        run_now=True,
        first_now=True,
        available=_need_workouts,
    ),
    JobSpec(
        "flush_usage",
        "Write Claude usage",
        "Statistics only.",
        _run_usage,
        _every(seconds=USAGE_SECONDS),
        available=_need_usage,
    ),
)

_SPECS = {spec.id: spec for spec in JOBS}


def job_spec(job_id: str) -> JobSpec:
    try:
        return _SPECS[job_id]
    except KeyError:
        raise NotFoundError(f"Unknown job {job_id!r}.") from None


@dataclass(frozen=True)
class JobResult:
    ran: bool  # False when it was not started (unavailable, off, already running)
    ok: bool
    message: str


def execute(ctx: JobContext, spec: JobSpec, *, manual: bool = False) -> JobResult:
    """Run one job now and record the outcome. Never raises: a failing job is logged and shown."""
    reason = spec.available(ctx)
    if reason is not None:
        return JobResult(False, False, reason)
    settings = ctx.settings()
    if not spec.enabled(settings):
        return JobResult(False, False, "Turned off in the settings.")
    lock = ctx.lock(spec.id)
    if not lock.acquire(blocking=False):
        return JobResult(False, False, "Already running.")
    started = utcnow()
    try:
        try:
            message, ok = spec.run(ctx, settings, manual), True
        except Exception as exc:  # a bug in one job must not stop the others
            log.exception("job %s failed", spec.id)
            message, ok = f"{exc.__class__.__name__}: {exc}", False
    finally:
        lock.release()
    try:
        with ctx.sessions() as session:
            job_runs.record(
                session,
                spec.id,
                started=started,
                finished=utcnow(),
                ok=ok,
                message=message,
                manual=manual,
            )
            session.commit()
    except Exception:  # the status is a convenience: never fail a job over it
        log.warning("could not record the run of %s", spec.id, exc_info=True)
    return JobResult(True, ok, message)


def sync_intervals(ctx: JobContext) -> None:
    """Move every job whose interval setting changed to its new interval."""
    if ctx.scheduler is None:
        return
    settings = ctx.settings()
    for spec in JOBS:
        job = ctx.scheduler.get_job(spec.id)
        if job is None:
            continue
        wanted = spec.interval(settings)
        if job.trigger.interval != timedelta(**wanted):
            ctx.scheduler.reschedule_job(spec.id, trigger="interval", **wanted)


def _tick(ctx: JobContext, job_id: str) -> None:
    execute(ctx, _SPECS[job_id])
    try:
        sync_intervals(ctx)  # also covers an edit saved by another process
    except Exception:
        log.warning("could not update the job intervals", exc_info=True)


@dataclass(frozen=True)
class JobStatus:
    spec: JobSpec
    every: str
    unavailable: str | None
    enabled: bool
    next_run: datetime | None
    last: JobRun | None


def describe_interval(interval: dict[str, int]) -> str:
    ((unit, amount),) = interval.items()
    short = {"seconds": "s", "minutes": "min", "hours": "h"}[unit]
    return f"every {amount} {short}"


def job_statuses(ctx: JobContext) -> list[JobStatus]:
    settings = ctx.settings()
    with ctx.sessions() as session:
        try:
            last = job_runs.last_runs(session)
        except Exception:
            log.warning("could not read the job runs", exc_info=True)
            last = {}
    result = []
    for spec in JOBS:
        job = ctx.scheduler.get_job(spec.id) if ctx.scheduler is not None else None
        result.append(
            JobStatus(
                spec,
                describe_interval(spec.interval(settings)),
                spec.available(ctx),
                spec.enabled(settings),
                job.next_run_time if job is not None else None,
                last.get(spec.id),
            )
        )
    return result


def start_scheduler(
    sessions: sessionmaker[Session],
    sender: PushSender | None,
    settings: Settings,
    *,
    calendar: CalendarSource | None = None,
    claude: ClaudeClient | JobClaudeClient | None = None,
    reviewer: WeeklyReviewer | None = None,
    usage_log: UsageLog | None = None,
    school: school_sync.SchoolSources | None = None,
    context: JobContext | None = None,
) -> BackgroundScheduler:
    """Start the jobs of `JOBS` that can run here. `context` is the shared one (the app also uses
    it for Run now); without it one is built from the arguments."""
    ctx = context or JobContext(
        sessions,
        lambda: settings,
        sender,
        calendar,
        claude,
        reviewer,
        usage_log,
        school,
    )
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    ctx.scheduler = scheduler
    current = ctx.settings()
    for spec in JOBS:
        if spec.available(ctx) is not None:
            continue
        extra = {"next_run_time": datetime.now(UTC)} if spec.first_now else {}
        scheduler.add_job(
            _tick,
            "interval",
            args=(ctx, spec.id),
            id=spec.id,
            max_instances=1,
            coalesce=True,
            **spec.interval(current),
            **extra,
        )
    scheduler.start()
    return scheduler
