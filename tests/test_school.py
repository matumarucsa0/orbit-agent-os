"""School service: manual items, the week, and the L1 pushes with their dedupe keys and quiet
hours (SPEC §2.20)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import LessonKind, SchoolEventKind, SchoolEventSource
from orbit.domain.inbox import Notification
from orbit.domain.school import Course, Lesson, SchoolEvent
from orbit.services import notifications, reminders, school
from orbit.services.errors import InvalidInputError
from orbit.services.school_sync import SchoolSources, SyncConfig, sync_school
from tests.school_helpers import ROZVRHNG, FakeSis, ok, replace_in

TZ = ZoneInfo("Europe/Prague")
QUIET = notifications.QuietHours(time(22, 0), time(8, 0), TZ)
BASE = "http://testserver"


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), TZ).astimezone(UTC)


class Sender:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def send(self, message: object) -> None:
        self.sent.append(message)


def add_lesson(
    db: Session,
    course: Course,
    start: datetime,
    *,
    uid: str,
    room: str = "N8",
    cancelled: bool = False,
) -> Lesson:
    row = Lesson(
        uid=uid,
        ticket_code=f"26a{course.code}x01",
        kind=LessonKind.SEMINAR,
        course_id=course.id,
        title=course.name,
        room=room,
        start_utc=start,
        end_utc=start + timedelta(minutes=90),
        cancelled=cancelled,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def course(db: Session) -> Course:
    row = Course(code="NINF062", name="Algoritmizace", credits=4)
    db.add(row)
    db.flush()
    return row


def pushes(db: Session, now: datetime, **kw: object) -> int:
    return school.send_school_pushes(
        db,
        kw.pop("sender", None),
        base_url=BASE,
        tz=TZ,
        quiet=QUIET,
        evening=time(20, 0),
        now=now,
        **kw,  # type: ignore[arg-type]
    )


def keys(db: Session) -> list[str]:
    return sorted(db.scalars(select(Notification.dedupe_key)))


# --- Helpers ---


def test_school_year_and_texts() -> None:
    assert school.school_year(date(2026, 9, 1)) == 2026
    assert school.school_year(date(2027, 8, 31)) == 2026
    assert school.when_text(at(date(2026, 10, 1), 9, 50), TZ) == "Thu 1 Oct 9:50"
    assert school.base_name("Programování 1 (Čt, 9:50, N8)") == "Programování 1"


# --- Manual items ---


def test_manual_event_crud(db: Session, course: Course) -> None:
    data = school.ManualEventInput(
        title="  Domácí úkol 2 ",
        kind=SchoolEventKind.DEADLINE,
        day=date(2026, 10, 20),
        at=time(23, 59),
        course_id=course.id,
        note="ReCodEx",
    )
    event = school.create_event(db, data, TZ)
    assert (event.source, event.uid, event.title) == (
        SchoolEventSource.MANUAL,
        None,
        "Domácí úkol 2",
    )
    assert event.start_utc == datetime(2026, 10, 20, 21, 59, tzinfo=UTC)
    assert not event.all_day
    whole = school.ManualEventInput(title="Test", kind=SchoolEventKind.EXAM, day=date(2026, 11, 3))
    school.update_event(db, event.id, whole, TZ)
    assert event.all_day and event.course_id is None and event.kind is SchoolEventKind.EXAM
    assert event.start_utc == datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
    school.delete_event(db, event.id)
    assert db.get(SchoolEvent, event.id) is None


def test_manual_event_validation_and_feed_rows(db: Session) -> None:
    with pytest.raises(InvalidInputError):
        school.create_event(
            db, school.ManualEventInput("  ", SchoolEventKind.DEADLINE, date(2026, 1, 1)), TZ
        )
    with pytest.raises(InvalidInputError):
        school.create_event(
            db, school.ManualEventInput("x", SchoolEventKind.TERM, date(2026, 1, 1)), TZ
        )
    with pytest.raises(InvalidInputError):
        school.create_event(
            db,
            school.ManualEventInput("x", SchoolEventKind.EXAM, date(2026, 1, 1), course_id=999),
            TZ,
        )
    feed = SchoolEvent(
        source=SchoolEventSource.RECODEX,
        uid="recodex/u/a",
        kind=SchoolEventKind.DEADLINE,
        title="hw",
        start_utc=at(date(2026, 10, 20), 12),
    )
    db.add(feed)
    db.flush()
    with pytest.raises(InvalidInputError, match="by hand"):
        school.delete_event(db, feed.id)


# --- Week ---


def test_week_groups_by_local_day(db: Session, course: Course) -> None:
    monday = date(2026, 10, 5)
    add_lesson(db, course, at(monday, 9), uid="a")
    add_lesson(db, course, at(monday + timedelta(days=2), 0, 30), uid="b", cancelled=True)
    add_lesson(db, course, at(monday + timedelta(days=7), 9), uid="next-week")
    school.create_event(
        db, school.ManualEventInput("Test", SchoolEventKind.EXAM, monday + timedelta(days=4)), TZ
    )
    days = school.week(db, monday, TZ)
    assert days[0].day == monday and len(days) == 7
    assert [lesson.uid for lesson in days[0].lessons] == ["a"]
    assert [lesson.uid for lesson in days[2].lessons] == ["b"]  # 00:30 local, cancelled shown
    assert [e.title for e in days[4].events] == ["Test"]
    assert school.week_start(date(2026, 10, 11)) == monday


# --- Pushes ---


def test_tomorrow_push_once_from_the_evening(db: Session, course: Course) -> None:
    day = date(2026, 10, 7)
    add_lesson(db, course, at(day + timedelta(days=1), 9), uid="a")
    add_lesson(db, course, at(day + timedelta(days=1), 12, 20), uid="b", room="N1")
    add_lesson(db, course, at(day + timedelta(days=1), 15), uid="c", cancelled=True)
    sender = Sender()
    assert pushes(db, at(day, 19, 59), sender=sender) == 0
    assert pushes(db, at(day, 20, 0), sender=sender) == 1
    assert pushes(db, at(day, 21, 0), sender=sender) == 0
    assert keys(db) == ["school_tomorrow:2026-10-08"]
    (message,) = sender.sent
    assert message.title == "🎓 Tomorrow: 2 lessons, first 9:00 N8 (Algoritmizace)"  # type: ignore[attr-defined]
    assert message.body.splitlines() == [  # type: ignore[attr-defined]
        "9:00 N8 Algoritmizace (seminar)",
        "12:20 N1 Algoritmizace (seminar)",
    ]


def test_no_tomorrow_push_without_lessons(db: Session) -> None:
    assert pushes(db, at(date(2026, 10, 7), 20, 30)) == 0


def test_exam_and_deadline_reminders(db: Session, course: Course) -> None:
    today = date(2026, 10, 7)
    exam = school.create_event(
        db,
        school.ManualEventInput(
            "Zkouška",
            SchoolEventKind.EXAM,
            today + timedelta(days=7),
            time(8, 0),
            course_id=course.id,
        ),
        TZ,
    )
    due = SchoolEvent(
        source=SchoolEventSource.RECODEX,
        uid="recodex/u/a",
        kind=SchoolEventKind.DEADLINE,
        title="Hello World",
        start_utc=at(today + timedelta(days=3), 23, 59),
        points=Decimal(0),
        max_points=Decimal(1),
        course_id=course.id,
    )
    db.add(due)
    db.flush()
    sender = Sender()
    assert pushes(db, at(today, 17, 0), sender=sender) == 0  # reminders go at 18:00
    assert pushes(db, at(today, 18, 0), sender=sender) == 2
    assert keys(db) == [
        f"school_deadline:{due.id}:{due.start_utc:%Y%m%dT%H%M}:3d",
        f"school_exam:{exam.id}:{exam.start_utc:%Y%m%dT%H%M}:7d",
    ]
    texts = {m.title: m.body for m in sender.sent}  # type: ignore[attr-defined]
    assert texts["📝 Exam in 7 days: Algoritmizace: Zkouška"] == "Wed 14 Oct 8:00"
    assert texts["⏰ Deadline in 3 days: Algoritmizace: Hello World"].endswith("0/1 points so far")
    # One day before: the exam isn't due yet (6 days), the deadline is unless it has full points.
    due.points = Decimal(1)
    assert pushes(db, at(today + timedelta(days=2), 18, 30), sender=sender) == 0
    assert pushes(db, at(today + timedelta(days=6), 18, 30), sender=sender) == 1
    assert f"school_exam:{exam.id}:{exam.start_utc:%Y%m%dT%H%M}:1d" in keys(db)
    assert not any(k.startswith("school_deadline") and k.endswith(":1d") for k in keys(db))


def test_moved_exam_is_reminded_again(db: Session) -> None:
    today = date(2026, 10, 7)
    exam = school.create_event(
        db, school.ManualEventInput("Zkouška", SchoolEventKind.EXAM, today + timedelta(days=1)), TZ
    )
    pushes(db, at(today, 18, 0))
    school.update_event(
        db,
        exam.id,
        school.ManualEventInput("Zkouška", SchoolEventKind.EXAM, today + timedelta(days=7)),
        TZ,
    )
    assert pushes(db, at(today, 18, 5)) == 1
    assert len(keys(db)) == 2


def test_reminder_in_quiet_hours_waits(db: Session) -> None:
    today = date(2026, 10, 7)
    school.create_event(
        db, school.ManualEventInput("Zkouška", SchoolEventKind.EXAM, today + timedelta(days=1)), TZ
    )
    sender = Sender()
    assert pushes(db, at(today, 23, 0), sender=sender) == 1
    record = db.scalars(select(Notification)).one()
    assert not record.delivered and record.not_before == at(today + timedelta(days=1), 8, 0)
    assert sender.sent == []


def test_schedule_change_push_batched_hourly(db: Session) -> None:
    config = SyncConfig(base_url=BASE, tz=TZ, quiet=QUIET)
    start = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    sources = SchoolSources(sis=FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    sync_school(db, sources, config, now=start)
    assert pushes(db, start) == 0  # the first import is no change
    moved = replace_in(
        ROZVRHNG,
        "DTSTART:20261001T075000Z\r\nALL_DAY:0\r\nDTEND:20261001T092000Z",
        "DTSTART:20261002T075000Z\r\nALL_DAY:0\r\nDTEND:20261002T092000Z",
    )
    sync_school(db, SchoolSources(sis=FakeSis({"rozvrhng": ok(moved)})), config, now=start)
    sender = Sender()
    assert pushes(db, start + timedelta(minutes=1), sender=sender) == 1
    (message,) = sender.sent
    assert message.title == "🎓 Schedule change"  # type: ignore[attr-defined]
    assert message.body == (  # type: ignore[attr-defined]
        "Programování 1 (seminar) Fri 2 Oct 9:50: moved from Thu 1 Oct 9:50"
    )
    # Another change 10 minutes later waits for the hour, then goes out alone.
    cancelled = moved.replace(b"RNG26aNINF120p1_2461313", b"RNG26aNINF120p1_9999999")
    sync_school(
        db,
        SchoolSources(sis=FakeSis({"rozvrhng": ok(cancelled)})),
        config,
        now=start + timedelta(minutes=10),
    )
    assert pushes(db, start + timedelta(minutes=30), sender=sender) == 0
    assert pushes(db, start + timedelta(minutes=62), sender=sender) == 1
    lines = sender.sent[-1].body.splitlines()  # type: ignore[attr-defined]
    assert any("cancelled" in line for line in lines) and any(": new" in line for line in lines)
    assert not any("moved" in line for line in lines)


def test_recodex_token_warning(db: Session) -> None:
    expires = datetime(2027, 9, 27, 12, 0, tzinfo=UTC)
    assert pushes(db, expires - timedelta(days=15), recodex_expires=expires) == 0
    assert pushes(db, expires - timedelta(days=14), recodex_expires=expires) == 1
    assert pushes(db, expires - timedelta(days=10), recodex_expires=expires) == 0
    assert keys(db) == ["school:recodex_exp:20270927"]


# --- Digest and Home ---


def test_digest_school_section(db: Session, course: Course) -> None:
    sunday = date(2026, 10, 11)
    for i in range(3):
        add_lesson(db, course, at(sunday + timedelta(days=1 + i), 9), uid=f"l{i}")
    add_lesson(db, course, at(sunday + timedelta(days=2), 11), uid="x", cancelled=True)
    school.create_event(
        db,
        school.ManualEventInput(
            "Písemka", SchoolEventKind.EXAM, sunday + timedelta(days=3), time(9)
        ),
        TZ,
    )
    digest = reminders.build_digest(db, at(sunday, 18, 0), TZ)
    assert digest.school_week is not None
    sections = reminders.digest_sections(digest, TZ)
    (text,) = [s for s in sections if s.startswith("🎓")]
    assert text.splitlines() == [
        "🎓 School next week: 3 lessons, 1 exam",
        "• Wed 14 Oct 9:00 exam: Písemka",
    ]


def test_digest_without_school_has_no_section(db: Session) -> None:
    digest = reminders.build_digest(db, at(date(2026, 10, 11), 18, 0), TZ)
    assert not any(s.startswith("🎓") for s in reminders.digest_sections(digest, TZ))


def test_home_strip(db: Session, course: Course) -> None:
    now = at(date(2026, 10, 7), 10)
    assert school.home_strip(db, now) is None
    add_lesson(db, course, now + timedelta(hours=2), uid="soon")
    school.create_event(
        db, school.ManualEventInput("Úkol", SchoolEventKind.DEADLINE, date(2026, 10, 9)), TZ
    )
    strip = school.home_strip(db, now)
    assert strip is not None and strip.next_lesson is not None
    assert strip.next_lesson.uid == "soon" and strip.next_item is not None
    assert strip.next_item.title == "Úkol"
