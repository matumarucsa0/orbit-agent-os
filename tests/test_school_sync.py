"""School ingest: idempotent upserts, moved and vanished lessons, the cancel guard, feed
problems, course enrichment, harmonogram, ReCodEx and Owl (SPEC §2.20)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    FetchSource,
    FetchStatus,
    SchoolEventKind,
    SchoolEventSource,
    ScoreSource,
)
from orbit.domain.inbox import Notification
from orbit.domain.school import Course, CourseScore, Lesson, OwlCourse, SchoolEvent, SisFetch
from orbit.integrations.recodex import RecodexDenied
from orbit.integrations.sis import FeedFetch, HarmonogramRow, SisError
from orbit.services import notifications, school
from orbit.services.school_sync import SchoolSources, SyncConfig, SyncReport, sync_school
from tests.school_helpers import (
    EMPTY,
    EXAM_FEED,
    ROZVRHNG,
    FakeOwl,
    FakeRecodex,
    FakeSis,
    drop_event,
    ninf030_page,
    ok,
    owl_fixture,
    recodex_assignments,
    recodex_groups,
    replace_in,
)

TZ = ZoneInfo("Europe/Prague")
QUIET = notifications.QuietHours(time(22, 0), time(8, 0), TZ)
CONFIG = SyncConfig(base_url="http://testserver", tz=TZ, quiet=QUIET)
BEFORE = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)  # every fixture lesson is in the future


class Sender:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def send(self, message: object) -> None:
        self.sent.append(message)


def run(
    db: Session,
    sis: FakeSis | None = None,
    *,
    now: datetime = BEFORE,
    sender: Sender | None = None,
    recodex: FakeRecodex | None = None,
    owl: FakeOwl | None = None,
) -> SyncReport:
    sources = SchoolSources(sis=sis, recodex=recodex, owl=owl)
    return sync_school(db, sources, CONFIG, sender=sender, now=now)


def lesson(db: Session, uid: str) -> Lesson:
    found = db.scalar(select(Lesson).where(Lesson.uid == uid))
    assert found is not None
    return found


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


# --- Lessons ---


def test_first_sync_imports_lessons_and_courses(db: Session) -> None:
    report = run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    first = report.results[0]
    assert (first.source, first.status, first.seen, first.changed) == (
        FetchSource.ROZVRHNG,
        FetchStatus.OK,
        11,
        11,
    )
    assert count(db, Lesson) == 11
    assert sorted(db.scalars(select(Course.code))) == [
        "NINF030",
        "NINF062",
        "NINF120",
        "NINF141",
        "NMAT101",
        "NMAT202",
        "NSPO014",
    ]
    first = lesson(db, "26aNMAT202p1_2461315")
    assert first.course is not None and first.course.name == "Diskrétní matematika"
    assert first.raw_json == {"uid": "RNG26aNMAT202p1_2461315@10000@sis.example.edu"}
    assert first.changed_at is None  # the first import is not a change
    fetch = db.scalars(select(SisFetch).where(SisFetch.source == FetchSource.ROZVRHNG)).one()
    assert fetch.status is FetchStatus.OK and fetch.body_hash


def test_second_sync_changes_nothing(db: Session) -> None:
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    restamped = ROZVRHNG.replace(b"DTSTAMP:20261006T183045Z", b"DTSTAMP:20261006T193045Z")
    report = run(db, FakeSis({"rozvrhng": ok(restamped)}))
    assert report.results[0].status is FetchStatus.UNCHANGED
    # A body that differs (an extra property) is parsed again, and still changes nothing.
    different = replace_in(ROZVRHNG, "TRANSP:OPAQUE", "TRANSP:OPAQUE\r\nX-FOO:1")
    report = run(db, FakeSis({"rozvrhng": ok(different)}))
    assert (report.results[0].status, report.results[0].changed) == (FetchStatus.OK, 0)
    assert count(db, Lesson) == 11
    assert db.scalar(select(func.count()).where(Lesson.changed_at.is_not(None))) == 0


def test_moved_lesson_and_new_room(db: Session) -> None:
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    moved = replace_in(
        ROZVRHNG,
        "DTSTART:20261001T075000Z\r\nALL_DAY:0\r\nDTEND:20261001T092000Z",
        "DTSTART:20261002T075000Z\r\nALL_DAY:0\r\nDTEND:20261002T092000Z",
    )
    moved = replace_in(moved, "D.; N8 9:50", "D.; N9 9:50")
    later = BEFORE + timedelta(hours=1)
    report = run(db, FakeSis({"rozvrhng": ok(moved)}), now=later)
    assert report.results[0].changed == 1
    prog = lesson(db, "26aNINF030x03_2461315")
    assert prog.start_utc == datetime(2026, 10, 2, 7, 50, tzinfo=UTC)
    assert prog.room == "N9"
    assert prog.change_note == "moved from Thu 1 Oct 9:50, room N8 → N9"
    assert prog.changed_at == later
    assert db.scalar(select(func.count()).where(Lesson.changed_at.is_not(None))) == 1


def test_vanished_future_lesson_is_cancelled_and_comes_back(db: Session) -> None:
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    gone = drop_event(ROZVRHNG, "26aNINF120p1")
    run(db, FakeSis({"rozvrhng": ok(gone)}))
    row = lesson(db, "26aNINF120p1_2461313")
    assert row.cancelled and row.change_note == "cancelled"
    assert count(db, Lesson) == 11  # never deleted
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    db.refresh(row)
    assert not row.cancelled and row.change_note == "back on"


def test_past_lessons_are_never_touched(db: Session) -> None:
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    after = datetime(2026, 10, 5, tzinfo=UTC)  # every fixture lesson is over
    gone = drop_event(ROZVRHNG, "26aNINF120p1")
    moved = replace_in(gone, "LOCATION:N1 - Building A", "LOCATION:Z1 - Elsewhere")
    run(db, FakeSis({"rozvrhng": ok(moved)}), now=after)
    row = lesson(db, "26aNINF120p1_2461313")
    assert not row.cancelled
    assert lesson(db, "26aNMAT202p1_2461315").location == "N1 - Building A"


def test_empty_feed_never_cancels_and_pushes_once_a_day(db: Session) -> None:
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    sender = Sender()
    report = run(db, FakeSis({"rozvrhng": ok(EMPTY)}), sender=sender)
    assert report.results[0].status is FetchStatus.EMPTY
    assert db.scalar(select(func.count()).where(Lesson.cancelled.is_(True))) == 0
    assert report.pushes == 1
    run(db, FakeSis({"rozvrhng": ok(EMPTY)}), sender=sender, now=BEFORE + timedelta(hours=2))
    keys = list(db.scalars(select(Notification.dedupe_key)))
    assert keys == ["school:feed:2026-09-25"]
    assert len(sender.sent) == 1
    # Still empty the next day: still a problem, one more push.
    report = run(
        db, FakeSis({"rozvrhng": ok(EMPTY)}), sender=sender, now=BEFORE + timedelta(days=1)
    )
    assert report.results[0].status is FetchStatus.EMPTY and report.pushes == 1
    assert len(sender.sent) == 2


def test_denied_feed_records_and_pushes(db: Session) -> None:
    sender = Sender()
    denied = FeedFetch(FetchStatus.DENIED, error="HTTP 403")
    report = run(db, FakeSis({"rozvrhng": denied}), sender=sender)
    assert report.results[0].status is FetchStatus.DENIED
    fetch = db.scalars(select(SisFetch)).one()
    assert (fetch.status, fetch.error) == (FetchStatus.DENIED, "HTTP 403")
    run(db, FakeSis({"rozvrhng": denied}), sender=sender, now=BEFORE + timedelta(days=1))
    assert sorted(db.scalars(select(Notification.dedupe_key))) == [
        "school:feed:2026-09-25",
        "school:feed:2026-09-26",
    ]


def test_switching_lesson_feeds_does_not_duplicate(db: Session) -> None:
    from tests.school_helpers import ROZ_EXTERNI

    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    run(db, FakeSis({"roz_externi": ok(ROZ_EXTERNI)}))
    assert count(db, Lesson) == 11
    assert db.scalar(select(func.count()).where(Lesson.cancelled.is_(True))) == 0


# --- Events ---


def test_exam_feed(db: Session) -> None:
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG), "term_st2": ok(EXAM_FEED)}))
    exam = db.scalars(select(SchoolEvent)).one()
    assert (exam.source, exam.kind) == (SchoolEventSource.EXAM_FEED, SchoolEventKind.EXAM)
    assert exam.uid == "TST2_12345@10000@sis.example.edu"
    assert exam.course is not None and exam.course.code == "NMAT202"
    assert exam.start_utc == datetime(2027, 1, 12, 7, 0, tzinfo=UTC)
    # Empty feed: kept (never cancels); the problem push is only for lessons.
    report = run(db, FakeSis({"term_st2": ok(EMPTY)}))
    db.refresh(exam)
    assert not exam.cancelled and report.pushes == 0
    # Gone from a feed with other items: cancelled.
    other = EXAM_FEED.replace(b"TST2_12345", b"TST2_99999")
    run(db, FakeSis({"term_st2": ok(other)}))
    db.refresh(exam)
    assert exam.cancelled


def test_harmonogram_once_a_day(db: Session) -> None:
    rows = [
        HarmonogramRow(
            datetime(2026, 9, 28, 22, 0, tzinfo=UTC),
            datetime(2027, 1, 8, 22, 59, tzinfo=UTC),
            "Uni",
            "Akademický rok - výuka",
            "Výuka v zimním semestru",
        )
    ]
    sis = FakeSis({"rozvrhng": ok(ROZVRHNG)}, harmonogram=rows)
    run(db, sis)
    run(db, sis, now=BEFORE + timedelta(hours=1))
    assert sis.harmonogram_calls == [("10000", date(2026, 9, 1), date(2027, 8, 31))]
    term = db.scalars(select(SchoolEvent)).one()
    assert (term.kind, term.all_day, term.title) == (
        SchoolEventKind.TERM,
        True,
        "Akademický rok - výuka",
    )
    run(db, sis, now=BEFORE + timedelta(days=1))
    assert len(sis.harmonogram_calls) == 2
    assert count(db, SchoolEvent) == 1


# --- Course pages ---


def test_course_enrichment_ttl_and_tries(db: Session) -> None:
    sis = FakeSis({"rozvrhng": ok(ROZVRHNG)}, pages={"NINF030": ninf030_page()})
    run(db, sis)
    prog = db.scalars(select(Course).where(Course.code == "NINF030")).one()
    assert (prog.credits, prog.hours_lecture, prog.hours_seminar, prog.completion) == (5, 2, 2, "Z")
    assert prog.web_url == "https://www.example.edu/~vzorny/"
    assert ("NINF030", 2026) in sis.course_calls
    assert len(sis.course_calls) == 7  # every course once
    # The other six had no page: tried again on the next runs, at most 3 times a day.
    for hours in (1, 2, 3, 4):
        run(db, sis, now=BEFORE + timedelta(hours=hours))
    calls = [code for code, _ in sis.course_calls]
    assert calls.count("NINF030") == 1  # fresh for 30 days
    assert calls.count("NMAT202") == 3
    run(db, sis, now=BEFORE + timedelta(days=1))
    assert [code for code, _ in sis.course_calls].count("NMAT202") == 4
    run(db, sis, now=BEFORE + timedelta(days=31))
    assert [code for code, _ in sis.course_calls].count("NINF030") == 2


def test_failing_course_pages_never_fail_the_sync(db: Session) -> None:
    sis = FakeSis({"rozvrhng": ok(ROZVRHNG)}, course_error=SisError("timed out"))
    report = run(db, sis)
    statuses = {r.source: r.status for r in report.results}
    assert statuses[FetchSource.ROZVRHNG] is FetchStatus.OK
    assert statuses[FetchSource.COURSE] is FetchStatus.ERROR
    assert report.pushes == 0
    assert count(db, Lesson) == 11


# --- ReCodEx ---


def test_recodex_feed_links_by_slot(db: Session) -> None:
    from tests.school_helpers import RECODEX

    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    rx = FakeRecodex(feed=ok((RECODEX / "ical.ics").read_bytes()), api=False)
    run(db, recodex=rx)
    event = db.scalars(select(SchoolEvent)).one()
    assert (event.source, event.kind) == (SchoolEventSource.RECODEX, SchoolEventKind.DEADLINE)
    assert event.title == "5P 0.1 Hello World"
    assert event.start_utc == datetime(2026, 10, 25, 22, 59, 59, tzinfo=UTC)
    assert event.course is not None and event.course.code == "NINF030"  # Thu 9:50 N8


def test_recodex_api_points_and_scores(db: Session) -> None:
    from tests.school_helpers import RECODEX

    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    rx = FakeRecodex(
        feed=ok((RECODEX / "ical.ics").read_bytes()),
        groups=recodex_groups(),
        assignments=recodex_assignments(),
    )
    run(db, recodex=rx)
    event = db.scalars(select(SchoolEvent)).one()  # the feed row and the API row are one
    assert (event.points, event.max_points) == (Decimal(1), Decimal(1))
    assert event.location == "Programming 1 (Thu, 9:50, N8)"  # the feed's text stays
    scores = {(s.course.code, s.source): s for s in db.scalars(select(CourseScore))}
    prog = scores[("NINF030", ScoreSource.RECODEX)]
    assert (prog.gained, prog.passes) == (Decimal(2), True)
    net = scores[("NINF141", ScoreSource.RECODEX)]
    assert (net.pass_threshold, net.passes) == (Decimal(2), False)
    run(db, recodex=rx, now=BEFORE + timedelta(hours=1))
    assert count(db, SchoolEvent) == 1 and count(db, CourseScore) == 3


def test_recodex_denied_pushes(db: Session) -> None:
    sender = Sender()
    rx = FakeRecodex(groups=RecodexDenied("HTTP 401"))
    report = run(db, recodex=rx, sender=sender)
    assert report.results[0].status is FetchStatus.DENIED
    assert list(db.scalars(select(Notification.dedupe_key))) == ["school:recodex:2026-09-25"]


# --- Owl ---


@pytest.fixture
def lessons(db: Session) -> None:
    run(db, FakeSis({"rozvrhng": ok(ROZVRHNG)}))


NOW_OWL = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def test_owl_links_and_deadlines(db: Session, lessons: None) -> None:
    owl = owl_fixture()
    report = run(db, owl=owl, now=NOW_OWL)
    assert report.results[-1].status is FetchStatus.OK
    links = {o.path: (o.course.code if o.course else None) for o in db.scalars(select(OwlCourse))}
    # A ticket id links exactly; "la1-svobodova" (filed under last summer) by name.
    assert links == {"zs2627/26aNMAT202x06": "NMAT202", "ls2526/la1-svobodova": "NMAT101"}
    events = {e.uid: e for e in db.scalars(select(SchoolEvent))}
    assert set(events) == {
        "zs2627/26aNMAT202x06/ukol-1",
        "zs2627/26aNMAT202x06/ukol-2",
        "ls2526/la1-svobodova/hw1",
    }
    hw = events["zs2627/26aNMAT202x06/ukol-2"]
    assert (hw.points, hw.max_points, hw.description) == (
        Decimal("1.50"),
        Decimal("3.00"),
        "Domácí úkoly",
    )
    assert hw.url == "https://owl.example.edu/c/zs2627/26aNMAT202x06/ukol-2/"
    score = db.scalars(select(CourseScore).where(CourseScore.source == ScoreSource.OWL)).all()
    by_code = {s.course.code: s for s in score}
    assert (by_code["NMAT202"].gained, by_code["NMAT202"].pass_threshold) == (
        Decimal("1.50"),
        Decimal("100.00"),
    )
    assert by_code["NMAT202"].passes is False
    rules = db.scalars(select(OwlCourse.description).where(OwlCourse.path.like("zs%"))).one()
    assert rules and "100 bodů" in rules


def test_owl_manual_link_sticks(db: Session, lessons: None) -> None:
    owl = owl_fixture()
    run(db, owl=owl, now=NOW_OWL)
    la = db.scalars(select(OwlCourse).where(OwlCourse.path == "ls2526/la1-svobodova")).one()
    other = db.scalars(select(Course).where(Course.code == "NINF120")).one()
    school.link_owl_course(db, la.id, other.id)
    run(db, owl=owl, now=NOW_OWL + timedelta(hours=1))
    db.refresh(la)
    assert la.course_id == other.id and la.link_manual
    hw = db.scalars(select(SchoolEvent).where(SchoolEvent.uid == "ls2526/la1-svobodova/hw1")).one()
    assert hw.course_id == other.id


def test_owl_skips_old_courses_and_never_guesses(db: Session, lessons: None) -> None:
    owl = FakeOwl(
        courses_html="""<h2>Summer semester 2024/2025</h2>
        <a href="/c/ls2425/old-course/">Old course</a>
        <h2>Winter semester 2026/2027</h2>
        <a href="/c/zs2627/x-unknown/">Something unknown</a>""",
        pages={"zs2627/x-unknown": "<h2>Something unknown</h2>"},
    )
    run(db, owl=owl, now=NOW_OWL)
    assert owl.fetched == ["zs2627/x-unknown"]
    row = db.scalars(select(OwlCourse)).one()
    assert row.course_id is None


def test_owl_expired_cookie(db: Session) -> None:
    sender = Sender()
    report = run(db, owl=FakeOwl(denied=True), sender=sender, now=NOW_OWL)
    assert report.results[0].status is FetchStatus.DENIED
    assert list(db.scalars(select(Notification.dedupe_key))) == ["school:owl:2026-10-06"]


def test_owl_vanished_topic_is_cancelled(db: Session, lessons: None) -> None:
    owl = owl_fixture()
    run(db, owl=owl, now=NOW_OWL)
    page = owl.pages["zs2627/26aNMAT202x06"]
    owl.pages["zs2627/26aNMAT202x06"] = page.replace(
        'href="/c/zs2627/26aNMAT202x06/ukol-2/"', 'href="/c/zs2627/26aNMAT202x06/ukol-3/"'
    )
    run(db, owl=owl, now=NOW_OWL + timedelta(hours=1))
    old = db.scalars(select(SchoolEvent).where(SchoolEvent.uid.like("%/ukol-2"))).one()
    la = db.scalars(select(SchoolEvent).where(SchoolEvent.uid.like("%/hw1"))).one()
    assert old.cancelled and not la.cancelled  # only within the same Owl course
