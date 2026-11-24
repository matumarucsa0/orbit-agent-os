"""School (SPEC §2.20): courses, lessons from the SIS timetable feed, exams and deadlines from
SIS, ReCodEx, Owl and by hand, and a log of every fetch.

Lessons are not meetings: they have no people, briefs or capture prompts. Nothing here is ever
deleted by a sync: a lesson or event that vanished from its feed is marked `cancelled` and comes
back if it reappears.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, TimestampMixin, enum_column, utcnow
from orbit.domain.enums import (
    FactTopic,
    FetchSource,
    FetchStatus,
    LessonKind,
    ResourceKind,
    ResourceOrigin,
    ResourceStatus,
    SchoolEventKind,
    SchoolEventSource,
    ScoreSource,
    Semester,
)

POINTS = Numeric(8, 2)


class Course(TimestampMixin, Base):
    """A SIS course by its code ("NINF030"). Made from the first lesson that names it; the details
    come from the public course page (at most once per SIS_COURSE_TTL_DAYS)."""

    __tablename__ = "course"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    name_en: Mapped[str | None] = mapped_column(String(300))
    credits: Mapped[int | None]
    hours_lecture: Mapped[int | None]  # per week
    hours_seminar: Mapped[int | None]
    completion: Mapped[str | None] = mapped_column(String(20))  # "Z", "Zk", "Z+Zk", "KZ"
    semester: Mapped[Semester | None] = mapped_column(enum_column(Semester, "course_semester"))
    faculty_code: Mapped[str | None] = mapped_column(String(10))
    department: Mapped[str | None] = mapped_column(String(300))
    guarantor: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(200))
    web_url: Mapped[str | None] = mapped_column(String(1000))
    detail_fetched_at: Mapped[datetime | None]
    detail_failed_at: Mapped[datetime | None]
    # Failed course-page fetches on the day of `detail_failed_at` (at most 3 a day).
    detail_tries: Mapped[int] = mapped_column(default=0)


class Lesson(TimestampMixin, Base):
    """One lesson (one date of a timetable ticket), upserted by `uid`."""

    __tablename__ = "lesson"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "<ticket>_<julianDay>" (the feed UID without its source prefix and "@..." suffix), so the
    # lesson is the same row whichever feed delivered it. A UID that doesn't parse is kept whole.
    uid: Mapped[str] = mapped_column(String(300), unique=True)
    ticket_code: Mapped[str | None] = mapped_column(String(40), index=True)
    kind: Mapped[LessonKind] = mapped_column(enum_column(LessonKind, "lesson_kind"))
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("course.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    teachers_text: Mapped[str | None] = mapped_column(Text)
    room: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(Text)
    start_utc: Mapped[datetime] = mapped_column(index=True)
    end_utc: Mapped[datetime]
    cancelled: Mapped[bool] = mapped_column(default=False)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    # The last change a sync saw (moved, new room, added, cancelled, back on), for the
    # "schedule changed" push; `change_note` says what it was ("moved from Thu 1 Oct 12:20").
    changed_at: Mapped[datetime | None] = mapped_column(index=True)
    change_note: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    course: Mapped[Course | None] = relationship()


class SchoolEvent(TimestampMixin, Base):
    """An exam, deadline, term or other dated school item. Feed rows are upserted by
    (source, uid); manual rows have no uid and are only changed by me."""

    __tablename__ = "school_event"
    __table_args__ = (UniqueConstraint("source", "uid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[SchoolEventSource] = mapped_column(
        enum_column(SchoolEventSource, "school_event_source"), index=True
    )
    uid: Mapped[str | None] = mapped_column(String(300))
    kind: Mapped[SchoolEventKind] = mapped_column(enum_column(SchoolEventKind, "school_event_kind"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("course.id", ondelete="SET NULL"), index=True
    )
    start_utc: Mapped[datetime] = mapped_column(index=True)
    end_utc: Mapped[datetime | None]
    second_deadline_utc: Mapped[datetime | None]
    points: Mapped[Decimal | None] = mapped_column(POINTS)  # gained (ReCodEx, Owl)
    max_points: Mapped[Decimal | None] = mapped_column(POINTS)
    all_day: Mapped[bool] = mapped_column(default=False)
    cancelled: Mapped[bool] = mapped_column(default=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    course: Mapped[Course | None] = relationship()


class CourseScore(Base):
    """Points toward a course's credit, as ReCodEx or Owl reports them (one row per source)."""

    __tablename__ = "course_score"
    __table_args__ = (UniqueConstraint("course_id", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"))
    source: Mapped[ScoreSource] = mapped_column(enum_column(ScoreSource, "score_source"))
    gained: Mapped[Decimal] = mapped_column(POINTS)
    total: Mapped[Decimal] = mapped_column(POINTS)
    pass_threshold: Mapped[Decimal | None] = mapped_column(POINTS)
    passes: Mapped[bool | None]
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)

    course: Mapped[Course] = relationship()


class OwlCourse(TimestampMixin, Base):
    """A course on the Postal Owl ("zs2627/26aNMAT202x06") and the SIS course it belongs to.
    The link is exact for a ticket id, else by name; a link I pick by hand (`link_manual`) is
    never changed by a sync."""

    __tablename__ = "owl_course"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(300), unique=True)  # "<sem>/<ident>"
    name: Mapped[str] = mapped_column(String(300))
    semester_label: Mapped[str | None] = mapped_column(String(100))
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("course.id", ondelete="SET NULL"), index=True
    )
    link_manual: Mapped[bool] = mapped_column(default=False)
    # The course rules text from the course page (grading, deadlines), for context.
    description: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None]

    course: Mapped[Course | None] = relationship()


class SisFetch(Base):
    """One fetch of one source. Never holds a token: `error` is redacted before it is stored."""

    __tablename__ = "sis_fetch"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[FetchSource] = mapped_column(enum_column(FetchSource, "fetch_source"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None]
    status: Mapped[FetchStatus] = mapped_column(enum_column(FetchStatus, "fetch_status"))
    items_seen: Mapped[int] = mapped_column(default=0)
    items_changed: Mapped[int] = mapped_column(default=0)
    body_hash: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class SchoolResource(TimestampMixin, Base):
    """A page, PDF, repository or note that belongs to a course (Phase 18c). Orbit fetches and
    extracts the text; Claude only reads the text. Anything not added by hand starts as a
    suggestion (`kept` false) and is fetched only after I keep it."""

    __tablename__ = "school_resource"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(
        ForeignKey("course.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[ResourceKind] = mapped_column(enum_column(ResourceKind, "resource_kind"))
    origin: Mapped[ResourceOrigin] = mapped_column(enum_column(ResourceOrigin, "resource_origin"))
    url: Mapped[str | None] = mapped_column(String(1000))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(300))
    kept: Mapped[bool] = mapped_column(default=True)
    # A suggestion I turned down: never suggested again.
    dismissed: Mapped[bool] = mapped_column(default=False)
    status: Mapped[ResourceStatus] = mapped_column(
        enum_column(ResourceStatus, "resource_status"), default=ResourceStatus.OK
    )
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str | None] = mapped_column(Text)
    prev_text: Mapped[str | None] = mapped_column(Text)
    last_diff: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None]
    changed_at: Mapped[datetime | None]
    read_at: Mapped[datetime | None]
    fail_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    suggested_reason: Mapped[str | None] = mapped_column(String(500))

    course: Mapped[Course | None] = relationship()


class CourseFact(Base):
    """A rule about a course ("credit: 120 of 240 points") from a confirmed `course_fact`
    proposal, shown on the course page and given to the agents."""

    __tablename__ = "course_fact"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    topic: Mapped[FactTopic] = mapped_column(enum_column(FactTopic, "fact_topic"))
    text: Mapped[str] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    resource_id: Mapped[int | None] = mapped_column(
        ForeignKey("school_resource.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    course: Mapped[Course] = relationship()
