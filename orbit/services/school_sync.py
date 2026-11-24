"""School ingest (SPEC §2.20): SIS feeds, the public course pages and harmonogram, ReCodEx and
Owl, into Course / Lesson / SchoolEvent / CourseScore. Deterministic source data (L0): nothing
here goes through a proposal and nothing is sent to Claude. Callers commit.

Rules every source follows:
- Idempotent: lessons upsert by normalized uid, events by (source, uid). A second run with the
  same data changes nothing.
- Nothing is deleted. A future lesson or event missing from a feed that parsed with at least one
  item is marked cancelled, and comes back if it reappears. Past items are never touched. An
  empty, failed or denied feed never cancels anything.
- One `SisFetch` row per source per run, with a redacted error. An unchanged body (DTSTAMP
  lines aside) ends that source's run early.
- A denied or broken source sends one push per day; the next poll simply tries again.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import (
    FetchSource,
    FetchStatus,
    SchoolEventKind,
    SchoolEventSource,
    ScoreSource,
)
from orbit.domain.normalize import normalize_text
from orbit.domain.school import Course, CourseScore, Lesson, OwlCourse, SchoolEvent, SisFetch
from orbit.integrations.ntfy import PushSender
from orbit.integrations.owl import OwlCourseRef, OwlDenied, OwlSource
from orbit.integrations.recodex import (
    RecodexDenied,
    RecodexGroup,
    RecodexSource,
    clean_title,
)
from orbit.integrations.sis import (
    FeedEvent,
    FeedFetch,
    LessonData,
    SisError,
    SisSource,
    body_hash,
    lesson_from_event,
    parse_feed,
    ticket_course_code,
    ticket_year,
)
from orbit.services import notifications, school
from orbit.settings import Settings

log = logging.getLogger(__name__)

COURSE_TRIES_PER_DAY = 3
LESSON_FEEDS = ("roz_externi", "rozvrhng")
EVENT_FEEDS = {
    "term_st2": (SchoolEventSource.EXAM_FEED, SchoolEventKind.EXAM),
    "stev": (SchoolEventSource.EVENTS_FEED, SchoolEventKind.EVENT),
}
_CODE_RE = re.compile(r"\b([A-Z]{3,5}\d{3}[A-Z]?)\b")
_GROUP_SLOT_RE = re.compile(
    r"\((?P<day>[^,()]+),\s*(?P<h>\d{1,2}):(?P<m>\d{2}),\s*(?P<room>[^()]+)\)"
)
_DAYS = {
    "mon": 0, "po": 0, "tue": 1, "út": 1, "ut": 1, "wed": 2, "st": 2,
    "thu": 3, "čt": 3, "ct": 3, "fri": 4, "pá": 4, "pa": 4, "sat": 5, "so": 5, "sun": 6, "ne": 6,
}  # fmt: skip


@dataclass
class SchoolSources:
    sis: SisSource | None = None
    recodex: RecodexSource | None = None
    owl: OwlSource | None = None

    @property
    def configured(self) -> bool:
        return bool(self.sis and self.sis.feeds) or self.recodex is not None or self.owl is not None


@dataclass(frozen=True)
class SyncConfig:
    base_url: str  # Orbit's, for push links
    tz: ZoneInfo
    quiet: notifications.QuietHours
    faculty: str = "10000"
    course_ttl_days: int = 30
    owl_base_url: str = "https://owl.example.edu"


def config_from(settings: Settings) -> SyncConfig:
    return SyncConfig(
        base_url=settings.orbit_url,
        tz=settings.timezone,
        quiet=notifications.quiet_hours(settings),
        faculty=settings.sis_faculty_id,
        course_ttl_days=settings.sis_course_ttl_days,
        owl_base_url=settings.owl_base_url,
    )


@dataclass
class SourceResult:
    source: FetchSource
    status: FetchStatus
    seen: int = 0
    changed: int = 0
    error: str | None = None


@dataclass
class SyncReport:
    results: list[SourceResult] = field(default_factory=list)
    pushes: int = 0

    @property
    def summary(self) -> str:
        if not self.results:
            return "nothing is set up"
        parts = []
        for r in self.results:
            text = f"{r.source.value} {r.status.value}"
            if r.status in (FetchStatus.OK, FetchStatus.EMPTY):
                text += f" ({r.seen} items, {r.changed} changed)"
            parts.append(text)
        return ", ".join(parts)

    @property
    def problems(self) -> list[SourceResult]:
        return [r for r in self.results if r.status in (FetchStatus.DENIED, FetchStatus.ERROR)]


# --- Bookkeeping ---


def _last_hash(session: Session, source: FetchSource) -> str | None:
    """The body hash of the last good fetch; None after an empty one, so an empty feed is
    looked at (and reported) every time, not skipped as unchanged."""
    last = session.scalars(
        select(SisFetch)
        .where(SisFetch.source == source, SisFetch.status.in_(school.GOOD))
        .order_by(SisFetch.id.desc())
        .limit(1)
    ).first()
    if last is None or last.status is FetchStatus.EMPTY:
        return None
    return last.body_hash


def _record(
    session: Session,
    result: SourceResult,
    started: datetime,
    now: datetime,
    digest: str | None = None,
) -> SourceResult:
    session.add(
        SisFetch(
            source=result.source,
            started_at=started,
            finished_at=now,
            status=result.status,
            items_seen=result.seen,
            items_changed=result.changed,
            body_hash=digest,
            error=(result.error or "")[:2000] or None,
        )
    )
    session.flush()
    return result


def _problem_push(
    session: Session,
    sender: PushSender | None,
    config: SyncConfig,
    *,
    key: str,
    title: str,
    body: str,
    now: datetime,
) -> int:
    day = now.astimezone(config.tz).date().isoformat()
    record = notifications.notify(
        session,
        sender,
        school.feed_push(title, body, config.base_url),
        dedupe_key=f"{key}:{day}",
        kind=school.FEED_KIND,
        quiet=config.quiet,
        now=now,
    )
    return record is not None


# --- Courses ---


def course_for_code(session: Session, code: str, name: str) -> Course:
    """The course with this code, made (with the name the feed gave) when it's new."""
    course = session.scalar(select(Course).where(Course.code == code))
    if course is None:
        course = Course(code=code, name=school.base_name(name) or code)
        session.add(course)
        session.flush()
    return course


def _code_in_text(session: Session, *texts: str | None) -> Course | None:
    """A known course whose code appears in the text (exam titles name the course code)."""
    codes = {m for text in texts if text for m in _CODE_RE.findall(text)}
    if not codes:
        return None
    found = list(session.scalars(select(Course).where(Course.code.in_(sorted(codes)))))
    return found[0] if len(found) == 1 else None


# --- Lessons ---


def _apply_lesson(
    lesson: Lesson, data: LessonData, course: Course | None, tz: ZoneInfo
) -> str | None:
    """Copy the feed over a future lesson; returns what changed for the change push."""
    notes = []
    if lesson.start_utc != data.start or lesson.end_utc != data.end:
        notes.append(f"moved from {school.when_text(lesson.start_utc, tz)}")
    if (lesson.room or None) != (data.room or None):
        notes.append(f"room {lesson.room or '?'} → {data.room or '?'}")
    if lesson.cancelled:
        notes.append("back on")
    lesson.cancelled = False
    lesson.start_utc, lesson.end_utc = data.start, data.end
    lesson.title, lesson.teachers_text = data.title, data.teachers
    lesson.room, lesson.location = data.room, data.location
    lesson.ticket_code, lesson.kind = data.ticket, data.kind
    if course is not None:
        lesson.course_id = course.id
    lesson.raw_json = {"uid": data.raw_uid}
    return ", ".join(notes) or None


def sync_lessons(
    session: Session, source: FetchSource, fetch: FeedFetch, config: SyncConfig, now: datetime
) -> SourceResult:
    started = now
    if fetch.status is not FetchStatus.OK:
        return _record(session, SourceResult(source, fetch.status, error=fetch.error), started, now)
    digest = body_hash(fetch.body)
    if digest == _last_hash(session, source):
        return _record(session, SourceResult(source, FetchStatus.UNCHANGED), started, now, digest)
    try:
        events = parse_feed(fetch.body)
    except SisError as exc:
        return _record(
            session, SourceResult(source, FetchStatus.ERROR, error=str(exc)), started, now
        )
    if not events:
        return _record(session, SourceResult(source, FetchStatus.EMPTY), started, now, digest)

    initial = not school.has_school_data(session)
    lessons = [lesson_from_event(e) for e in events]
    by_uid = {data.uid: data for data in lessons}
    existing = {
        lesson.uid: lesson
        for lesson in session.scalars(select(Lesson).where(Lesson.uid.in_(list(by_uid))))
    }
    result = SourceResult(source, FetchStatus.OK, seen=len(by_uid))
    for uid, data in by_uid.items():
        course = (
            course_for_code(session, data.course_code, data.title) if data.course_code else None
        )
        lesson = existing.get(uid)
        if lesson is None:
            lesson = Lesson(
                uid=uid,
                ticket_code=data.ticket,
                kind=data.kind,
                course_id=course.id if course else None,
                title=data.title,
                teachers_text=data.teachers,
                room=data.room,
                location=data.location,
                start_utc=data.start,
                end_utc=data.end,
                first_seen_at=now,
                raw_json={"uid": data.raw_uid},
            )
            if not initial and data.start > now:
                lesson.changed_at, lesson.change_note = now, "new"
            session.add(lesson)
            result.changed += 1
            continue
        if lesson.start_utc < now:
            continue  # the past is never rewritten
        note = _apply_lesson(lesson, data, course, config.tz)
        if note:
            lesson.changed_at, lesson.change_note = now, note
            result.changed += 1
    session.flush()

    stale = session.scalars(
        select(Lesson).where(
            Lesson.cancelled.is_(False), Lesson.start_utc > now, Lesson.uid.not_in(list(by_uid))
        )
    )
    for lesson in stale:
        lesson.cancelled = True
        lesson.changed_at, lesson.change_note = now, "cancelled"
        result.changed += 1
    session.flush()
    return _record(session, result, started, now, digest)


def future_lessons(session: Session, now: datetime) -> bool:
    return (
        session.scalar(
            select(Lesson.id).where(Lesson.start_utc > now, Lesson.cancelled.is_(False)).limit(1)
        )
        is not None
    )


# --- Events (exam terms, SIS events, ReCodEx, harmonogram, Owl) ---


@dataclass(frozen=True)
class EventData:
    uid: str
    kind: SchoolEventKind
    title: str
    start: datetime
    end: datetime | None = None
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    url: str | None = None
    course_id: int | None = None
    # True: only fill an empty course_id (a guess must not undo an exact link).
    keep_course: bool = False
    # True: title, description, location and url are written only when the row is new (the
    # ReCodEx API must not flip the feed's texts to another language on every run).
    fill_only: bool = False
    points: Decimal | None = None
    max_points: Decimal | None = None
    second_deadline: datetime | None = None
    set_points: bool = False  # this source knows the points
    raw: dict[str, str] | None = None


def upsert_events(
    session: Session,
    source: SchoolEventSource,
    items: Sequence[EventData],
    now: datetime,
    *,
    scope: Callable[[SchoolEvent], bool] | None = None,
    cancel_missing: bool = True,
) -> int:
    """Upsert by (source, uid); returns how many rows were added or changed. With
    `cancel_missing`, a future row of this source (within `scope`) that is not in `items` is
    cancelled; callers pass it only when the source answered with at least one item."""
    by_uid = {item.uid: item for item in items}
    existing = {
        e.uid: e
        for e in session.scalars(
            select(SchoolEvent).where(
                SchoolEvent.source == source, SchoolEvent.uid.in_(list(by_uid))
            )
        )
    }
    changed = 0
    for uid, item in by_uid.items():
        event = existing.get(uid)
        before: tuple[object, ...] | None = None
        if event is None:
            event = SchoolEvent(
                source=source, uid=uid, kind=item.kind, title=item.title, start_utc=item.start
            )
            session.add(event)
        else:
            before = _snapshot(event)
        if before is None or not item.fill_only:
            event.title, event.description = item.title, item.description
            event.location, event.url = item.location, item.url
        event.kind = item.kind
        event.start_utc, event.end_utc, event.all_day = item.start, item.end, item.all_day
        if item.course_id is not None and (event.course_id is None or not item.keep_course):
            event.course_id = item.course_id
        elif item.course_id is None and not item.keep_course:
            event.course_id = None
        if item.set_points:
            event.points, event.max_points = item.points, item.max_points
            event.second_deadline_utc = item.second_deadline
        if item.raw is not None:
            event.raw_json = item.raw
        event.cancelled = False
        if before is None or before != _snapshot(event):
            changed += 1
    session.flush()
    if cancel_missing:
        stale = session.scalars(
            select(SchoolEvent).where(
                SchoolEvent.source == source,
                SchoolEvent.cancelled.is_(False),
                SchoolEvent.start_utc > now,
                SchoolEvent.uid.not_in(list(by_uid)),
            )
        )
        for event in stale:
            if scope is None or scope(event):
                event.cancelled = True
                changed += 1
        session.flush()
    return changed


def _snapshot(event: SchoolEvent) -> tuple[object, ...]:
    return (
        event.kind,
        event.title,
        event.start_utc,
        event.end_utc,
        event.all_day,
        event.description,
        event.location,
        event.url,
        event.course_id,
        event.points,
        event.max_points,
        event.second_deadline_utc,
        event.cancelled,
    )


def _feed_event(event: FeedEvent, kind: SchoolEventKind, course: Course | None) -> EventData:
    return EventData(
        uid=event.uid,
        kind=kind,
        title=" ".join((event.summary or "(SIS item)").split()),
        start=event.start,
        end=event.end,
        all_day=event.all_day,
        description=event.description,
        location=event.location,
        url=event.url,
        course_id=course.id if course else None,
        raw=event.raw,
    )


def sync_event_feed(session: Session, feed: str, fetch: FeedFetch, now: datetime) -> SourceResult:
    source = FetchSource(feed)
    event_source, kind = EVENT_FEEDS[feed]
    if fetch.status is not FetchStatus.OK:
        return _record(session, SourceResult(source, fetch.status, error=fetch.error), now, now)
    digest = body_hash(fetch.body)
    if digest == _last_hash(session, source):
        return _record(session, SourceResult(source, FetchStatus.UNCHANGED), now, now, digest)
    try:
        events = parse_feed(fetch.body)
    except SisError as exc:
        return _record(session, SourceResult(source, FetchStatus.ERROR, error=str(exc)), now, now)
    if not events:  # normal for exams before any is registered; never cancels
        return _record(session, SourceResult(source, FetchStatus.EMPTY), now, now, digest)
    items = [_feed_event(e, kind, _code_in_text(session, e.summary, e.description)) for e in events]
    changed = upsert_events(session, event_source, items, now)
    result = SourceResult(source, FetchStatus.OK, seen=len(items), changed=changed)
    return _record(session, result, now, now, digest)


def sync_harmonogram(
    session: Session, sis: SisSource, config: SyncConfig, now: datetime
) -> SourceResult | None:
    """The events SIS publishes to everyone for the faculty in the school year of the newest
    lesson; at most once a (local) day. None when it isn't due."""
    today = now.astimezone(config.tz).date()
    day_start, _ = school.local_bounds(today, today, config.tz)
    done = session.scalar(
        select(SisFetch.id).where(
            SisFetch.source == FetchSource.HARMONOGRAM,
            SisFetch.status.in_(school.GOOD),
            SisFetch.started_at >= day_start,
        )
    )
    newest = session.scalar(select(Lesson.start_utc).order_by(Lesson.start_utc.desc()).limit(1))
    if done is not None or newest is None:
        return None
    year = school.school_year(newest.astimezone(config.tz).date())
    first, last = date(year, 9, 1), date(year + 1, 8, 31)
    source = FetchSource.HARMONOGRAM
    try:
        rows = sis.fetch_harmonogram(config.faculty, first, last)
    except SisError as exc:
        return _record(session, SourceResult(source, FetchStatus.ERROR, error=str(exc)), now, now)
    if not rows:
        return _record(session, SourceResult(source, FetchStatus.EMPTY), now, now)
    items = [
        EventData(
            uid=hashlib.sha256(
                f"{config.faculty}\x1f{row.title}\x1f{row.start.isoformat()}".encode()
            ).hexdigest()[:40],
            kind=SchoolEventKind.TERM,
            title=row.title,
            start=row.start,
            end=row.end,
            all_day=True,
            description=row.note,
        )
        for row in rows
    ]
    lo, hi = school.local_bounds(first, last, config.tz)
    changed = upsert_events(
        session,
        SchoolEventSource.HARMONOGRAM,
        items,
        now,
        scope=lambda e: lo <= e.start_utc < hi,
    )
    return _record(session, SourceResult(source, FetchStatus.OK, len(items), changed), now, now)


# --- Course pages ---


def enrich_courses(
    session: Session, sis: SisSource, config: SyncConfig, now: datetime
) -> SourceResult | None:
    """Fill courses from their public SIS page: new ones, and the rest every TTL days. A failed
    page never fails the sync; a course is tried at most 3 times a day. None: nothing due."""
    today = now.astimezone(config.tz).date()
    stale_before = now - timedelta(days=config.course_ttl_days)
    due = []
    for course in session.scalars(select(Course).order_by(Course.id)):
        if course.detail_fetched_at is not None and course.detail_fetched_at > stale_before:
            continue
        failed_today = (
            course.detail_failed_at is not None
            and course.detail_failed_at.astimezone(config.tz).date() == today
        )
        if failed_today and course.detail_tries >= COURSE_TRIES_PER_DAY:
            continue
        due.append(course)
    if not due:
        return None
    result = SourceResult(FetchSource.COURSE, FetchStatus.OK)
    errors = []
    for course in due:
        result.seen += 1
        try:
            page = sis.fetch_course(course.code, school.course_year(session, course, today))
        except SisError as exc:
            page = None
            errors.append(f"{course.code}: {exc}")
        else:
            if page is None:
                errors.append(f"{course.code}: no course page")
        if page is None or page.code != course.code:
            failed_today = (
                course.detail_failed_at is not None
                and course.detail_failed_at.astimezone(config.tz).date() == today
            )
            course.detail_tries = course.detail_tries + 1 if failed_today else 1
            course.detail_failed_at = now
            continue
        course.name = page.name or course.name
        course.name_en, course.credits = page.name_en, page.credits
        course.hours_lecture, course.hours_seminar = page.hours_lecture, page.hours_seminar
        course.completion, course.semester = page.completion, page.semester
        course.faculty_code, course.department = page.faculty_code, page.department
        course.guarantor, course.language, course.web_url = (
            page.guarantor,
            page.language,
            page.web_url,
        )
        course.detail_fetched_at, course.detail_tries = now, 0
        result.changed += 1
    if errors:
        result.status = FetchStatus.ERROR if not result.changed else FetchStatus.OK
        result.error = "; ".join(errors)
    session.flush()
    return _record(session, result, now, now)


# --- ReCodEx ---


def _slot_course(session: Session, location: str | None, tz: ZoneInfo) -> int | None:
    """The course of a ReCodEx group named like "Programming 1 (Thu, 9:50, N8)": the one course
    with a lesson of this semester on that day, time and room. None when not exactly one."""
    match = _GROUP_SLOT_RE.search(location or "")
    if not match:
        return None
    day = _DAYS.get(match.group("day").strip().casefold())
    if day is None:
        return None
    semester = school.current_semester(session)
    if semester is None:
        return None
    room = match.group("room").strip().casefold()
    hour, minute = int(match.group("h")), int(match.group("m"))
    found = set()
    for lesson in session.scalars(
        select(Lesson).where(Lesson.ticket_code.like(f"{semester}%"), Lesson.course_id.is_not(None))
    ):
        local = lesson.start_utc.astimezone(tz)
        if (local.weekday(), local.hour, local.minute) == (day, hour, minute) and (
            (lesson.room or "").casefold() == room
        ):
            found.add(lesson.course_id)
    return found.pop() if len(found) == 1 else None


def sync_recodex_feed(
    session: Session, recodex: RecodexSource, config: SyncConfig, now: datetime
) -> SourceResult:
    source = FetchSource.RECODEX
    fetch = recodex.fetch_feed()
    if fetch.status is not FetchStatus.OK:
        return _record(session, SourceResult(source, fetch.status, error=fetch.error), now, now)
    digest = body_hash(fetch.body)
    if digest == _last_hash(session, source):
        return _record(session, SourceResult(source, FetchStatus.UNCHANGED), now, now, digest)
    try:
        events = parse_feed(fetch.body, floating_tz=UTC)  # floating, but really UTC
    except SisError as exc:
        return _record(session, SourceResult(source, FetchStatus.ERROR, error=str(exc)), now, now)
    if not events:
        return _record(session, SourceResult(source, FetchStatus.EMPTY), now, now, digest)
    items = [
        EventData(
            uid=e.uid,
            kind=SchoolEventKind.DEADLINE,
            title=clean_title(e.summary),
            start=e.start,
            location=e.location,
            url=e.url,
            course_id=_slot_course(session, e.location, config.tz),
            keep_course=True,  # the API's exact link wins over the slot match
        )
        for e in events
    ]
    changed = upsert_events(session, SchoolEventSource.RECODEX, items, now)
    return _record(
        session, SourceResult(source, FetchStatus.OK, len(items), changed), now, now, digest
    )


def _threshold(group: RecodexGroup) -> Decimal | None:
    if group.points_limit is not None:
        return group.points_limit
    if group.threshold is not None and group.stats is not None and group.stats.total:
        return (group.threshold * group.stats.total).quantize(Decimal("0.01"))
    return None


def _set_score(
    session: Session,
    course_id: int,
    source: ScoreSource,
    gained: Decimal,
    total: Decimal,
    threshold: Decimal | None,
    passes: bool | None,
    now: datetime,
) -> None:
    row = session.scalar(
        select(CourseScore).where(CourseScore.course_id == course_id, CourseScore.source == source)
    )
    if row is None:
        row = CourseScore(course_id=course_id, source=source, gained=gained, total=total)
        session.add(row)
    row.gained, row.total, row.pass_threshold, row.passes = gained, total, threshold, passes
    row.fetched_at = now


def sync_recodex_api(
    session: Session, recodex: RecodexSource, now: datetime
) -> SourceResult | None:
    """Exact course links, both deadlines, points and the course score from the read-only API.
    None without an API token."""
    claims = recodex.claims
    if claims is None:
        return None
    source = FetchSource.RECODEX_API
    try:
        groups = recodex.fetch_groups()
        assignments = {g.id: recodex.fetch_assignments(g.id) for g in groups}
    except RecodexDenied as exc:
        return _record(session, SourceResult(source, FetchStatus.DENIED, error=str(exc)), now, now)
    except SisError as exc:
        return _record(session, SourceResult(source, FetchStatus.ERROR, error=str(exc)), now, now)
    items = []
    totals: dict[int, list[RecodexGroup]] = {}
    for group in groups:
        course = (
            course_for_code(session, group.course_code, group.name) if group.course_code else None
        )
        if course is not None:
            totals.setdefault(course.id, []).append(group)
        for a in assignments[group.id]:
            if a.first_deadline is None:
                continue
            gained = group.stats.points.get(a.id) if group.stats else None
            items.append(
                EventData(
                    uid=f"recodex/{claims.user_id}/{a.id}",
                    kind=SchoolEventKind.DEADLINE,
                    title=a.name or "(assignment)",
                    start=a.first_deadline,
                    location=group.name,
                    url=recodex.assignment_url(a.id),
                    course_id=course.id if course else None,
                    fill_only=True,
                    points=gained if gained is not None else (Decimal(0) if group.stats else None),
                    max_points=a.max_points,
                    second_deadline=a.second_deadline,
                    set_points=True,
                )
            )
    changed = upsert_events(session, SchoolEventSource.RECODEX, items, now, cancel_missing=False)
    for course_id, found in totals.items():
        with_stats = [g for g in found if g.stats is not None]
        if not with_stats:
            continue
        thresholds = [_threshold(g) for g in with_stats]
        passes = [g.stats.passes for g in with_stats if g.stats]
        _set_score(
            session,
            course_id,
            ScoreSource.RECODEX,
            sum((g.stats.gained for g in with_stats if g.stats), Decimal(0)),
            sum((g.stats.total for g in with_stats if g.stats), Decimal(0)),
            thresholds[0] if len(thresholds) == 1 else None,
            all(p for p in passes if p is not None) if all(p is not None for p in passes) else None,
            now,
        )
    session.flush()
    return _record(session, SourceResult(source, FetchStatus.OK, len(items), changed), now, now)


# --- Owl ---


def _year_courses(session: Session, year: int, tz: ZoneInfo) -> list[Course]:
    lo, hi = school.local_bounds(date(year, 9, 1), date(year + 1, 8, 31), tz)
    ids = select(Lesson.course_id).where(
        Lesson.start_utc >= lo, Lesson.start_utc < hi, Lesson.course_id.is_not(None)
    )
    return list(session.scalars(select(Course).where(Course.id.in_(ids)).order_by(Course.id)))


def match_owl_course(
    session: Session, ref: OwlCourseRef, courses: Sequence[Course]
) -> Course | None:
    """The SIS course of an Owl course: exact for a ticket id; else the one course of this year
    with the same name (diacritics-insensitive), a teacher surname in the id breaking a tie.
    None when there is no single match: nothing is guessed."""
    code = ticket_course_code(ref.ident)
    if code is not None:
        return course_for_code(session, code, ref.name)
    key = school.name_key(ref.name)
    matches = [c for c in courses if school.name_key(c.name) == key]
    if len(matches) > 1:
        tokens = {normalize_text(t) for t in re.split(r"[-_.]", ref.ident) if len(t) >= 3}
        by_teacher = []
        for course in matches:
            teachers = " ".join(
                normalize_text(t or "")
                for t in session.scalars(
                    select(Lesson.teachers_text).where(Lesson.course_id == course.id)
                )
            )
            if any(token in teachers for token in tokens):
                by_teacher.append(course)
        matches = by_teacher
    return matches[0] if len(matches) == 1 else None


def _current_owl(ref: OwlCourseRef, year: int, names: set[str], kept: set[str]) -> bool:
    """Whether an Owl course belongs to this school year: its semester code, its ticket's year,
    a misfiled course whose name is one of this year's courses, or one I linked by hand."""
    yy = f"{year % 100:02d}{(year + 1) % 100:02d}"
    if ref.semester in (f"zs{yy}", f"ls{yy}") or ref.path in kept:
        return True
    ticket = ticket_year(ref.ident)
    if ticket is not None:
        return ticket == year
    return school.name_key(ref.name) in names


def _under(prefix: str) -> Callable[[SchoolEvent], bool]:
    def check(event: SchoolEvent) -> bool:
        return (event.uid or "").startswith(prefix)

    return check


def sync_owl(session: Session, owl: OwlSource, config: SyncConfig, now: datetime) -> SourceResult:
    source = FetchSource.OWL
    year = school.school_year(now.astimezone(config.tz).date())
    try:
        refs = owl.fetch_courses()
    except OwlDenied as exc:
        return _record(session, SourceResult(source, FetchStatus.DENIED, error=str(exc)), now, now)
    except SisError as exc:
        return _record(session, SourceResult(source, FetchStatus.ERROR, error=str(exc)), now, now)
    courses = _year_courses(session, year, config.tz)
    names = {school.name_key(c.name) for c in courses}
    rows = {o.path: o for o in session.scalars(select(OwlCourse))}
    kept = {p for p, o in rows.items() if o.link_manual and o.course_id is not None}
    result = SourceResult(source, FetchStatus.OK)
    errors = []
    for ref in refs:
        if not _current_owl(ref, year, names, kept):
            continue
        row = rows.get(ref.path)
        if row is None:
            row = OwlCourse(path=ref.path, name=ref.name, semester_label=ref.semester_label)
            session.add(row)
            rows[ref.path] = row
        row.name, row.semester_label = ref.name, ref.semester_label
        if not row.link_manual:
            matched = match_owl_course(session, ref, courses)
            row.course_id = matched.id if matched else None
        try:
            page = owl.fetch_course(ref)
        except OwlDenied as exc:
            session.flush()
            return _record(
                session, SourceResult(source, FetchStatus.DENIED, error=str(exc)), now, now
            )
        except SisError as exc:
            errors.append(f"{ref.path}: {exc}")
            continue
        row.description, row.fetched_at = page.description, now
        session.flush()
        prefix = f"{ref.path}/"
        items = [
            EventData(
                uid=f"{prefix}{topic.ident}",
                kind=SchoolEventKind.DEADLINE,
                title=topic.title,
                start=topic.deadline,
                description=topic.group,
                url=f"{config.owl_base_url}{topic.href}",
                course_id=row.course_id,
                points=topic.points,
                max_points=topic.max_points,
                set_points=True,
            )
            for topic in page.topics
            if topic.deadline is not None  # topics without one count only in the total
        ]
        result.seen += len(items)
        result.changed += upsert_events(
            session,
            SchoolEventSource.OWL,
            items,
            now,
            scope=_under(prefix),
            cancel_missing=bool(page.topics),
        )
        if row.course_id is not None and page.total_points is not None:
            threshold = page.threshold
            _set_score(
                session,
                row.course_id,
                ScoreSource.OWL,
                page.total_points,
                page.total_max or Decimal(0),
                threshold,
                page.total_points >= threshold if threshold is not None else None,
                now,
            )
    if errors:
        result.error = "; ".join(errors)
        if not result.seen:
            result.status = FetchStatus.ERROR
    session.flush()
    return _record(session, result, now, now)


# --- The whole run ---


SIS_PROBLEM = (
    "⚠️ SIS calendar feed stopped working",
    "Re-enable the export on SIS (Harmonogram → Export do kalendáře, or Rozvrh → Export) and "
    "update SIS_ICAL_TOKEN / SIS_TIMETABLE_KEY in .env.",
)
RECODEX_PROBLEM = (
    "⚠️ ReCodEx stopped answering",
    "The iCal or read-only token was rejected. Make a new one on your ReCodEx profile and update "
    "RECODEX_ICAL_TOKEN / RECODEX_API_TOKEN in .env.",
)
OWL_PROBLEM = (
    "⚠️ Owl login expired",
    "Copy a new owl_session cookie from your browser into OWL_SESSION_COOKIE in .env.",
)


def sync_school(
    session: Session,
    sources: SchoolSources,
    config: SyncConfig,
    *,
    sender: PushSender | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """One run over every configured source, sequentially. Never raises for a source problem:
    it is recorded, and pushed at most once a day."""
    now = now or utcnow()
    report = SyncReport()
    sis_problem = recodex_problem = owl_problem = False
    if sources.sis is not None:
        for feed in sources.sis.feeds:
            fetch = sources.sis.fetch_feed(feed)
            if feed in LESSON_FEEDS:
                result = sync_lessons(session, FetchSource(feed), fetch, config, now)
                if result.status is FetchStatus.EMPTY and future_lessons(session, now):
                    sis_problem = True
            else:
                result = sync_event_feed(session, feed, fetch, now)
            sis_problem |= result.status in (FetchStatus.DENIED, FetchStatus.ERROR)
            report.results.append(result)
        for extra in (
            enrich_courses(session, sources.sis, config, now),
            sync_harmonogram(session, sources.sis, config, now),
        ):
            if extra is not None:
                report.results.append(extra)
    if sources.recodex is not None:
        if sources.recodex.has_feed:
            result = sync_recodex_feed(session, sources.recodex, config, now)
            recodex_problem |= result.status is FetchStatus.DENIED
            report.results.append(result)
        api = sync_recodex_api(session, sources.recodex, now)
        if api is not None:
            recodex_problem |= api.status is FetchStatus.DENIED
            report.results.append(api)
    if sources.owl is not None:
        result = sync_owl(session, sources.owl, config, now)
        owl_problem = result.status is FetchStatus.DENIED
        report.results.append(result)

    for flag, key, (title, body) in (
        (sis_problem, "school:feed", SIS_PROBLEM),
        (recodex_problem, "school:recodex", RECODEX_PROBLEM),
        (owl_problem, "school:owl", OWL_PROBLEM),
    ):
        if flag:
            report.pushes += _problem_push(
                session, sender, config, key=key, title=title, body=body, now=now
            )
    session.flush()
    return report


def lesson_sources(sources: SchoolSources) -> list[FetchSource]:
    """Every fetch source this setup reads, for the status page."""
    found: list[FetchSource] = []
    if sources.sis is not None:
        found += [FetchSource(feed) for feed in sources.sis.feeds]
        if sources.sis.feeds:
            found += [FetchSource.COURSE, FetchSource.HARMONOGRAM]
    if sources.recodex is not None:
        if sources.recodex.has_feed:
            found.append(FetchSource.RECODEX)
        if sources.recodex.claims is not None:
            found.append(FetchSource.RECODEX_API)
    if sources.owl is not None:
        found.append(FetchSource.OWL)
    return found
