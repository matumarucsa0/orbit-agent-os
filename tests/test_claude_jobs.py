"""The Claude job queue and the client built on it (SPEC §2.19)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from orbit.db import make_engine, make_sessionmaker
from orbit.domain.claude_job import ClaudeJob
from orbit.domain.enums import JobStatus, ProposalKind, ProposalStatus
from orbit.domain.inbox import Notification, Proposal
from orbit.domain.usage import ClaudeCall
from orbit.integrations.claude import (
    BriefLine,
    BriefPerson,
    BriefRequest,
    CaptureExtraction,
    CaptureRequest,
    ClaudeError,
    DraftRequest,
    JobSpec,
    MessageDraft,
    capture_spec,
    draft_spec,
)
from orbit.services import capture, claude_jobs, notifications
from orbit.services.claude_jobs import (
    BACKGROUND,
    INTERACTIVE,
    JobClaudeClient,
    JobOutputError,
    LateContext,
    QueueLimits,
)
from orbit.services.errors import InvalidInputError
from tests.conftest import TEST_DATABASE_URL, reset_schema

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
LIMITS = QueueLimits(wait_interactive=45, wait_background=120, cap_interactive=3, cap_background=3)
TODAY = date(2026, 10, 6)


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = make_engine(TEST_DATABASE_URL)
    reset_schema(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


def note_answer(text: str = "x") -> dict[str, Any]:
    return {
        "notes": [{"person_ref": "new:0", "text": text}],
        "interactions": [],
        "debts": [],
        "promises": [],
        "unknown_people": [{"name": "Kamil", "context": "a friend"}],
        "confidence": 0.9,
    }


def capture_request(text: str = "Kamil má rád kávu") -> CaptureRequest:
    return CaptureRequest(text=text, today=TODAY, candidates=(), default_currency="EUR", goals=())


def draft_request(name: str = "Paťo") -> DraftRequest:
    return DraftRequest(
        occasion="birthday", name=name, language="sk", today=TODAY, date=TODAY, style="casual"
    )


def spec(text: str = "Paťo") -> JobSpec[MessageDraft]:
    """A draft question; the name makes each text a different question."""
    return draft_spec(draft_request(text))


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def alive(sessions: sessionmaker[Session], now: datetime = T0, **kw: Any) -> None:
    with sessions() as s:
        claude_jobs.heartbeat(s, now=now, **kw)
        s.commit()


def serve(
    sessions: sessionmaker[Session], output: dict[str, Any], now: datetime = T0
) -> int | None:
    """What the worker does: claim the next job and answer it."""
    with sessions() as s:
        job = claude_jobs.claim(s, now)
        if job is None:
            return None
        claude_jobs.submit(s, job.id, output, model="m", input_tokens=10, output_tokens=5, now=now)
        s.commit()
        return job.id


# --- Queue ---


def test_the_same_question_shares_a_job(db: Session) -> None:
    first = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0)
    again = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0 + timedelta(seconds=30))
    other = claude_jobs.enqueue(db, spec("else"), limits=LIMITS, now=T0)
    later = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0 + timedelta(minutes=5))
    assert again.id == first.id
    assert other.id != first.id and later.id != first.id


def test_a_full_queue_refuses_more(db: Session) -> None:
    for n in range(3):
        claude_jobs.enqueue(db, spec(f"q{n}"), limits=LIMITS, now=T0)
    with pytest.raises(ClaudeError, match="full"):
        claude_jobs.enqueue(db, spec("one too many"), limits=LIMITS, now=T0)


def test_interactive_jobs_go_before_background_ones(db: Session) -> None:
    brief = JobSpec("brief", "s", "b", BriefLine)
    background = claude_jobs.enqueue(db, brief, limits=LIMITS, now=T0)
    interactive = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0 + timedelta(seconds=1))
    assert (background.priority, interactive.priority) == (BACKGROUND, INTERACTIVE)
    first = claude_jobs.claim(db, T0 + timedelta(seconds=2))
    second = claude_jobs.claim(db, T0 + timedelta(seconds=2))
    assert first is not None and second is not None
    assert (first.id, second.id) == (interactive.id, background.id)
    assert claude_jobs.claim(db, T0 + timedelta(seconds=2)) is None


def test_a_job_nobody_waits_for_expires_but_a_capture_is_kept(db: Session) -> None:
    gone = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0)
    kept = claude_jobs.enqueue(
        db,
        capture_spec(capture_request()),
        limits=LIMITS,
        late={"text": "t", "default_currency": "EUR", "goal_ids": []},
        now=T0,
    )
    job = claude_jobs.claim(db, T0 + timedelta(minutes=10))
    assert job is not None and job.id == kept.id
    assert gone.status is JobStatus.EXPIRED


def test_a_silent_worker_loses_nothing(db: Session) -> None:
    job = claude_jobs.enqueue(db, spec(), limits=QueueLimits(wait_interactive=3600), now=T0)
    assert claude_jobs.claim(db, T0) is not None
    assert claude_jobs.claim(db, T0 + timedelta(minutes=11)) is not None  # requeued, second run
    assert job.attempts == 2
    assert claude_jobs.claim(db, T0 + timedelta(minutes=30)) is None
    assert job.status is JobStatus.FAILED  # out of attempts


def test_submit_checks_the_answer_and_logs_usage(db: Session) -> None:
    job = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0)
    claude_jobs.claim(db, T0)
    with pytest.raises(JobOutputError) as caught:
        claude_jobs.submit(db, job.id, {"wrong": 1}, now=T0)
    assert caught.value.errors and job.status is JobStatus.RUNNING  # it may answer again
    claude_jobs.submit(
        db, job.id, {"text": "Všetko najlepšie!"}, model="m", input_tokens=7, output_tokens=3
    )
    assert job.status is JobStatus.DONE and job.result_json == {"text": "Všetko najlepšie!"}
    call = db.query(ClaudeCall).one()
    assert (call.purpose, call.model, call.input_tokens, call.output_tokens, call.ok) == (
        "draft",
        "m",
        7,
        3,
        True,
    )
    with pytest.raises(InvalidInputError, match="not running"):
        claude_jobs.submit(db, job.id, {"text": "again"})


def test_failures(db: Session) -> None:
    job = claude_jobs.enqueue(db, spec(), limits=QueueLimits(wait_interactive=3600), now=T0)
    claude_jobs.claim(db, T0)
    claude_jobs.fail(db, job.id, "boom", now=T0)  # first failure: another try
    assert job.status is JobStatus.QUEUED
    claude_jobs.claim(db, T0)
    claude_jobs.fail(db, job.id, "boom again", now=T0)
    assert job.status is JobStatus.FAILED and job.error == "boom again"

    limited = claude_jobs.enqueue(
        db, spec("two"), limits=QueueLimits(wait_interactive=3600), now=T0
    )
    claude_jobs.claim(db, T0)
    claude_jobs.fail(
        db, limited.id, "429", kind="rate_limit", retry_after=timedelta(minutes=20), now=T0
    )
    worker = claude_jobs.worker_row(db)
    assert limited.status is JobStatus.QUEUED and limited.attempts == 0
    assert worker.rate_limited_until == T0 + timedelta(minutes=20)
    limited.status = JobStatus.CANCELLED  # (the next claim would take it again)

    login = claude_jobs.enqueue(
        db, spec("three"), limits=QueueLimits(wait_interactive=3600), now=T0
    )
    claude_jobs.claim(db, T0)
    claude_jobs.fail(db, login.id, "not logged in", kind="auth", now=T0)
    assert login.status is JobStatus.FAILED and worker.auth_ok is False
    claude_jobs.heartbeat(db, auth_ok=True, now=T0)
    assert worker.auth_ok is True


def test_rerun_and_prune(db: Session) -> None:
    job = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0)
    with pytest.raises(InvalidInputError, match="only finished"):
        claude_jobs.rerun(db, job.id, LIMITS, T0)
    claude_jobs.claim(db, T0)
    claude_jobs.claim(db, T0)
    job.status, job.error = JobStatus.FAILED, "x"
    claude_jobs.rerun(db, job.id, LIMITS, T0 + timedelta(minutes=1))
    assert job.status is JobStatus.QUEUED and job.error is None and job.attempts == 0

    job.status = JobStatus.DONE
    job.result_json = {"text": "t"}
    assert claude_jobs.prune(db, 7, T0 + timedelta(days=3)) == 0
    assert claude_jobs.prune(db, 7, T0 + timedelta(days=8)) == 1
    assert (job.prompt, job.system, job.result_json) == (
        "",
        "",
        None,
    ) and job.status is JobStatus.DONE
    assert claude_jobs.prune(db, 7, T0 + timedelta(days=9)) == 0
    job.status = JobStatus.FAILED
    with pytest.raises(InvalidInputError, match="cleared"):
        claude_jobs.rerun(db, job.id, LIMITS, T0)


# --- The client ---


def make_client(
    sessions: sessionmaker[Session], clock: Clock, on_sleep: Callable[[], None] | None = None
) -> JobClaudeClient:
    def sleep(seconds: float) -> None:
        clock.advance(seconds)
        if on_sleep is not None:
            on_sleep()

    return JobClaudeClient(sessions, LIMITS, sleep=sleep, clock=clock)


def test_the_client_returns_the_workers_answer(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions)
    answered: list[int | None] = []
    client = make_client(
        sessions, clock, lambda: answered.append(serve(sessions, {"text": "Hurá!"}, clock.now))
    )
    assert client.draft_message(draft_request()) == MessageDraft(text="Hurá!")
    with sessions() as s:
        job = s.query(ClaudeJob).one()
        assert job.status is JobStatus.DONE and job.consumed


def test_the_client_gives_up_and_cancels_a_queued_job(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions)
    client = make_client(sessions, clock)  # the worker never answers
    with pytest.raises(ClaudeError, match="in time"):
        client.draft_message(draft_request())
    assert (clock.now - T0).total_seconds() >= LIMITS.wait_interactive
    with sessions() as s:
        assert s.query(ClaudeJob).one().status is JobStatus.CANCELLED


def test_a_failed_job_is_an_error_for_the_caller(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions)

    def fail_it() -> None:
        with sessions() as s:
            job = claude_jobs.claim(s, clock.now)
            if job is not None:
                claude_jobs.fail(s, job.id, "no good", kind="auth", now=clock.now)
                s.commit()

    with pytest.raises(ClaudeError, match="no good"):
        make_client(sessions, clock, fail_it).draft_message(draft_request())


def test_the_client_fails_fast_when_the_login_is_bad(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions, auth_ok=False)
    with pytest.raises(ClaudeError, match="not logged in"):
        make_client(sessions, clock).draft_message(draft_request())
    assert clock.now == T0  # it did not wait
    with sessions() as s:
        assert s.query(ClaudeJob).count() == 0


def test_the_client_fails_fast_when_the_worker_is_away(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions, now=T0 - timedelta(minutes=10))
    with pytest.raises(ClaudeError, match="not running"):
        make_client(sessions, clock).draft_message(draft_request())
    assert clock.now == T0
    with sessions() as s:
        assert s.query(ClaudeJob).count() == 0  # nobody will use a late draft


def test_a_rate_limit_stops_the_waiting_ones_only(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions, rate_limited_until=T0 + timedelta(minutes=10))
    client = make_client(sessions, clock)
    with pytest.raises(ClaudeError, match="rate limited"):
        client.draft_message(draft_request())
    assert clock.now == T0
    # A brief is background work: it waits in the queue for the limit to pass.
    brief = BriefRequest("Lunch", TODAY, (BriefPerson("Paťo", ()),))
    answers: list[int | None] = []
    client = make_client(
        sessions, clock, lambda: answers.append(serve(sessions, {"summary": "ok"}, clock.now))
    )
    assert client.summarize_brief(brief).summary == "ok"


def test_a_capture_is_kept_when_the_worker_is_away(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    with pytest.raises(ClaudeError):
        make_client(sessions, clock).extract_capture(capture_request())
    with sessions() as s:
        job = s.query(ClaudeJob).one()
        assert job.status is JobStatus.QUEUED and job.late_json is not None


# --- A late capture ---


def late_ctx() -> LateContext:
    return LateContext(
        sender=None,
        base_url="https://orbit.test",
        quiet=notifications.QuietHours(time(22, 0), time(8, 0), capture.DEFAULT_TZ),
        tz=capture.DEFAULT_TZ,
    )


def dictate(sessions: sessionmaker[Session], clock: Clock, text: str) -> None:
    """A capture whose request gives up before the worker answers: a note draft is saved."""
    alive(sessions)
    client = make_client(sessions, clock)
    with sessions() as s:
        result = capture.capture(
            s, client, text, today=TODAY, default_currency="EUR", now=clock.now
        )
        s.commit()
    assert result.proposals[0].payload_json["notes"][0]["text"] == text


def test_a_late_answer_replaces_the_untouched_draft(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    dictate(sessions, clock, "Kamil má rád kávu")
    serve(sessions, note_answer("Likes coffee"), clock.now)
    with sessions() as s:
        assert claude_jobs.apply_late(s, late_ctx(), clock.now + timedelta(minutes=1)) == 1
        s.commit()
    with sessions() as s:
        proposals = s.query(Proposal).order_by(Proposal.id).all()
        assert [p.kind for p in proposals] == [ProposalKind.CAPTURE]
        payload = capture.CapturePayload.model_validate(proposals[0].payload_json)
        assert payload.notes[0].text == "Likes coffee" and payload.notes[0].person == "new:0"
        assert [p.name for p in payload.new_people] == ["Kamil"]
        assert proposals[0].status is ProposalStatus.PENDING and proposals[0].confidence == 0.9
        assert s.query(ClaudeJob).one().consumed
        assert s.query(Notification).one().title == "📥 Capture ready"
        # a second pass does nothing more
        assert claude_jobs.apply_late(s, late_ctx(), clock.now + timedelta(minutes=2)) == 0


def test_a_late_answer_leaves_an_edited_or_resolved_draft_alone(
    sessions: sessionmaker[Session],
) -> None:
    clock = Clock()
    dictate(sessions, clock, "Kamil má rád kávu")
    with sessions() as s:
        proposal = s.query(Proposal).one()
        payload = capture.CapturePayload.model_validate(proposal.payload_json)
        payload.notes[0].text = "my own wording"
        proposal.payload_json = payload.model_dump(mode="json")
        s.commit()
    serve(sessions, note_answer(), clock.now)
    with sessions() as s:
        assert claude_jobs.apply_late(s, late_ctx(), clock.now + timedelta(minutes=1)) == 0
        s.commit()
        assert s.query(Proposal).count() == 1
        assert s.query(Proposal).one().payload_json["notes"][0]["text"] == "my own wording"
        assert "edited" in (s.query(ClaudeJob).one().late_json or {})["outcome"]

    clock2 = Clock(T0 + timedelta(hours=3))
    dictate(sessions, clock2, "Jano volal")
    with sessions() as s:
        proposal = s.query(Proposal).order_by(Proposal.id.desc()).first()
        assert proposal is not None
        proposal.status = ProposalStatus.REJECTED
        proposal.resolved_at = clock2.now
        s.commit()
    serve(sessions, note_answer("x"), clock2.now)
    with sessions() as s:
        assert claude_jobs.apply_late(s, late_ctx(), clock2.now + timedelta(minutes=1)) == 0


def test_a_late_answer_waits_for_a_draft_that_is_not_saved_yet(
    sessions: sessionmaker[Session],
) -> None:
    clock = Clock()
    alive(sessions)
    with pytest.raises(ClaudeError):
        make_client(sessions, clock).extract_capture(capture_request("Peter volal"))
    serve(sessions, note_answer(), clock.now)
    with sessions() as s:
        assert claude_jobs.apply_late(s, late_ctx(), clock.now + timedelta(minutes=1)) == 0
        assert not s.query(ClaudeJob).one().consumed  # tried again later
        s.commit()


def test_an_answer_the_caller_used_is_not_applied_again(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions)
    client = make_client(
        sessions, clock, lambda: serve(sessions, note_answer("seen"), clock.now) and None
    )
    result = client.extract_capture(capture_request("Peter volal"))
    assert isinstance(result, CaptureExtraction) and result.notes[0].text == "seen"
    with sessions() as s:
        assert claude_jobs.apply_late(s, late_ctx(), clock.now + timedelta(minutes=5)) == 0


def test_stored_prompts_are_masked(sessions: sessionmaker[Session]) -> None:
    clock = Clock()
    alive(sessions)
    with pytest.raises(ClaudeError):
        make_client(sessions, clock).extract_capture(
            capture_request("Platba na SK3112000000198742637541, volal na +421 905 123 456")
        )
    with sessions() as s:
        prompt = s.query(ClaudeJob).one().prompt
    assert "198742637541" not in prompt and "905 123" not in prompt


# --- Alerts ---


def alerts(db: Session, now: datetime) -> list[str]:
    claude_jobs.send_alerts(
        db,
        None,
        base_url="https://orbit.test",
        quiet=notifications.QuietHours(time(0, 0), time(0, 0), capture.DEFAULT_TZ),
        now=now,
    )
    return [n.title for n in db.query(Notification).order_by(Notification.id)]


def test_an_expired_login_is_pushed_once_a_day(db: Session) -> None:
    claude_jobs.heartbeat(db, auth_ok=False, now=T0)
    assert alerts(db, T0) == ["🔑 Claude login expired"]
    assert alerts(db, T0 + timedelta(hours=1)) == ["🔑 Claude login expired"]
    assert len(alerts(db, T0 + timedelta(days=1))) == 2


def test_a_missing_worker_is_pushed_only_with_jobs_waiting(db: Session) -> None:
    assert alerts(db, T0) == []  # nothing waits, nothing to say
    claude_jobs.enqueue(db, spec(), limits=QueueLimits(wait_interactive=3600), now=T0)
    assert alerts(db, T0 + timedelta(minutes=5)) == []  # not yet 10 minutes
    assert alerts(db, T0 + timedelta(minutes=11)) == ["⚠️ Claude worker is down"]
    assert alerts(db, T0 + timedelta(minutes=30)) == ["⚠️ Claude worker is down"]  # once a day
    claude_jobs.heartbeat(db, now=T0 + timedelta(days=1))
    assert len(alerts(db, T0 + timedelta(days=1, minutes=1))) == 1  # the worker is back
