"""The job registry: running a job by hand, its recorded status, live intervals (SPEC §2.21)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from orbit.db import make_engine, make_sessionmaker
from orbit.domain import Base
from orbit.domain.app_settings import JobRun
from orbit.domain.review import WeeklyReview
from orbit.jobs import scheduler
from orbit.jobs.scheduler import JOBS, JobContext, JobSpec, execute, job_spec, job_statuses
from orbit.services.errors import NotFoundError
from orbit.settings import Settings
from tests.test_notifications import FakeSender
from tests.test_review import FakeReviewer


@pytest.fixture
def ctx(make_settings) -> Any:  # type: ignore[no-untyped-def]
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = make_settings(quiet_hours="00:00-00:00")
    context = JobContext(make_sessionmaker(engine), lambda: settings, FakeSender())
    yield context
    engine.dispose()


def last_run(ctx: JobContext, job_id: str) -> JobRun | None:
    with ctx.sessions() as session:
        return session.get(JobRun, job_id)


def test_the_registry_has_one_spec_per_job_and_the_ones_with_a_button() -> None:
    ids = [spec.id for spec in JOBS]
    assert len(ids) == len(set(ids))
    assert {s.id for s in JOBS if s.run_now} == {
        "flush_notifications",
        "sync_calendar",
        "meeting_pushes",
        "reminder_pushes",
        "prune_contact_backups",
        "weekly_review",
        "prune_claude_jobs",
        "sis_sync",
        "school_pushes",
        "school_resources",
        "liftoff_sync",
    }
    with pytest.raises(NotFoundError):
        job_spec("nope")


def test_run_now_runs_the_job_and_records_it(ctx: JobContext) -> None:
    result = execute(ctx, job_spec("reminder_pushes"), manual=True)
    assert (result.ran, result.ok, result.message) == (True, True, "0 recorded")
    row = last_run(ctx, "reminder_pushes")
    assert row is not None and row.ok and row.manual and row.message == "0 recorded"
    execute(ctx, job_spec("reminder_pushes"))  # a scheduled run replaces it
    again = last_run(ctx, "reminder_pushes")
    assert again is not None and not again.manual


def test_a_failing_job_is_reported_not_raised(ctx: JobContext) -> None:
    def boom(_ctx: JobContext, _s: Settings, _manual: bool) -> str:
        raise RuntimeError("the feed is down")

    spec = JobSpec("boom", "Boom", "", boom, lambda _s: {"seconds": 60})
    result = execute(ctx, spec)
    assert (result.ran, result.ok) == (True, False)
    assert result.message == "RuntimeError: the feed is down"
    row = last_run(ctx, "boom")
    assert row is not None and not row.ok and "feed is down" in row.message


def test_a_job_that_cannot_run_says_why_and_leaves_no_trace(ctx: JobContext) -> None:
    result = execute(ctx, job_spec("sync_calendar"), manual=True)
    assert (result.ran, result.ok) == (False, False)
    assert "CALDAV" in result.message
    assert last_run(ctx, "sync_calendar") is None
    assert "Claude worker" in execute(ctx, job_spec("late_claude_answers")).message


def test_a_switched_off_job_does_not_run(make_settings) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = make_settings(school_pushes_enabled=False)
    context = JobContext(make_sessionmaker(engine), lambda: settings)
    result = execute(context, job_spec("school_pushes"), manual=True)
    assert (result.ran, result.message) == (False, "Turned off in the settings.")
    engine.dispose()


def test_a_job_never_runs_twice_at_once(ctx: JobContext) -> None:
    held = ctx.lock("flush_notifications")
    held.acquire()
    try:
        result = execute(ctx, job_spec("flush_notifications"), manual=True)
    finally:
        held.release()
    assert (result.ran, result.message) == (False, "Already running.")
    assert execute(ctx, job_spec("flush_notifications")).ran


def test_a_status_write_failure_does_not_fail_the_job(make_settings) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine("sqlite://")  # no tables: recording fails, the job itself works
    context = JobContext(make_sessionmaker(engine), lambda: make_settings())
    spec = JobSpec("quiet", "Quiet", "", lambda *_: "fine", lambda _s: {"seconds": 60})
    assert execute(context, spec).ok
    engine.dispose()


def test_flush_run_now_sends_a_push_waiting_for_a_retry(ctx: JobContext) -> None:
    from orbit.integrations.ntfy import PushMessage
    from orbit.services import notifications

    sender = FakeSender(fail=True)
    ctx.sender = sender
    with ctx.sessions() as session:
        notifications.notify(
            session,
            sender,
            PushMessage("t", "b"),
            dedupe_key="k",
            kind="birthday",
            quiet=notifications.quiet_hours(ctx.settings()),
        )
        session.commit()
    sender.fail = False
    assert execute(ctx, job_spec("flush_notifications")).message == "0 sent"  # waits 5 minutes
    assert execute(ctx, job_spec("flush_notifications"), manual=True).message == "1 sent"


def test_weekly_review_run_now_makes_next_weeks_review_on_any_day(
    ctx: JobContext, make_settings, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    reviewer = FakeReviewer()
    ctx.reviewer = reviewer
    thursday = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(scheduler, "utcnow", lambda: thursday)  # execute() reads the clock
    settings = ctx.settings()
    assert scheduler.weekly_review(ctx.sessions, reviewer, ctx.sender, settings, thursday) is None
    made = scheduler.weekly_review(ctx.sessions, reviewer, ctx.sender, settings, thursday, True)
    assert made is not None and len(reviewer.requests) == 1
    # Run now again: that week is ready, nothing more is made.
    assert (
        scheduler.weekly_review(ctx.sessions, reviewer, ctx.sender, settings, thursday, True)
        is None
    )
    with ctx.sessions() as session:
        assert session.scalar(select(func.count(WeeklyReview.id))) == 1
    assert execute(ctx, job_spec("weekly_review"), manual=True).message == "nothing due"


def test_weekly_review_without_a_reviewer_says_who_makes_it(ctx: JobContext) -> None:
    assert "worker" in execute(ctx, job_spec("weekly_review"), manual=True).message


# --- The scheduler ---


def started(make_settings, **kw: Any):  # type: ignore[no-untyped-def]
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = make_settings(**kw)
    box = {"settings": settings}
    context = JobContext(make_sessionmaker(engine), lambda: box["settings"], FakeSender())
    sched = scheduler.start_scheduler(
        context.sessions, None, settings, reviewer=kw.pop("reviewer", None), context=context
    )
    return engine, context, sched, box


def test_jobs_follow_what_is_available_and_switches_are_checked_at_run_time(
    make_settings,
) -> None:  # type: ignore[no-untyped-def]
    engine, ctx, sched, _ = started(make_settings, weekly_review_enabled=False)
    try:
        assert {j.id for j in sched.get_jobs()} == {
            "flush_notifications",
            "meeting_pushes",
            "reminder_pushes",
            "prune_contact_backups",
            "school_pushes",
        }
        assert ctx.scheduler is sched
    finally:
        sched.shutdown(wait=False)
        engine.dispose()


def test_a_saved_interval_moves_the_job(make_settings) -> None:  # type: ignore[no-untyped-def]
    engine, ctx, sched, box = started(make_settings)
    try:
        job = sched.get_job("prune_contact_backups")
        assert job.trigger.interval == timedelta(hours=24)
        box["settings"] = box["settings"].model_copy(update={"backup_prune_hours": 6})
        scheduler.sync_intervals(ctx)
        assert sched.get_job("prune_contact_backups").trigger.interval == timedelta(hours=6)
        before = sched.get_job("prune_contact_backups").next_run_time
        scheduler.sync_intervals(ctx)  # unchanged: not moved again
        assert sched.get_job("prune_contact_backups").next_run_time == before
    finally:
        sched.shutdown(wait=False)
        engine.dispose()


def test_a_tick_runs_the_job_and_corrects_the_interval(make_settings) -> None:  # type: ignore[no-untyped-def]
    engine, ctx, sched, box = started(make_settings)
    try:
        box["settings"] = box["settings"].model_copy(update={"backup_prune_hours": 12})
        scheduler._tick(ctx, "reminder_pushes")
        assert last_run(ctx, "reminder_pushes") is not None
        assert sched.get_job("prune_contact_backups").trigger.interval == timedelta(hours=12)
    finally:
        sched.shutdown(wait=False)
        engine.dispose()


def test_status_lists_every_job_with_its_last_run(ctx: JobContext) -> None:
    execute(ctx, job_spec("reminder_pushes"), manual=True)
    by_id = {s.spec.id: s for s in job_statuses(ctx)}
    assert set(by_id) == {j.id for j in JOBS}
    assert by_id["reminder_pushes"].last is not None
    assert by_id["reminder_pushes"].every == "every 60 s"
    assert by_id["sis_sync"].every == "every 60 min"
    assert by_id["prune_contact_backups"].every == "every 24 h"
    assert by_id["sync_calendar"].unavailable is not None
    assert by_id["sync_calendar"].next_run is None  # no scheduler running


def test_the_app_shares_one_context_between_scheduler_and_run_now(app: FastAPI) -> None:
    assert isinstance(app.state.jobs, JobContext)
    assert app.state.jobs.settings() == app.state.settings  # no overrides: env values
    assert isinstance(app.state.jobs.lock("x"), type(threading.Lock()))
