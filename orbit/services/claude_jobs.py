"""The Claude job queue (SPEC §2.19): questions for Claude, answered by the Claude Code worker.

`JobClaudeClient` has the same methods as `ClaudeClient`, so services do not know which one they
have. A call puts a job in the queue and waits a bounded time; on a timeout, a bad login or a rate
limit it raises `ClaudeError`, and the caller falls back exactly as it does without Claude.

The worker side (`claim`, `submit`, `fail`, `heartbeat`) is called by the worker endpoints. Service
functions take a session and the caller commits; `JobClaudeClient` opens its own short sessions,
because it waits and the worker must be able to see the job while it does.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from orbit import tracing
from orbit.domain.base import utcnow
from orbit.domain.claude_job import ClaudeJob, ClaudeWorker
from orbit.domain.enums import JobStatus, ReviewStatus
from orbit.domain.report import ReviewReport
from orbit.domain.review import WeeklyReview
from orbit.domain.usage import ClaudeCall
from orbit.integrations.claude import (
    DEFAULT_OWNER,
    BriefLine,
    BriefRequest,
    CaptureExtraction,
    CaptureRequest,
    ClaudeError,
    DraftRequest,
    JobSpec,
    MeetingLinkRequest,
    MeetingPeople,
    MessageDraft,
    ReportOutput,
    ReportRequest,
    ReviewOutput,
    SchoolReadOutput,
    SchoolReadRequest,
    TransactionClassification,
    TransactionRow,
    brief_spec,
    build_report_prompt,
    build_review_prompt,
    capture_spec,
    classification_spec,
    draft_spec,
    link_spec,
    report_system,
    review_system,
    school_read_spec,
    with_owner,
)
from orbit.integrations.ntfy import PushMessage, PushSender
from orbit.integrations.prompts_registry import prompt_version
from orbit.services import capture, notifications, reports, review
from orbit.services.errors import InvalidInputError, NotFoundError

log = logging.getLogger(__name__)

INTERACTIVE = 1  # someone is waiting for the answer
BACKGROUND = 2
REVIEW = 3  # the weekly review: runs when nothing else waits

MAX_ATTEMPTS = 2  # runs of one job (a dead worker, a bad answer)
LEASE = timedelta(minutes=10)  # a running job whose worker is silent this long is requeued
REVIEW_LEASE = timedelta(minutes=30)  # the review reads a week of context: it takes longer
REPORT_WAIT = timedelta(hours=2)  # a queued report waits this long for the worker (SPEC §2.22)
ALIVE_WINDOW = timedelta(minutes=3)  # the worker counts as running if seen this recently
DEDUPE_WINDOW = timedelta(seconds=60)  # the same question again this soon shares the first job
LATE_TTL = timedelta(hours=1)  # a late-capable job may still be answered this long after asking
LATE_GRACE = timedelta(seconds=2)  # the caller's own deadline plus this passes before "late"
DEFAULT_RATE_LIMIT = timedelta(minutes=15)  # when the worker reports a limit without a time
POLL_SECONDS = 0.5


@dataclass(frozen=True)
class KindSpec:
    priority: int
    output: type[BaseModel]
    # A late answer may still be used (the capture draft is replaced), so the job is kept.
    late: bool = False


KINDS: dict[str, KindSpec] = {
    "capture": KindSpec(INTERACTIVE, CaptureExtraction, late=True),
    "draft": KindSpec(INTERACTIVE, MessageDraft),
    "brief": KindSpec(BACKGROUND, BriefLine),
    "meeting_link": KindSpec(BACKGROUND, MeetingPeople),
    "classification": KindSpec(BACKGROUND, TransactionClassification),
    "review": KindSpec(REVIEW, ReviewOutput),
    "report": KindSpec(BACKGROUND, ReportOutput),
    "school_read": KindSpec(BACKGROUND, SchoolReadOutput),
}


@dataclass(frozen=True)
class QueueLimits:
    wait_interactive: float = 45.0
    wait_background: float = 120.0
    cap_interactive: int = 10
    cap_background: int = 50

    def wait_for(self, priority: int) -> float:
        return self.wait_interactive if priority == INTERACTIVE else self.wait_background

    def cap_for(self, priority: int) -> int:
        return self.cap_interactive if priority == INTERACTIVE else self.cap_background


class JobOutputError(InvalidInputError):
    """The worker's answer does not match the kind's schema; `errors` says how (retry once)."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"The answer does not match the schema: {len(errors)} errors.")
        self.errors = errors


# --- The worker row ---


def worker_row(session: Session) -> ClaudeWorker:
    row = session.get(ClaudeWorker, 1)
    if row is None:
        row = ClaudeWorker(id=1, auth_ok=True)
        session.add(row)
        session.flush()
    return row


def is_alive(worker: ClaudeWorker, now: datetime) -> bool:
    return worker.last_seen_at is not None and now - worker.last_seen_at <= ALIVE_WINDOW


def is_rate_limited(worker: ClaudeWorker, now: datetime) -> bool:
    return worker.rate_limited_until is not None and worker.rate_limited_until > now


def heartbeat(
    session: Session,
    *,
    cli_version: str | None = None,
    auth_ok: bool | None = None,
    rate_limited_until: datetime | None = None,
    selftest_ok: bool = False,
    now: datetime | None = None,
) -> ClaudeWorker:
    """The worker says it is alive (and, optionally, how its login and limits are)."""
    moment = now or utcnow()
    worker = worker_row(session)
    worker.last_seen_at = moment
    if cli_version is not None:
        worker.cli_version = cli_version
    if auth_ok is not None:
        worker.auth_ok = auth_ok
    if rate_limited_until is not None:
        worker.rate_limited_until = rate_limited_until
    elif worker.rate_limited_until is not None and worker.rate_limited_until <= moment:
        worker.rate_limited_until = None
    if selftest_ok:
        worker.last_selftest_at = moment
    session.flush()
    return worker


# --- Asking (Orbit side) ---


def input_hash(kind: str, prompt: str) -> str:
    return hashlib.sha256(f"{kind}\n{prompt}".encode()).hexdigest()


def _queued_count(session: Session, priority: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(ClaudeJob)
            .where(ClaudeJob.status == JobStatus.QUEUED, ClaudeJob.priority == priority)
        )
        or 0
    )


def enqueue(
    session: Session,
    spec: JobSpec[Any],
    *,
    limits: QueueLimits,
    late: dict[str, Any] | None = None,
    wait: bool = True,
    now: datetime | None = None,
) -> ClaudeJob:
    """Put a question in the queue (or share the identical one asked a moment ago).

    `wait=False` is for a caller that will not wait (the worker is away) but still wants a late
    answer: the deadline is then already over, so only a late-capable job is kept."""
    kind = KINDS[spec.purpose]
    moment = now or utcnow()
    digest = input_hash(spec.purpose, spec.prompt)
    same = session.scalars(
        select(ClaudeJob)
        .where(
            ClaudeJob.input_hash == digest,
            ClaudeJob.created_at >= moment - DEDUPE_WINDOW,
            ClaudeJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE)),
        )
        .order_by(ClaudeJob.id.desc())
    ).first()
    if same is not None:
        return same
    if _queued_count(session, kind.priority) >= limits.cap_for(kind.priority):
        raise ClaudeError("Claude's queue is full.")
    job = ClaudeJob(
        kind=spec.purpose,
        status=JobStatus.QUEUED,
        priority=kind.priority,
        input_hash=digest,
        system=spec.system,
        prompt=spec.prompt,
        schema_json=spec.output.model_json_schema(),
        # No caller waits (`wait=False`): the deadline is already past, grace included.
        wait_until=(
            moment + timedelta(seconds=limits.wait_for(kind.priority))
            if wait
            else moment - LATE_GRACE
        ),
        late_json=late if kind.late else None,
        created_at=moment,
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: int) -> ClaudeJob:
    job = session.get(ClaudeJob, job_id)
    if job is None:
        raise NotFoundError(f"Claude job {job_id} not found.")
    return job


def recent_jobs(session: Session, limit: int = 50) -> Sequence[ClaudeJob]:
    return session.scalars(select(ClaudeJob).order_by(ClaudeJob.id.desc()).limit(limit)).all()


def queue_depth(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ClaudeJob.status, func.count())
        .where(ClaudeJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
        .group_by(ClaudeJob.status)
    )
    depth = {status.value: count for status, count in rows}
    return {"queued": depth.get("queued", 0), "running": depth.get("running", 0)}


# --- Serving (worker side) ---


def housekeeping(session: Session, now: datetime) -> None:
    """Requeue jobs whose worker went silent and expire the ones nobody waits for any more."""
    stale = session.scalars(
        select(ClaudeJob).where(ClaudeJob.status == JobStatus.RUNNING, ClaudeJob.lease_until < now)
    )
    for job in stale:
        if job.review_id is not None:
            _fail_review(session, job, "The worker stopped answering.", now)
        elif job.attempts < MAX_ATTEMPTS:
            job.status = JobStatus.QUEUED
            job.lease_until = None
        else:
            job.status = JobStatus.FAILED
            job.error = "The worker stopped answering."
            job.finished_at = now
            _close_report(session, job)
    queued = session.scalars(select(ClaudeJob).where(ClaudeJob.status == JobStatus.QUEUED))
    for job in queued:
        if job.late_json is not None:
            gone = job.created_at + LATE_TTL < now
        else:
            gone = job.wait_until < now
        if gone:
            job.status = JobStatus.EXPIRED
            job.finished_at = now
            job.error = job.error or "Claude Code on the server did not take the job in time."
            _close_report(session, job)
    session.flush()


def claim(
    session: Session, now: datetime | None = None, *, interactive_only: bool = False
) -> ClaudeJob | None:
    """The next job for the worker: interactive before background, oldest first.

    `interactive_only` is the worker's reserved slot (SPEC §2.34): it takes only jobs somebody
    is waiting for (capture, draft), so a long school read or report never blocks them."""
    moment = now or utcnow()
    housekeeping(session, moment)
    query = select(ClaudeJob).where(ClaudeJob.status == JobStatus.QUEUED)
    if interactive_only:
        query = query.where(ClaudeJob.priority == INTERACTIVE)
    job = session.scalars(
        query.order_by(ClaudeJob.priority, ClaudeJob.id).limit(1).with_for_update(skip_locked=True)
    ).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.claimed_at = moment
    job.lease_until = moment + (REVIEW_LEASE if job.report_id is not None else LEASE)
    job.attempts += 1
    session.flush()
    return job


def start_review_job(
    session: Session,
    *,
    tz: ZoneInfo,
    budget_chars: int,
    now: datetime | None = None,
    timing: review.ReviewTiming | None = None,
    owner: str = DEFAULT_OWNER,
) -> ClaudeJob | None:
    """When the weekly review is due, claim its week and hand the worker the job to make it.

    Nothing starts while the worker's login is bad or it is rate limited, so no attempt is wasted.
    The job is already running (the worker asked for work); the review row keeps the retry rules."""
    moment = now or utcnow()
    worker = worker_row(session)
    if not worker.auth_ok or is_rate_limited(worker, moment):
        return None
    plan_start = review.due_plan_start(moment, tz, timing)
    if plan_start is None:
        return None
    row = review.claim(session, plan_start, moment)
    if row is None:
        return None
    session.commit()  # the claim is stored before anyone is asked
    request = replace(
        review.prepare(session, row, tz=tz, budget_chars=budget_chars, now=moment), owner=owner
    )
    prompt = build_review_prompt(request)
    job = ClaudeJob(
        kind="review",
        status=JobStatus.RUNNING,
        priority=REVIEW,
        input_hash=input_hash("review", prompt),
        system=review_system(request),
        prompt=prompt,
        schema_json=ReviewOutput.model_json_schema(),
        wait_until=moment,
        review_id=row.id,
        attempts=1,
        claimed_at=moment,
        lease_until=moment + REVIEW_LEASE,
        created_at=moment,
    )
    session.add(job)
    session.flush()
    return job


def enqueue_report(
    session: Session,
    report: ReviewReport,
    request: ReportRequest,
    now: datetime | None = None,
    owner: str = DEFAULT_OWNER,
) -> ClaudeJob:
    """Queue the question of a report (SPEC §2.22). The worker takes it when it is free; the job
    waits REPORT_WAIT, and a report that nobody answered in time is failed by `housekeeping`."""
    moment = now or utcnow()
    request = replace(request, owner=owner)
    prompt = build_report_prompt(request)
    job = ClaudeJob(
        kind="report",
        status=JobStatus.QUEUED,
        priority=KINDS["report"].priority,
        input_hash=input_hash("report", prompt),
        system=report_system(request),
        prompt=prompt,
        schema_json=ReportOutput.model_json_schema(),
        wait_until=moment + REPORT_WAIT,
        report_id=report.id,
        created_at=moment,
    )
    session.add(job)
    session.flush()
    return job


def submit(
    session: Session,
    job_id: int,
    output: dict[str, Any],
    *,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int | None = None,
    now: datetime | None = None,
) -> ClaudeJob:
    """Store the worker's answer, after checking it against the kind's schema."""
    moment = now or utcnow()
    job = get_job(session, job_id)
    if job.status is not JobStatus.RUNNING:
        raise InvalidInputError(f"Job {job_id} is {job.status.value}, not running.")
    try:
        parsed = KINDS[job.kind].output.model_validate(output)
    except ValidationError as exc:
        raise JobOutputError(
            [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        ) from exc
    job.error = None
    if job.review_id is not None and isinstance(parsed, ReviewOutput):
        row = review.get_review(session, job.review_id)
        if row.status is ReviewStatus.PENDING:
            review.finish(
                session,
                row,
                parsed,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        else:
            job.error = f"The review was already {row.status.value}."
    if job.report_id is not None and isinstance(parsed, ReportOutput):
        report = session.get(ReviewReport, job.report_id)
        if report is None:
            job.error = "The report was deleted."
        elif report.status is ReviewStatus.PENDING:
            reports.finish(
                session,
                report,
                parsed,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        else:
            job.error = f"The report was already {report.status.value}."
    job.status = JobStatus.DONE
    job.result_json = parsed.model_dump(mode="json")
    job.finished_at = moment
    job.lease_until = None
    job.model = model
    job.input_tokens = input_tokens
    job.output_tokens = output_tokens
    job.duration_ms = duration_ms
    session.add(
        ClaudeCall(
            created_at=moment,
            purpose=job.kind,
            model=model or "claude-code",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ok=True,
            latency_ms=duration_ms,
            prompt_version=prompt_version(job.kind),
            cost_micro_eur=0,  # the worker runs on a subscription: no per-token price
        )
    )
    tracing.record_worker_job(
        job.kind,
        model or "claude-code",
        input_tokens,
        output_tokens,
        duration_ms,
        True,
        prompt_version(job.kind),
        _ns(moment),
    )
    session.flush()
    return job


def _ns(moment: datetime) -> int:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return int(aware.timestamp() * 1_000_000_000)


FailureKind = Literal["auth", "rate_limit", "other"]


def _fail_review(
    session: Session, job: ClaudeJob, error: str, now: datetime, *, spare_attempt: bool = False
) -> None:
    """A review job ended without an answer: the job fails, and the review is marked failed so the
    schedule can try the week again."""
    job.status = JobStatus.FAILED
    job.error = error[:2000]
    job.finished_at = now
    job.lease_until = None
    if job.review_id is None:
        return
    row = session.get(WeeklyReview, job.review_id)
    if row is not None and row.status is ReviewStatus.PENDING:
        row.status = ReviewStatus.FAILED
        row.error = error[:1000]
        if spare_attempt:
            row.attempts = max(row.attempts - 1, 0)


def _close_report(session: Session, job: ClaudeJob) -> None:
    """A report job that ended without an answer (failed or expired) fails its report; the page
    then offers Try again. A job still queued or running leaves the report pending."""
    if job.report_id is not None and job.status in (JobStatus.FAILED, JobStatus.EXPIRED):
        reports.fail(session, job.report_id, job.error or "Claude gave no answer.")


def fail(
    session: Session,
    job_id: int,
    error: str,
    *,
    kind: FailureKind = "other",
    retry_after: timedelta | None = None,
    now: datetime | None = None,
) -> ClaudeJob:
    """The worker could not answer. A rate limit puts the job back (the worker is paused);
    a bad login fails it; anything else gets one more try."""
    moment = now or utcnow()
    job = get_job(session, job_id)
    if job.status is not JobStatus.RUNNING:
        raise InvalidInputError(f"Job {job_id} is {job.status.value}, not running.")
    job.error = error[:2000]
    job.lease_until = None
    worker = worker_row(session)
    if job.review_id is not None:
        # The review row keeps the attempt count and the retry rules (§2.17); a rate limit or a
        # bad login is not the review's fault, so it does not use up an attempt.
        _fail_review(session, job, error, moment, spare_attempt=kind != "other")
        if kind == "rate_limit":
            worker.rate_limited_until = moment + (retry_after or DEFAULT_RATE_LIMIT)
        elif kind == "auth":
            worker.auth_ok = False
    elif kind == "rate_limit":
        worker.rate_limited_until = moment + (retry_after or DEFAULT_RATE_LIMIT)
        job.status = JobStatus.QUEUED
        job.attempts = max(job.attempts - 1, 0)  # not the job's fault
    elif kind == "auth":
        worker.auth_ok = False
        job.status = JobStatus.FAILED
        job.finished_at = moment
    elif job.attempts < MAX_ATTEMPTS:
        job.status = JobStatus.QUEUED
    else:
        job.status = JobStatus.FAILED
        job.finished_at = moment
    _close_report(session, job)
    session.add(
        ClaudeCall(
            created_at=moment,
            purpose=job.kind,
            model=job.model or "claude-code",
            input_tokens=0,
            output_tokens=0,
            ok=False,
            prompt_version=prompt_version(job.kind),
        )
    )
    tracing.record_worker_job(
        job.kind,
        job.model or "claude-code",
        0,
        0,
        None,
        False,
        prompt_version(job.kind),
        _ns(moment),
    )
    session.flush()
    return job


def rerun(
    session: Session, job_id: int, limits: QueueLimits, now: datetime | None = None
) -> ClaudeJob:
    """Ask again for a job that failed, expired or was cancelled (while its prompt is kept)."""
    moment = now or utcnow()
    job = get_job(session, job_id)
    if job.status not in (JobStatus.FAILED, JobStatus.EXPIRED, JobStatus.CANCELLED):
        raise InvalidInputError(f"Job {job_id} is {job.status.value}; only finished ones rerun.")
    if job.review_id is not None:
        raise InvalidInputError("A review is retried by the weekly schedule, not rerun here.")
    if job.report_id is not None:
        raise InvalidInputError("A report is tried again from its own page.")
    if not job.prompt:
        raise InvalidInputError("The prompt of this job was already cleared.")
    job.status = JobStatus.QUEUED
    job.attempts = 0
    job.error = None
    job.finished_at = None
    job.result_json = None
    job.consumed = False
    job.wait_until = moment + timedelta(seconds=limits.wait_for(job.priority))
    session.flush()
    return job


def prune(session: Session, retention_days: int, now: datetime | None = None) -> int:
    """Clear what was sent and answered in finished jobs older than the retention period."""
    moment = now or utcnow()
    old = session.scalars(
        select(ClaudeJob).where(
            ClaudeJob.created_at < moment - timedelta(days=retention_days),
            ClaudeJob.status.in_(
                (JobStatus.DONE, JobStatus.FAILED, JobStatus.EXPIRED, JobStatus.CANCELLED)
            ),
            ClaudeJob.prompt != "",
        )
    ).all()
    for job in old:
        job.system = ""
        job.prompt = ""
        job.schema_json = {}
        job.result_json = None
        job.late_json = None
    session.flush()
    return len(old)


# --- Late answers ---


@dataclass(frozen=True)
class LateContext:
    sender: PushSender | None
    base_url: str
    quiet: notifications.QuietHours
    tz: ZoneInfo


def _late_capture(session: Session, job: ClaudeJob, ctx: LateContext) -> str | None:
    """Use a capture answer that came after the request fell back to a note draft."""
    late = job.late_json or {}
    extraction = CaptureExtraction.model_validate(job.result_json)
    result = capture.upgrade_draft(
        session,
        extraction,
        text=str(late["text"]),
        since=job.created_at,
        goal_ids=[int(i) for i in late.get("goal_ids", [])],
        default_currency=str(late["default_currency"]),
        tz=ctx.tz,
    )
    if result is None:
        return None
    if result.outcome == "applied":
        notifications.notify(
            session,
            ctx.sender,
            PushMessage(
                title="📥 Capture ready",
                body=result.summary or "Your dictation is in the Inbox.",
                click=f"{ctx.base_url}/inbox",
                tags=("inbox_tray",),
            ),
            dedupe_key=f"late_capture:{job.id}",
            kind="late_capture",
            quiet=ctx.quiet,
        )
    return result.outcome


def _give_up_failed_captures(session: Session, now: datetime) -> None:
    """A deferred capture whose job failed or expired keeps its note draft; the draft stops
    saying it is being read (SPEC §2.33)."""
    dead = session.scalars(
        select(ClaudeJob).where(
            ClaudeJob.kind == "capture",
            ClaudeJob.status.in_((JobStatus.FAILED, JobStatus.EXPIRED, JobStatus.CANCELLED)),
            ClaudeJob.late_json.is_not(None),
            ClaudeJob.created_at >= now - 2 * LATE_TTL,
        )
    ).all()
    for job in dead:
        late = job.late_json or {}
        if "outcome" in late:
            continue
        capture.give_up(session, text=str(late.get("text", "")), since=job.created_at)
        job.late_json = {**late, "outcome": "no answer"}


def apply_late(session: Session, ctx: LateContext, now: datetime | None = None) -> int:
    """Use answers that arrived after their caller gave up. Returns how many were applied.

    A job whose draft is not saved yet is tried again on the next call, for up to LATE_TTL."""
    moment = now or utcnow()
    housekeeping(session, moment)  # expire what nobody took, so its draft stops "processing"
    _give_up_failed_captures(session, moment)
    waiting = session.scalars(
        select(ClaudeJob).where(
            ClaudeJob.status == JobStatus.DONE,
            ClaudeJob.consumed.is_(False),
            ClaudeJob.late_json.is_not(None),
            ClaudeJob.wait_until < moment - LATE_GRACE,
        )
    ).all()
    applied = 0
    for job in waiting:
        outcome = _late_capture(session, job, ctx) if job.kind == "capture" else "unsupported"
        if outcome is None and job.created_at + LATE_TTL > moment:
            continue  # the draft is not there yet
        job.consumed = True
        job.late_json = {**(job.late_json or {}), "outcome": outcome or "no draft found"}
        if outcome == "applied":
            applied += 1
        else:
            log.info("late answer of job %s not used: %s", job.id, job.late_json["outcome"])
    session.flush()
    return applied


# --- The client services use ---


class JobClaudeClient:
    """The methods of `ClaudeClient`, answered through the queue. Not the weekly review."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        limits: QueueLimits | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
        owner_name: str = DEFAULT_OWNER,
    ) -> None:
        self._owner = owner_name
        self._sessions = sessions
        self._limits = limits or QueueLimits()
        self._sleep = sleep
        self._clock = clock

    def _ask[T: BaseModel](self, spec: JobSpec[T], late: dict[str, Any] | None = None) -> T:
        kind = KINDS[spec.purpose]
        now = self._clock()
        with self._sessions() as session:
            worker = worker_row(session)
            if not worker.auth_ok:
                raise ClaudeError("Claude Code on the server is not logged in.")
            away = not is_alive(worker, now)
            limited = is_rate_limited(worker, now)
            # Nobody waits for a worker that is away; a rate limit only stops those who would.
            if away or (limited and kind.priority == INTERACTIVE):
                if kind.late:
                    enqueue(session, spec, limits=self._limits, late=late, wait=False, now=now)
                    session.commit()
                reason = "is not running" if away else "is rate limited"
                raise ClaudeError(f"Claude Code on the server {reason}.")
            job = enqueue(session, spec, limits=self._limits, late=late, now=now)
            job_id, deadline = job.id, job.wait_until
            session.commit()
        return self._wait(spec, job_id, deadline)

    def _wait[T: BaseModel](self, spec: JobSpec[T], job_id: int, deadline: datetime) -> T:
        while True:
            with self._sessions() as session:
                job = get_job(session, job_id)
                if job.status is JobStatus.DONE:
                    job.consumed = True
                    session.commit()
                    return spec.output.model_validate(job.result_json)
                if job.status in (JobStatus.FAILED, JobStatus.EXPIRED, JobStatus.CANCELLED):
                    raise ClaudeError(job.error or f"Claude job {job.status.value}.")
                if self._clock() >= deadline:
                    if job.status is JobStatus.QUEUED and job.late_json is None:
                        job.status = JobStatus.CANCELLED
                        job.finished_at = self._clock()
                        session.commit()
                    raise ClaudeError("Claude did not answer in time.")
            self._sleep(POLL_SECONDS)

    @staticmethod
    def _late_data(request: CaptureRequest) -> dict[str, Any]:
        return {
            "text": request.text,
            "default_currency": request.default_currency,
            "goal_ids": [g.id for g in request.goals],
            "today": request.today.isoformat(),
        }

    def extract_capture(self, request: CaptureRequest) -> CaptureExtraction:
        return self._ask(capture_spec(with_owner(request, self._owner)), self._late_data(request))

    def defer_capture(self, request: CaptureRequest) -> None:
        """Queue a capture without waiting: the answer is used when it arrives (`apply_late`),
        which replaces the note draft saved meanwhile. Raises ClaudeError if the queue is full."""
        with self._sessions() as session:
            enqueue(
                session,
                capture_spec(with_owner(request, self._owner)),
                limits=self._limits,
                late=self._late_data(request),
                wait=False,
            )
            session.commit()

    def classify_transactions(
        self, rows: Sequence[TransactionRow], shared_min: str
    ) -> TransactionClassification:
        return self._ask(classification_spec(rows, shared_min, self._owner))

    def link_meeting(self, request: MeetingLinkRequest) -> MeetingPeople:
        return self._ask(link_spec(with_owner(request, self._owner)))

    def summarize_brief(self, request: BriefRequest) -> BriefLine:
        return self._ask(brief_spec(with_owner(request, self._owner)))

    def read_school(self, request: SchoolReadRequest) -> SchoolReadOutput:
        return self._ask(school_read_spec(with_owner(request, self._owner)))

    def draft_message(self, request: DraftRequest) -> MessageDraft:
        return self._ask(draft_spec(with_owner(request, self._owner)))

    def close(self) -> None:
        pass


WORKER_DOWN_AFTER = timedelta(minutes=10)


def send_alerts(
    session: Session,
    sender: PushSender | None,
    *,
    base_url: str,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> int:
    """One push a day for each of: the login expired, or jobs wait while the worker is away.

    The dedupe key holds the day, so a problem that lasts is mentioned once a day. Returns how many
    pushes were recorded now."""
    moment = now or utcnow()
    worker = worker_row(session)
    day = moment.date().isoformat()
    sent = 0
    url = f"{base_url}/ops/claude"
    if not worker.auth_ok:
        record = notifications.notify(
            session,
            sender,
            PushMessage(
                title="🔑 Claude login expired",
                body=(
                    "Claude Code on the server is not logged in, so Orbit's Claude features "
                    "fall back to plain modes. On the server run `claude setup-token` as the "
                    "worker user and update CLAUDE_CODE_OAUTH_TOKEN in /etc/orbit-review.env."
                ),
                click=url,
                tags=("key",),
            ),
            dedupe_key=f"claude_auth:{day}",
            kind="claude_worker",
            quiet=quiet,
            now=moment,
        )
        sent += record is not None
    seen = worker.last_seen_at
    silent = seen is None or moment - seen > WORKER_DOWN_AFTER
    oldest = session.scalar(
        select(func.min(ClaudeJob.created_at)).where(
            ClaudeJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING))
        )
    )
    if silent and oldest is not None and moment - oldest > WORKER_DOWN_AFTER:
        record = notifications.notify(
            session,
            sender,
            PushMessage(
                title="⚠️ Claude worker is down",
                body=(
                    "Jobs are waiting but the Claude Code worker has not been seen for "
                    f"{WORKER_DOWN_AFTER.seconds // 60} minutes. Check "
                    "`systemctl status orbit-claude-worker` on the server."
                ),
                click=url,
                tags=("warning",),
            ),
            dedupe_key=f"claude_worker_down:{day}",
            kind="claude_worker",
            quiet=quiet,
            now=moment,
        )
        sent += record is not None
    session.flush()
    return sent


def status_json(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """The worker's state and the queue, for the status endpoint and the ops page."""
    moment = now or utcnow()
    worker = worker_row(session)
    return {
        "alive": is_alive(worker, moment),
        "last_seen_at": worker.last_seen_at.isoformat() if worker.last_seen_at else None,
        "cli_version": worker.cli_version,
        "auth_ok": worker.auth_ok,
        "rate_limited_until": worker.rate_limited_until.isoformat()
        if worker.rate_limited_until and worker.rate_limited_until > moment
        else None,
        "queue": queue_depth(session),
    }
