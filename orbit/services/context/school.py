"""School: lessons, exams, deadlines and the workload of the window, and this semester's courses.

The time and workload side of the week (SPEC §2.20). No SIS tokens, ids or URLs are in it; teacher
names are public catalogue text. A person or goal focus returns an empty context (nothing in
school links to people or goals yet). The budget layer trims lessons before exams and deadlines.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.enums import LessonKind, SchoolEventKind, SchoolEventSource, ScoreSource
from orbit.domain.school import (
    Course,
    CourseFact,
    Lesson,
    OwlCourse,
    SchoolEvent,
    SchoolResource,
)
from orbit.services import school
from orbit.services.context.base import (
    NAME_CHARS,
    ContextFocus,
    ContextWindow,
    ModuleContext,
    text,
)

MAX_LESSONS = 300
MAX_CANCELLED = 100
MAX_EVENTS = 100
MAX_COURSES = 40
AHEAD_DAYS = 60  # exams and deadlines this far past the window end
PAST_DAYS = 14  # passed (and cancelled) items this recent are still shown
SOON_DAYS = 14


class CourseRef(BaseModel):
    code: str
    name: str


class LessonContext(BaseModel):
    id: int
    course: CourseRef | None
    kind: LessonKind
    title: str
    start: datetime  # local time
    end: datetime
    room: str | None
    teachers: str | None


class SchoolItemContext(BaseModel):
    id: int
    kind: SchoolEventKind
    source: SchoolEventSource
    title: str
    course: CourseRef | None
    start: datetime  # local time
    all_day: bool
    days_until: int  # from the window start; negative = passed
    in_window: bool
    location: str | None
    note: str | None
    points: float | None
    max_points: float | None
    second_deadline: datetime | None


class DayLoad(BaseModel):
    date: date
    lesson_minutes: int
    first_start: str | None  # "9:00"
    last_end: str | None


class WorkloadContext(BaseModel):
    days: list[DayLoad]
    lesson_hours_in_window: float
    exams_next_14_days: int
    deadlines_next_14_days: int


class ScoreContext(BaseModel):
    source: ScoreSource
    gained: float
    total: float
    pass_threshold: float | None
    passes: bool | None


class ResourcesContext(BaseModel):
    kept: int
    last_change: datetime | None  # local time of the newest change in a kept resource


class CourseContext(BaseModel):
    code: str
    name: str
    credits: int | None
    completion: str | None  # "Z" credit, "Zk" exam, "Z+Zk" both
    hours_lecture: int | None
    hours_seminar: int | None
    next_lesson: datetime | None  # local time
    scores: list[ScoreContext]
    rules: list[str]  # course rules text from its Owl page(s)
    facts: list[str] = Field(default_factory=list)  # confirmed rules, "grading: 120 of 240 points"
    resources: ResourcesContext | None = None  # the pages and files kept for the course


class SchoolContext(ModuleContext):
    trimmable: ClassVar[tuple[str, ...]] = (
        "lessons",
        "cancelled_lessons",
        "courses",
        "exams_and_deadlines",
    )
    trim_in_order: ClassVar[bool] = True
    module: Literal["school"] = "school"
    lessons: list[LessonContext] = Field(default_factory=list)
    cancelled_lessons: list[LessonContext] = Field(default_factory=list)
    exams_and_deadlines: list[SchoolItemContext] = Field(default_factory=list)
    workload: WorkloadContext | None = None
    courses: list[CourseContext] = Field(default_factory=list)
    credits_total: int = 0
    credits_missing: int = 0  # courses whose credits aren't known


def _ref(course: Course | None) -> CourseRef | None:
    if course is None:
        return None
    return CourseRef(code=course.code, name=text(course.name, NAME_CHARS))


def _lesson(lesson: Lesson, window: ContextWindow) -> LessonContext:
    return LessonContext(
        id=lesson.id,
        course=_ref(lesson.course),
        kind=lesson.kind,
        title=text(lesson.title, NAME_CHARS),
        start=lesson.start_utc.astimezone(window.tz),
        end=lesson.end_utc.astimezone(window.tz),
        room=text(lesson.room, NAME_CHARS) if lesson.room else None,
        teachers=text(lesson.teachers_text) if lesson.teachers_text else None,
    )


def _item(event: SchoolEvent, window: ContextWindow) -> SchoolItemContext:
    local = event.start_utc.astimezone(window.tz)
    return SchoolItemContext(
        id=event.id,
        kind=event.kind,
        source=event.source,
        title=text(event.title, NAME_CHARS),
        course=_ref(event.course),
        start=local,
        all_day=event.all_day,
        days_until=(local.date() - window.start).days,
        in_window=window.contains(local.date()),
        location=text(event.location, NAME_CHARS) if event.location else None,
        note=text(event.description) if event.description else None,
        points=float(event.points) if event.points is not None else None,
        max_points=float(event.max_points) if event.max_points is not None else None,
        second_deadline=(
            event.second_deadline_utc.astimezone(window.tz) if event.second_deadline_utc else None
        ),
    )


def _workload(
    lessons: list[Lesson], items: list[SchoolItemContext], window: ContextWindow
) -> WorkloadContext:
    days = []
    total = 0
    for offset in range(window.days + 1):
        day = window.start + timedelta(days=offset)
        found = [ls for ls in lessons if ls.start_utc.astimezone(window.tz).date() == day]
        minutes = sum(int((ls.end_utc - ls.start_utc).total_seconds() // 60) for ls in found)
        total += minutes
        days.append(
            DayLoad(
                date=day,
                lesson_minutes=minutes,
                first_start=school.clock(min(ls.start_utc for ls in found), window.tz)
                if found
                else None,
                last_end=school.clock(max(ls.end_utc for ls in found), window.tz)
                if found
                else None,
            )
        )
    soon = [i for i in items if 0 <= i.days_until < SOON_DAYS]
    return WorkloadContext(
        days=days,
        lesson_hours_in_window=round(total / 60, 1),
        exams_next_14_days=sum(i.kind is SchoolEventKind.EXAM for i in soon),
        deadlines_next_14_days=sum(i.kind is SchoolEventKind.DEADLINE for i in soon),
    )


def _resources(found: list[SchoolResource], window: ContextWindow) -> ResourcesContext | None:
    if not found:
        return None
    changes = [r.changed_at for r in found if r.changed_at is not None]
    return ResourcesContext(
        kept=len(found),
        last_change=max(changes).astimezone(window.tz) if changes else None,
    )


def _courses(session: Session, window: ContextWindow) -> tuple[list[CourseContext], int, int]:
    courses = school.current_courses(session)
    upcoming = school.next_lessons(session, window.start_utc)
    scores = school.scores(session, [c.id for c in courses])
    rules: dict[int, list[str]] = {}
    for owl in session.scalars(select(OwlCourse).where(OwlCourse.course_id.is_not(None))):
        if owl.description and owl.course_id is not None:
            rules.setdefault(owl.course_id, []).append(text(owl.description))
    facts: dict[int, list[str]] = {}
    for fact in session.scalars(select(CourseFact).order_by(CourseFact.id)):
        facts.setdefault(fact.course_id, []).append(f"{fact.topic.value}: {text(fact.text)}")
    kept: dict[int, list[SchoolResource]] = {}
    for resource in session.scalars(
        select(SchoolResource).where(
            SchoolResource.kept.is_(True), SchoolResource.course_id.is_not(None)
        )
    ):
        assert resource.course_id is not None
        kept.setdefault(resource.course_id, []).append(resource)
    found = []
    for course in courses[:MAX_COURSES]:
        nxt = upcoming.get(course.id)
        found.append(
            CourseContext(
                code=course.code,
                name=text(course.name, NAME_CHARS),
                credits=course.credits,
                completion=course.completion,
                hours_lecture=course.hours_lecture,
                hours_seminar=course.hours_seminar,
                next_lesson=nxt.start_utc.astimezone(window.tz) if nxt else None,
                scores=[
                    ScoreContext(
                        source=s.source,
                        gained=float(s.gained),
                        total=float(s.total),
                        pass_threshold=float(s.pass_threshold) if s.pass_threshold else None,
                        passes=s.passes,
                    )
                    for s in scores.get(course.id, [])
                ],
                rules=rules.get(course.id, []),
                facts=facts.get(course.id, []),
                resources=_resources(kept.get(course.id, []), window),
            )
        )
    known = sum(c.credits or 0 for c in courses)
    missing = sum(c.credits is None for c in courses)
    return found, known, missing


class SchoolProvider:
    name = "school"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> SchoolContext:
        if focus is not None and focus.person_id is not None:
            return SchoolContext()
        start, end = window.start_utc, window.end_utc
        lessons = list(
            session.scalars(
                select(Lesson)
                .where(Lesson.start_utc >= start, Lesson.start_utc < end)
                .options(selectinload(Lesson.course))
                .order_by(Lesson.start_utc, Lesson.id)
            )
        )
        active = [ls for ls in lessons if not ls.cancelled]
        recent = start - timedelta(days=PAST_DAYS)
        cancelled = [
            ls
            for ls in lessons
            if ls.cancelled and ls.changed_at is not None and ls.changed_at >= recent
        ]
        events = school.list_events(
            session,
            recent,
            end + timedelta(days=AHEAD_DAYS),
            kinds=(SchoolEventKind.EXAM, SchoolEventKind.DEADLINE),
        )
        items = [_item(e, window) for e in events]
        courses, credits, missing = _courses(session, window)
        truncated = (
            len(active) > MAX_LESSONS
            or len(cancelled) > MAX_CANCELLED
            or len(items) > MAX_EVENTS
            or len(school.current_courses(session)) > MAX_COURSES
        )
        return SchoolContext(
            lessons=[_lesson(ls, window) for ls in active[:MAX_LESSONS]],
            cancelled_lessons=[_lesson(ls, window) for ls in cancelled[:MAX_CANCELLED]],
            exams_and_deadlines=items[:MAX_EVENTS],
            workload=_workload(active, items, window),
            courses=courses,
            credits_total=credits,
            credits_missing=missing,
            truncated=truncated,
        )
