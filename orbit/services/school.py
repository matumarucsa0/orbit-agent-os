"""School (SPEC §2.20): queries, the week view, manual exams and deadlines, and the L1 pushes
(tomorrow's lessons, exam and deadline reminders, schedule changes, token expiry). Callers commit.

Ingest from SIS, ReCodEx and Owl is in `school_sync.py`. Nothing here sends anything to Claude.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.base import utcnow
from orbit.domain.enums import (
    FetchSource,
    FetchStatus,
    LessonKind,
    SchoolEventKind,
    SchoolEventSource,
    ScoreSource,
)
from orbit.domain.inbox import Notification
from orbit.domain.normalize import normalize_text
from orbit.domain.school import Course, CourseScore, Lesson, OwlCourse, SchoolEvent, SisFetch
from orbit.integrations.ntfy import PushMessage, PushSender, view_action
from orbit.services import notifications
from orbit.services.errors import InvalidInputError, NotFoundError

UPCOMING_DAYS = 60
CHANGE_DAYS = 14  # schedule changes this close are pushed
CHANGE_GAP = timedelta(hours=1)  # at most one schedule-change push per hour
CHANGE_MAX_AGE = timedelta(days=2)
REMINDER_TIME = time(18, 0)  # exam and deadline reminders
EXAM_DAYS = (7, 1)
DEADLINE_DAYS = (3, 1)
TOKEN_WARN_DAYS = 14
TOMORROW_LINES = 6
CHANGE_LINES = 5
DIGEST_ENTRIES = 5
MANUAL_KINDS = (SchoolEventKind.DEADLINE, SchoolEventKind.EXAM, SchoolEventKind.OTHER)
TITLE_CHARS = 300
NOTE_CHARS = 5000

TOMORROW_KIND = "school_tomorrow"
EXAM_KIND = "school_exam"
DEADLINE_KIND = "school_deadline"
CHANGE_KIND = "school_change"
FEED_KIND = "school_feed"
TOKEN_KIND = "school_token"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_KIND_LABELS = {LessonKind.LECTURE: "lecture", LessonKind.SEMINAR: "seminar"}


def school_year(day: date) -> int:
    """The school year a day belongs to: 2026 from 1 Sep 2026 to 31 Aug 2027."""
    return day.year if day.month >= 9 else day.year - 1


def clock(moment: datetime, tz: ZoneInfo) -> str:
    """ "9:50" (no leading zero, like SIS)."""
    local = moment.astimezone(tz)
    return f"{local.hour}:{local.minute:02d}"


def short_day(moment: datetime, tz: ZoneInfo) -> str:
    """ "Thu 1 Oct"."""
    local = moment.astimezone(tz)
    return f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}"


def when_text(moment: datetime, tz: ZoneInfo, *, all_day: bool = False) -> str:
    """ "Thu 1 Oct 9:50", or "Thu 1 Oct" for an all-day item."""
    return short_day(moment, tz) if all_day else f"{short_day(moment, tz)} {clock(moment, tz)}"


def points_text(value: Decimal | None) -> str:
    if value is None:
        return "?"
    text = f"{value.normalize():f}"
    return text


# --- Courses ---


def get_course(session: Session, code: str) -> Course:
    course = session.scalar(select(Course).where(Course.code == code.strip().upper()))
    if course is None:
        raise NotFoundError(f"Course {code} not found.")
    return course


def course_year(session: Session, course: Course, today: date) -> int:
    """The school year of the course's newest lesson (for its SIS page), else this one."""
    newest = session.scalar(
        select(Lesson.ticket_code)
        .where(Lesson.course_id == course.id, Lesson.ticket_code.is_not(None))
        .order_by(Lesson.start_utc.desc())
        .limit(1)
    )
    if newest and newest[:2].isdigit():
        return 2000 + int(newest[:2])
    return school_year(today)


def current_semester(session: Session) -> str | None:
    """The newest semester any lesson's ticket belongs to ("26a" winter 2026, "26b" summer)."""
    prefixes = session.scalars(
        select(func.substr(Lesson.ticket_code, 1, 3)).where(Lesson.ticket_code.is_not(None))
    ).all()
    found = {p for p in prefixes if p and len(p) == 3 and p[:2].isdigit() and p[2] in "ab"}
    return max(found, default=None)


def current_courses(session: Session) -> list[Course]:
    """Courses with a lesson in the current semester, by name."""
    semester = current_semester(session)
    if semester is None:
        return []
    ids = select(Lesson.course_id).where(
        Lesson.ticket_code.like(f"{semester}%"), Lesson.course_id.is_not(None)
    )
    return list(session.scalars(select(Course).where(Course.id.in_(ids)).order_by(Course.name)))


def scores(session: Session, course_ids: Sequence[int]) -> dict[int, list[CourseScore]]:
    found: dict[int, list[CourseScore]] = {}
    if not course_ids:
        return found
    rows = session.scalars(
        select(CourseScore)
        .where(CourseScore.course_id.in_(list(course_ids)))
        .order_by(CourseScore.source)
    )
    for row in rows:
        found.setdefault(row.course_id, []).append(row)
    return found


def next_lessons(session: Session, now: datetime) -> dict[int, Lesson]:
    """course id -> its next lesson that isn't cancelled."""
    rows = session.scalars(
        select(Lesson)
        .where(Lesson.start_utc >= now, Lesson.cancelled.is_(False), Lesson.course_id.is_not(None))
        .order_by(Lesson.start_utc, Lesson.id)
    )
    found: dict[int, Lesson] = {}
    for lesson in rows:
        assert lesson.course_id is not None
        found.setdefault(lesson.course_id, lesson)
    return found


# --- Lessons and events ---


def list_lessons(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    course_id: int | None = None,
    include_cancelled: bool = True,
) -> list[Lesson]:
    """Lessons starting in [start, end), earliest first."""
    query = (
        select(Lesson)
        .where(Lesson.start_utc >= start, Lesson.start_utc < end)
        .options(selectinload(Lesson.course))
        .order_by(Lesson.start_utc, Lesson.id)
    )
    if course_id is not None:
        query = query.where(Lesson.course_id == course_id)
    if not include_cancelled:
        query = query.where(Lesson.cancelled.is_(False))
    return list(session.scalars(query))


def list_events(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    kinds: Sequence[SchoolEventKind] | None = None,
    course_id: int | None = None,
    include_cancelled: bool = False,
) -> list[SchoolEvent]:
    """Events starting in [start, end), earliest first."""
    query = (
        select(SchoolEvent)
        .where(SchoolEvent.start_utc >= start, SchoolEvent.start_utc < end)
        .options(selectinload(SchoolEvent.course))
        .order_by(SchoolEvent.start_utc, SchoolEvent.id)
    )
    if kinds:
        query = query.where(SchoolEvent.kind.in_(list(kinds)))
    if course_id is not None:
        query = query.where(SchoolEvent.course_id == course_id)
    if not include_cancelled:
        query = query.where(SchoolEvent.cancelled.is_(False))
    return list(session.scalars(query))


def upcoming(session: Session, now: datetime, days: int = UPCOMING_DAYS) -> list[SchoolEvent]:
    """Exams and deadlines from now for `days` days (all-day ones from the start of today)."""
    return list_events(
        session,
        now - timedelta(days=1),
        now + timedelta(days=days),
        kinds=(SchoolEventKind.EXAM, SchoolEventKind.DEADLINE),
    )


def is_past(event: SchoolEvent, now: datetime) -> bool:
    if event.all_day:
        return (event.end_utc or event.start_utc) < now
    return event.start_utc < now


def days_until(moment: datetime, now: datetime, tz: ZoneInfo) -> int:
    return (moment.astimezone(tz).date() - now.astimezone(tz).date()).days


def full_points(event: SchoolEvent) -> bool:
    return (
        event.points is not None
        and event.max_points is not None
        and event.max_points > 0
        and event.points >= event.max_points
    )


@dataclass
class Day:
    day: date
    lessons: list[Lesson] = field(default_factory=list)
    events: list[SchoolEvent] = field(default_factory=list)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def local_bounds(first: date, last: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start of `first`, start of the day after `last`) in UTC."""
    start = datetime.combine(first, time.min, tz).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time.min, tz).astimezone(UTC)
    return start, end


def week(session: Session, monday: date, tz: ZoneInfo) -> list[Day]:
    """Seven days from `monday`: lessons (cancelled ones too) and events (not cancelled)."""
    start, end = local_bounds(monday, monday + timedelta(days=6), tz)
    days = {monday + timedelta(days=i): Day(monday + timedelta(days=i)) for i in range(7)}
    for lesson in list_lessons(session, start, end):
        days[lesson.start_utc.astimezone(tz).date()].lessons.append(lesson)
    for event in list_events(session, start, end):
        local = event.start_utc.astimezone(tz).date()
        if local in days:
            days[local].events.append(event)
    return list(days.values())


# --- Manual items ---


@dataclass(frozen=True)
class ManualEventInput:
    title: str
    kind: SchoolEventKind
    day: date
    at: time | None = None  # None = all day
    course_id: int | None = None
    note: str | None = None


def _manual_values(
    session: Session, data: ManualEventInput, tz: ZoneInfo
) -> tuple[str, datetime, datetime | None, bool, str | None]:
    title = " ".join(data.title.split())
    if not title:
        raise InvalidInputError("A title is required.")
    if len(title) > TITLE_CHARS:
        raise InvalidInputError(f"The title is longer than {TITLE_CHARS} characters.")
    if data.kind not in MANUAL_KINDS:
        raise InvalidInputError("Kind must be deadline, exam or other.")
    note = (data.note or "").strip() or None
    if note and len(note) > NOTE_CHARS:
        raise InvalidInputError(f"The note is longer than {NOTE_CHARS} characters.")
    if data.course_id is not None and session.get(Course, data.course_id) is None:
        raise InvalidInputError(f"Course {data.course_id} not found.")
    if data.at is None:
        start = datetime.combine(data.day, time.min, tz).astimezone(UTC)
        end = datetime.combine(data.day, time(23, 59), tz).astimezone(UTC)
        return title, start, end, True, note
    return title, datetime.combine(data.day, data.at, tz).astimezone(UTC), None, False, note


def get_event(session: Session, event_id: int) -> SchoolEvent:
    event = session.get(SchoolEvent, event_id)
    if event is None:
        raise NotFoundError(f"School event {event_id} not found.")
    return event


def _manual(session: Session, event_id: int) -> SchoolEvent:
    event = get_event(session, event_id)
    if event.source not in (SchoolEventSource.MANUAL, SchoolEventSource.PROPOSAL):
        raise InvalidInputError(
            "Only items added by hand or from the Inbox can be changed; the rest come from feeds."
        )
    return event


def create_event(session: Session, data: ManualEventInput, tz: ZoneInfo) -> SchoolEvent:
    title, start, end, all_day, note = _manual_values(session, data, tz)
    event = SchoolEvent(
        source=SchoolEventSource.MANUAL,
        uid=None,
        kind=data.kind,
        title=title,
        description=note,
        course_id=data.course_id,
        start_utc=start,
        end_utc=end,
        all_day=all_day,
    )
    session.add(event)
    session.flush()
    return event


def update_event(
    session: Session, event_id: int, data: ManualEventInput, tz: ZoneInfo
) -> SchoolEvent:
    event = _manual(session, event_id)
    title, start, end, all_day, note = _manual_values(session, data, tz)
    event.title, event.kind, event.description = title, data.kind, note
    event.course_id = data.course_id
    event.start_utc, event.end_utc, event.all_day = start, end, all_day
    session.flush()
    return event


def delete_event(session: Session, event_id: int) -> None:
    session.delete(_manual(session, event_id))
    session.flush()


# --- Owl links ---


def owl_courses(session: Session) -> list[OwlCourse]:
    return list(
        session.scalars(
            select(OwlCourse).options(selectinload(OwlCourse.course)).order_by(OwlCourse.path)
        )
    )


def link_owl_course(session: Session, owl_id: int, course_id: int | None) -> OwlCourse:
    """My pick of the SIS course an Owl course belongs to (None: not a course of mine). Kept on
    every sync; its Owl deadlines move to the course."""
    owl = session.get(OwlCourse, owl_id)
    if owl is None:
        raise NotFoundError(f"Owl course {owl_id} not found.")
    if course_id is not None and session.get(Course, course_id) is None:
        raise InvalidInputError(f"Course {course_id} not found.")
    owl.course_id = course_id
    owl.link_manual = True
    prefix = f"{owl.path}/"
    for event in session.scalars(
        select(SchoolEvent).where(
            SchoolEvent.source == SchoolEventSource.OWL, SchoolEvent.uid.like(f"{prefix}%")
        )
    ):
        event.course_id = course_id
    session.flush()
    return owl


# --- Sync status ---


@dataclass(frozen=True)
class FeedStatus:
    source: FetchSource
    last: SisFetch | None
    last_ok: SisFetch | None


GOOD = (FetchStatus.OK, FetchStatus.UNCHANGED, FetchStatus.EMPTY)


def fetch_status(session: Session, sources: Sequence[FetchSource]) -> list[FeedStatus]:
    """The last fetch and the last good one of each source."""
    found = []
    for source in sources:
        query = select(SisFetch).where(SisFetch.source == source).order_by(SisFetch.id.desc())
        last = session.scalars(query.limit(1)).first()
        ok = session.scalars(query.where(SisFetch.status.in_(GOOD)).limit(1)).first()
        found.append(FeedStatus(source, last, ok))
    return found


def has_school_data(session: Session) -> bool:
    return session.scalar(select(Lesson.id).limit(1)) is not None


# --- Pushes ---


def _course_name(lesson: Lesson) -> str:
    return lesson.course.name if lesson.course is not None else lesson.title


def lesson_line(lesson: Lesson, tz: ZoneInfo) -> str:
    """ "9:00 N8 Algoritmizace (seminar)"."""
    kind = _KIND_LABELS.get(lesson.kind)
    parts = [clock(lesson.start_utc, tz), lesson.room or "", lesson.title]
    text = " ".join(p for p in parts if p)
    return f"{text} ({kind})" if kind else text


def tomorrow_push(lessons: Sequence[Lesson], tz: ZoneInfo, base_url: str) -> PushMessage:
    first = lessons[0]
    head = f"first {clock(first.start_utc, tz)}"
    if first.room:
        head += f" {first.room}"
    noun = "lesson" if len(lessons) == 1 else "lessons"
    lines = [lesson_line(lesson, tz) for lesson in lessons[:TOMORROW_LINES]]
    if len(lessons) > TOMORROW_LINES:
        lines.append(f"… and {len(lessons) - TOMORROW_LINES} more")
    url = f"{base_url}/school"
    return PushMessage(
        title=f"🎓 Tomorrow: {len(lessons)} {noun}, {head} ({first.title})",
        body="\n".join(lines),
        click=url,
        tags=("mortar_board",),
        actions=(view_action("Open", url),),
    )


def event_course(event: SchoolEvent) -> str | None:
    return event.course.name if event.course is not None else None


def reminder_push(event: SchoolEvent, days: int, tz: ZoneInfo, base_url: str) -> PushMessage:
    noun = "Exam" if event.kind is SchoolEventKind.EXAM else "Deadline"
    when = "tomorrow" if days == 1 else f"in {days} days"
    course = event_course(event)
    name = event.title if not course or course in event.title else f"{course}: {event.title}"
    facts = [when_text(event.start_utc, tz, all_day=event.all_day)]
    if event.location and event.source is not SchoolEventSource.RECODEX:
        facts.append(event.location)
    if event.max_points is not None:
        gained = points_text(event.points or Decimal(0))
        facts.append(f"{gained}/{points_text(event.max_points)} points so far")
    url = event.url or f"{base_url}/school/upcoming"
    icon = "📝" if event.kind is SchoolEventKind.EXAM else "⏰"
    return PushMessage(
        title=f"{icon} {noun} {when}: {name}"[:200],
        body=", ".join(facts),
        click=url,
        tags=("pencil",) if event.kind is SchoolEventKind.EXAM else ("alarm_clock",),
        actions=(view_action("Open", url),),
    )


def change_line(lesson: Lesson, tz: ZoneInfo) -> str:
    """ "Programování 1 (seminar) Fri 2 Oct 9:50: moved from Thu 1 Oct 12:20"."""
    kind = _KIND_LABELS.get(lesson.kind)
    name = f"{lesson.title} ({kind})" if kind else lesson.title
    return f"{name} {when_text(lesson.start_utc, tz)}: {lesson.change_note or 'changed'}"


def change_push(lines: Sequence[str], base_url: str) -> PushMessage:
    shown = list(lines[:CHANGE_LINES])
    if len(lines) > CHANGE_LINES:
        shown.append(f"… and {len(lines) - CHANGE_LINES} more")
    url = f"{base_url}/school"
    return PushMessage(
        title="🎓 Schedule change" if len(lines) == 1 else f"🎓 {len(lines)} schedule changes",
        body="\n".join(shown),
        click=url,
        tags=("mortar_board",),
        actions=(view_action("Week", url),),
    )


def feed_push(title: str, body: str, base_url: str) -> PushMessage:
    url = f"{base_url}/school/status"
    return PushMessage(
        title=title, body=body, click=url, tags=("warning",), actions=(view_action("Status", url),)
    )


@dataclass
class _Out:
    session: Session
    sender: PushSender | None
    quiet: notifications.QuietHours
    now: datetime
    count: int = 0

    def send(self, message: PushMessage, *, dedupe_key: str, kind: str) -> None:
        if notifications.was_sent(self.session, dedupe_key):
            return
        record = notifications.notify(
            self.session,
            self.sender,
            message,
            dedupe_key=dedupe_key,
            kind=kind,
            quiet=self.quiet,
            now=self.now,
        )
        self.count += record is not None


def _arrives_today(now: datetime, quiet: notifications.QuietHours, tz: ZoneInfo) -> bool:
    if not quiet.contains(now):
        return True
    return quiet.end_after(now).astimezone(tz).date() == now.astimezone(tz).date()


def send_tomorrow(out: _Out, *, tz: ZoneInfo, evening: time, base_url: str) -> None:
    local = out.now.astimezone(tz)
    if local.time() < evening or not _arrives_today(out.now, out.quiet, tz):
        return
    tomorrow = local.date() + timedelta(days=1)
    start, end = local_bounds(tomorrow, tomorrow, tz)
    lessons = list_lessons(out.session, start, end, include_cancelled=False)
    if lessons:
        out.send(
            tomorrow_push(lessons, tz, base_url),
            dedupe_key=f"{TOMORROW_KIND}:{tomorrow.isoformat()}",
            kind=TOMORROW_KIND,
        )


def send_event_reminders(
    out: _Out,
    *,
    tz: ZoneInfo,
    base_url: str,
    at: time = REMINDER_TIME,
    exam_days: Sequence[int] = EXAM_DAYS,
    deadline_days: Sequence[int] = DEADLINE_DAYS,
) -> None:
    local = out.now.astimezone(tz)
    if local.time() < at:
        return
    today = local.date()
    horizon = max(*exam_days, *deadline_days)
    start, end = local_bounds(today + timedelta(days=1), today + timedelta(days=horizon), tz)
    for event in list_events(
        out.session, start, end, kinds=(SchoolEventKind.EXAM, SchoolEventKind.DEADLINE)
    ):
        days = days_until(event.start_utc, out.now, tz)
        exam = event.kind is SchoolEventKind.EXAM
        if days not in (exam_days if exam else deadline_days):
            continue
        if not exam and days == 1 and full_points(event):
            continue  # nothing left to do for it
        kind = EXAM_KIND if exam else DEADLINE_KIND
        out.send(
            reminder_push(event, days, tz, base_url),
            dedupe_key=f"{kind}:{event.id}:{event.start_utc:%Y%m%dT%H%M}:{days}d",
            kind=kind,
        )


def last_change_push(session: Session) -> datetime | None:
    last = session.scalar(
        select(func.max(Notification.created_at)).where(Notification.kind == CHANGE_KIND)
    )
    if last is None:
        return None
    return last if last.tzinfo else last.replace(tzinfo=UTC)


def pending_changes(session: Session, now: datetime) -> list[Lesson]:
    """Lessons in the next CHANGE_DAYS that changed after the last schedule-change push (and in
    the last CHANGE_MAX_AGE: an old change is no news)."""
    since = now - CHANGE_MAX_AGE
    last = last_change_push(session)
    if last is not None and last > since:
        since = last
    return list(
        session.scalars(
            select(Lesson)
            .where(
                Lesson.changed_at > since,
                Lesson.start_utc >= now,
                Lesson.start_utc < now + timedelta(days=CHANGE_DAYS),
            )
            .options(selectinload(Lesson.course))
            .order_by(Lesson.start_utc, Lesson.id)
        )
    )


def send_changes(out: _Out, *, tz: ZoneInfo, base_url: str, gap: timedelta = CHANGE_GAP) -> None:
    last = last_change_push(out.session)
    if last is not None and out.now - last < gap:
        return
    changed = pending_changes(out.session, out.now)
    if not changed:
        return
    lines = [change_line(lesson, tz) for lesson in changed]
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    out.send(change_push(lines, base_url), dedupe_key=f"{CHANGE_KIND}:{digest}", kind=CHANGE_KIND)


def send_token_warning(
    out: _Out,
    *,
    expires: datetime | None,
    tz: ZoneInfo,
    base_url: str,
    warn_days: int = TOKEN_WARN_DAYS,
) -> None:
    if expires is None or out.now < expires - timedelta(days=warn_days):
        return
    days = max(days_until(expires, out.now, tz), 0)
    out.send(
        feed_push(
            "🔑 ReCodEx token expires soon" if days else "🔑 ReCodEx token expired",
            f"It ends {short_day(expires, tz)}. Make a new read-only token on your ReCodEx "
            "profile and put it in RECODEX_API_TOKEN.",
            base_url,
        ),
        dedupe_key=f"school:recodex_exp:{expires:%Y%m%d}",
        kind=TOKEN_KIND,
    )


def send_school_pushes(
    session: Session,
    sender: PushSender | None,
    *,
    base_url: str,
    tz: ZoneInfo,
    quiet: notifications.QuietHours,
    evening: time,
    recodex_expires: datetime | None = None,
    now: datetime | None = None,
    reminder_time: time = REMINDER_TIME,
    exam_days: Sequence[int] = EXAM_DAYS,
    deadline_days: Sequence[int] = DEADLINE_DAYS,
    change_gap: timedelta = CHANGE_GAP,
    token_warn_days: int = TOKEN_WARN_DAYS,
) -> int:
    """Record (and send, or queue for after quiet hours) the school pushes that are due.
    Each has a dedupe key, so running this every minute sends each one once."""
    out = _Out(session, sender, quiet, now or utcnow())
    send_tomorrow(out, tz=tz, evening=evening, base_url=base_url)
    send_event_reminders(
        out,
        tz=tz,
        base_url=base_url,
        at=reminder_time,
        exam_days=exam_days,
        deadline_days=deadline_days,
    )
    send_changes(out, tz=tz, base_url=base_url, gap=change_gap)
    send_token_warning(
        out, expires=recodex_expires, tz=tz, base_url=base_url, warn_days=token_warn_days
    )
    session.flush()
    return out.count


# --- The Sunday digest ---


@dataclass(frozen=True)
class WeekSummary:
    lessons: int
    exams: list[SchoolEvent]
    deadlines: list[SchoolEvent]

    @property
    def empty(self) -> bool:
        return not (self.lessons or self.exams or self.deadlines)


def week_summary(session: Session, start: datetime, end: datetime) -> WeekSummary:
    lessons = session.scalar(
        select(func.count(Lesson.id)).where(
            Lesson.start_utc >= start, Lesson.start_utc < end, Lesson.cancelled.is_(False)
        )
    )
    events = list_events(
        session, start, end, kinds=(SchoolEventKind.EXAM, SchoolEventKind.DEADLINE)
    )
    return WeekSummary(
        lessons=lessons or 0,
        exams=[e for e in events if e.kind is SchoolEventKind.EXAM],
        deadlines=[e for e in events if e.kind is SchoolEventKind.DEADLINE],
    )


def digest_section(summary: WeekSummary, tz: ZoneInfo) -> str | None:
    """ "🎓 School next week: 18 lessons, 1 exam, 2 deadlines" and up to 5 entries."""
    if summary.empty:
        return None

    def count(n: int, noun: str) -> str:
        return f"{n} {noun}" + ("" if n == 1 else "s")

    head = f"🎓 School next week: {count(summary.lessons, 'lesson')}"
    if summary.exams:
        head += f", {count(len(summary.exams), 'exam')}"
    if summary.deadlines:
        head += f", {count(len(summary.deadlines), 'deadline')}"
    items = sorted(summary.exams + summary.deadlines, key=lambda e: (e.start_utc, e.id))
    lines = [head]
    for event in items[:DIGEST_ENTRIES]:
        label = "exam" if event.kind is SchoolEventKind.EXAM else "due"
        lines.append(
            f"• {when_text(event.start_utc, tz, all_day=event.all_day)} {label}: {event.title}"
        )
    if len(items) > DIGEST_ENTRIES:
        lines.append(f"• … and {len(items) - DIGEST_ENTRIES} more")
    return "\n".join(lines)


# --- Home ---


@dataclass(frozen=True)
class HomeStrip:
    next_lesson: Lesson | None
    next_item: SchoolEvent | None


def home_strip(session: Session, now: datetime) -> HomeStrip | None:
    lesson = session.scalars(
        select(Lesson)
        .where(Lesson.end_utc > now, Lesson.cancelled.is_(False))
        .options(selectinload(Lesson.course))
        .order_by(Lesson.start_utc, Lesson.id)
        .limit(1)
    ).first()
    items = [e for e in upcoming(session, now) if not is_past(e, now)]
    item = items[0] if items else None
    if lesson is None and item is None:
        return None
    return HomeStrip(lesson, item)


# --- Matching helpers shared with the sync ---


_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def base_name(name: str) -> str:
    """ "Programování 1 (Čt, 9:50, N8)" -> "Programování 1"."""
    return _PAREN_RE.sub("", name).strip()


def name_key(name: str) -> str:
    return normalize_text(base_name(name))


SCORE_LABELS = {ScoreSource.RECODEX: "ReCodEx", ScoreSource.OWL: "Owl"}
