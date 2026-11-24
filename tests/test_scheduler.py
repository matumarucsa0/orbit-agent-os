from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select

from orbit.db import make_engine, make_sessionmaker
from orbit.domain import Base
from orbit.domain.enums import DebtDirection, DebtType
from orbit.domain.inbox import Notification
from orbit.integrations.ntfy import PushMessage
from orbit.jobs.scheduler import flush_notifications, reminder_pushes, start_scheduler
from orbit.services import ledger, people
from orbit.services.notifications import quiet_hours
from orbit.settings import Settings
from tests.test_notifications import FakeSender


def test_quiet_hours_come_from_settings(make_settings) -> None:  # type: ignore[no-untyped-def]
    quiet = quiet_hours(make_settings(quiet_hours="23:00-07:30"))
    assert (quiet.start, quiet.end) == (time(23, 0), time(7, 30))


def test_flush_job_sends_due_pushes_and_commits(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings: Settings = make_settings(quiet_hours="00:00-00:00")  # no quiet hours
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions() as session:
        session.add(
            Notification(
                dedupe_key="k",
                kind="birthday",
                title="t",
                body="b",
                payload_json=PushMessage("t", "b").to_dict(),
                created_at=now - timedelta(minutes=5),
                not_before=now - timedelta(minutes=1),
            )
        )
        session.commit()

    sender = FakeSender()
    assert flush_notifications(sessions, sender, settings) == 1
    with sessions() as session:
        assert session.scalars(select(Notification)).one().delivered
    assert flush_notifications(sessions, sender, settings) == 0
    engine.dispose()


def test_reminder_job_records_due_reminders_and_commits(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings: Settings = make_settings(quiet_hours="00:00-00:00")
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions() as session:
        pato = people.create_person(session, people.PersonCreate(display_name="Paťo"))
        charger = ledger.create_debt(
            session,
            ledger.DebtCreate(
                person_id=pato.id,
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.ITEM,
                description="charger",
            ),
            "EUR",
        )
        charger.created_at = now - timedelta(days=60)
        session.commit()

    sender = FakeSender()
    # Reminders go out from 09:00 local time on.
    due = int(now.astimezone(settings.timezone).time() >= time(9, 0))
    assert reminder_pushes(sessions, sender, settings) == due
    with sessions() as session:
        assert len(session.scalars(select(Notification)).all()) == due
    assert reminder_pushes(sessions, sender, settings) == 0
    assert len(sender.sent) == due
    engine.dispose()


def test_scheduler_runs_the_reminder_job(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings: Settings = make_settings()
    engine = make_engine("sqlite://")
    scheduler = start_scheduler(make_sessionmaker(engine), None, settings)
    try:
        assert {job.id for job in scheduler.get_jobs()} >= {
            "flush_notifications",
            "meeting_pushes",
            "reminder_pushes",
        }
    finally:
        scheduler.shutdown(wait=False)
        engine.dispose()
