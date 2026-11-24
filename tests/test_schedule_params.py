"""The times and counts that used to be constants are parameters now (SPEC §2.21)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.contacts import ContactBackup
from orbit.domain.enums import LessonKind, SchoolEventKind
from orbit.domain.inbox import Notification
from orbit.domain.school import Course, Lesson
from orbit.services import contact_sync, notifications, people, reminders, review, school
from orbit.services.notifications import QuietHours, flush_due, notify
from tests.test_notifications import FakeSender, push
from tests.test_reminders import PRAGUE, QUIET, SUNDAY_SIX, TODAY, birthday, local, run

TZ = ZoneInfo("Europe/Prague")
NO_QUIET = QuietHours(time(0, 0), time(0, 0), TZ)


# --- Reminders and the digest ---


def test_reminders_wait_for_a_later_reminder_time(db: Session) -> None:
    birthday(db, "Jano", TODAY)
    nine = local(TODAY, 9)
    kwargs = {"reminder_time": time(11, 0)}

    def go(now: datetime) -> int:
        return reminders.send_reminders(
            db,
            FakeSender(),
            base_url="http://x",
            tz=PRAGUE,
            quiet=QUIET,
            item_reminder_days=21,
            now=now,
            **kwargs,
        )

    assert go(nine) == 0
    assert go(local(TODAY, 11)) == 1


def test_digest_on_another_weekday_and_time(db: Session) -> None:
    friday = date(2026, 9, 25)

    def go(now: datetime) -> int:
        return reminders.send_reminders(
            db,
            FakeSender(),
            base_url="http://x",
            tz=PRAGUE,
            quiet=QUIET,
            item_reminder_days=21,
            now=now,
            digest_weekday=4,
            digest_time=time(17, 0),
        )

    assert go(local(friday, 16, 59)) == 0
    assert go(local(friday, 17)) == 1  # the digest, on Friday
    assert go(SUNDAY_SIX) == 0  # and no longer on Sunday


def test_close_contact_heads_up_days_are_a_parameter(db: Session) -> None:
    birthday(db, "Jano", TODAY + timedelta(days=9), "close")

    def go(days: int) -> int:
        return reminders.send_reminders(
            db,
            FakeSender(),
            base_url="http://x",
            tz=PRAGUE,
            quiet=QUIET,
            item_reminder_days=21,
            now=local(TODAY, 9),
            close_days_ahead=days,
        )

    assert go(5) == 0
    assert go(9) == 1
    assert run(db, local(TODAY, 9)) == 0  # sent once


# --- School ---


def school_pushes(db: Session, now: datetime, **kw: object) -> int:
    return school.send_school_pushes(
        db,
        None,
        base_url="http://x",
        tz=TZ,
        quiet=NO_QUIET,
        evening=time(20, 0),
        now=now,
        **kw,  # type: ignore[arg-type]
    )


def test_exam_reminder_days_and_time_are_parameters(db: Session) -> None:
    today = date(2026, 10, 7)
    school.create_event(
        db,
        school.ManualEventInput(
            "Zkouška", SchoolEventKind.EXAM, today + timedelta(days=5), time(8, 0)
        ),
        TZ,
    )
    at = lambda hh: datetime.combine(today, time(hh), TZ).astimezone(UTC)  # noqa: E731
    # Default days (7, 1): nothing five days ahead.
    assert school_pushes(db, at(18)) == 0
    # From 19:00 and five days ahead: nothing at 18:30, one at 19:00.
    custom = {"reminder_time": time(19, 0), "exam_days": [5]}
    assert school_pushes(db, at(18) + timedelta(minutes=30), **custom) == 0
    assert school_pushes(db, at(19), **custom) == 1


def test_schedule_change_gap_is_a_parameter(db: Session) -> None:
    course = Course(code="NINF062", name="Algoritmizace")
    db.add(course)
    db.flush()
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def lesson(uid: str, changed: datetime) -> None:
        start = now + timedelta(days=2 if uid == "a" else 3)  # another text: another dedupe key
        db.add(
            Lesson(
                uid=uid,
                ticket_code=uid,
                kind=LessonKind.SEMINAR,
                course_id=course.id,
                title="Algoritmizace",
                start_utc=start,
                end_utc=start + timedelta(minutes=90),
                changed_at=changed,
            )
        )
        db.flush()

    lesson("a", now - timedelta(minutes=1))
    assert school_pushes(db, now) == 1
    later = now + timedelta(minutes=30)
    lesson("b", now + timedelta(minutes=20))
    assert school_pushes(db, later) == 0  # one push per hour
    assert school_pushes(db, later, change_gap=timedelta(minutes=10)) == 1


# --- Weekly review ---


def test_review_timing_moves_the_start() -> None:
    friday = review.ReviewTiming(weekday=4, at=time(17, 0), catch_up_weekday=1)
    fri_1659 = datetime(2026, 10, 9, 16, 59, tzinfo=PRAGUE)
    assert review.due_plan_start(fri_1659, PRAGUE, friday) is None
    assert review.due_plan_start(fri_1659.replace(hour=17, minute=0), PRAGUE, friday) == date(
        2026, 10, 12
    )
    # Catch-up until Tuesday, not Wednesday.
    assert review.due_plan_start(datetime(2026, 10, 13, 23, 0, tzinfo=PRAGUE), PRAGUE, friday) == (
        date(2026, 10, 12)
    )
    assert (
        review.due_plan_start(datetime(2026, 10, 14, 1, 0, tzinfo=PRAGUE), PRAGUE, friday) is None
    )


def test_review_timing_from_settings(make_settings) -> None:  # type: ignore[no-untyped-def]
    timing = review.timing_from(
        make_settings(
            weekly_review_weekday=5, weekly_review_time="08:30", weekly_review_catch_up_weekday=3
        )
    )
    assert timing == review.ReviewTiming(5, time(8, 30), 3)
    assert review.timing_from(make_settings()) == review.ReviewTiming()


# --- Notifications ---


def test_delivery_policy_comes_from_settings(make_settings) -> None:  # type: ignore[no-untyped-def]
    quiet = notifications.quiet_hours(
        make_settings(
            notification_retry_minutes=2,
            notification_max_attempts=3,
            notification_max_age_hours=6,
        )
    )
    assert (quiet.retry_delay, quiet.max_attempts, quiet.max_age) == (
        timedelta(minutes=2),
        3,
        timedelta(hours=6),
    )


def test_retry_delay_and_attempt_limit_follow_the_policy(db: Session) -> None:
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    policy = QuietHours(
        time(0, 0), time(0, 0), TZ, retry_delay=timedelta(minutes=1), max_attempts=2
    )
    sender = FakeSender(fail=True)
    row = notify(db, sender, push(), dedupe_key="k", kind="birthday", quiet=policy, now=now)
    assert row is not None and row.not_before == now + timedelta(minutes=1)
    flush_due(db, sender, quiet=policy, now=now + timedelta(minutes=2))
    assert row.attempts == 2
    flush_due(db, sender, quiet=policy, now=now + timedelta(minutes=5))
    assert row.attempts == 2  # the limit of this policy, not the default 5


def test_old_pushes_are_dropped_by_the_policy_age(db: Session) -> None:
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    policy = QuietHours(time(0, 0), time(0, 0), TZ, max_age=timedelta(hours=2))
    sender = FakeSender(fail=True)
    notify(db, sender, push(), dedupe_key="k", kind="birthday", quiet=policy, now=now)
    sender.fail = False
    assert flush_due(db, sender, quiet=policy, now=now + timedelta(hours=3)) == 0


def test_flush_can_ignore_the_wait_for_a_retry_but_not_quiet_hours(db: Session) -> None:
    now = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)  # 12:00 in Prague
    sender = FakeSender(fail=True)
    notify(db, sender, push(), dedupe_key="k", kind="birthday", quiet=NO_QUIET, now=now)
    sender.fail = False
    soon = now + timedelta(minutes=1)
    assert flush_due(db, sender, quiet=NO_QUIET, now=soon) == 0  # waits 5 minutes
    quiet_now = QuietHours(time(11, 0), time(23, 0), TZ)
    assert flush_due(db, sender, quiet=quiet_now, now=soon, ignore_not_before=True) == 0
    assert flush_due(db, sender, quiet=NO_QUIET, now=soon, ignore_not_before=True) == 1
    assert db.scalars(select(Notification)).one().delivered


# --- Saved Apple cards ---


def test_contact_backup_retention_is_a_parameter(db: Session) -> None:
    person = people.create_person(db, people.PersonCreate(display_name="Paťo"))
    old = datetime(2026, 1, 1, tzinfo=UTC)
    db.add(ContactBackup(person_id=person.id, href="/x", vcard_text="x", created_at=old))
    db.flush()
    assert contact_sync.prune_backups(db, now=old + timedelta(days=10), keep_days=30) == 0
    assert contact_sync.prune_backups(db, now=old + timedelta(days=10), keep_days=5) == 1
