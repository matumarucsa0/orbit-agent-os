"""The `school` context provider and its place in the snapshot (SPEC §2.20)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from orbit.domain.enums import LessonKind, SchoolEventKind, SchoolEventSource, ScoreSource
from orbit.domain.school import Course, CourseScore, Lesson, OwlCourse, SchoolEvent
from orbit.services.context import ContextFocus, ContextWindow, build_context
from orbit.services.context import school as provider_mod
from orbit.services.context.school import SchoolProvider

TZ = ZoneInfo("Europe/Prague")
WINDOW = ContextWindow(date(2026, 10, 5), date(2026, 10, 11), TZ)


def at(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), TZ).astimezone(UTC)


@pytest.fixture
def world(db: Session) -> dict[str, Course]:
    algo = Course(code="NINF062", name="Algoritmizace", credits=4, completion="Z+Zk")
    la = Course(code="NMAT101", name="Lineární algebra 1")
    db.add_all([algo, la])
    db.flush()
    for i, (course, day, hh) in enumerate(
        [(algo, date(2026, 10, 5), 9), (algo, date(2026, 10, 8), 9), (la, date(2026, 10, 8), 12)]
    ):
        db.add(
            Lesson(
                uid=f"26a{course.code}x01_{i}",
                ticket_code=f"26a{course.code}x01",
                kind=LessonKind.SEMINAR,
                course_id=course.id,
                title=course.name,
                teachers_text="Teacher Name, call +421 905 123 456",
                room="N8",
                start_utc=at(day, hh),
                end_utc=at(day, hh) + timedelta(minutes=90),
            )
        )
    db.add(
        Lesson(
            uid="cancelled",
            ticket_code="26aNMAT101x01",
            kind=LessonKind.SEMINAR,
            course_id=la.id,
            title=la.name,
            start_utc=at(date(2026, 10, 6), 9),
            end_utc=at(date(2026, 10, 6), 10),
            cancelled=True,
            changed_at=at(date(2026, 10, 1), 9),
        )
    )
    db.add_all(
        [
            SchoolEvent(
                source=SchoolEventSource.MANUAL,
                kind=SchoolEventKind.EXAM,
                title="Písemka",
                course_id=la.id,
                start_utc=at(date(2026, 10, 9), 9),
            ),
            SchoolEvent(
                source=SchoolEventSource.RECODEX,
                uid="recodex/u/a",
                kind=SchoolEventKind.DEADLINE,
                title="Hello World",
                course_id=algo.id,
                start_utc=at(date(2026, 11, 20), 23, 59),
                points=Decimal(0),
                max_points=Decimal(1),
                url="https://recodex/x?token=secret",
            ),
            SchoolEvent(
                source=SchoolEventSource.MANUAL,
                kind=SchoolEventKind.DEADLINE,
                title="Too far",
                start_utc=at(date(2027, 3, 1), 9),
            ),
        ]
    )
    db.add(
        CourseScore(
            course_id=algo.id,
            source=ScoreSource.RECODEX,
            gained=Decimal(2),
            total=Decimal(10),
            pass_threshold=Decimal(7),
            passes=False,
        )
    )
    db.add(OwlCourse(path="zs2627/la1-x", name="LA", course_id=la.id, description="Zápočet: 60 %"))
    db.flush()
    return {"algo": algo, "la": la}


def test_provider_for_a_fixed_week(db: Session, world: dict[str, Course]) -> None:
    ctx = SchoolProvider().build(db, WINDOW)
    assert ctx.module == "school" and not ctx.truncated
    assert [(ls.title, ls.start.isoformat()) for ls in ctx.lessons] == [
        ("Algoritmizace", "2026-10-05T09:00:00+02:00"),
        ("Algoritmizace", "2026-10-08T09:00:00+02:00"),
        ("Lineární algebra 1", "2026-10-08T12:00:00+02:00"),
    ]
    assert [ls.id for ls in ctx.cancelled_lessons] and ctx.cancelled_lessons[0].room is None
    items = {i.title: i for i in ctx.exams_and_deadlines}
    assert set(items) == {"Písemka", "Hello World"}  # "Too far" is past the 60 days
    assert (items["Písemka"].days_until, items["Písemka"].in_window) == (4, True)
    assert items["Hello World"].in_window is False and items["Hello World"].max_points == 1
    load = ctx.workload
    assert load is not None
    assert [d.lesson_minutes for d in load.days] == [90, 0, 0, 180, 0, 0, 0]
    assert (load.days[3].first_start, load.days[3].last_end) == ("9:00", "13:30")
    assert load.lesson_hours_in_window == 4.5
    assert (load.exams_next_14_days, load.deadlines_next_14_days) == (1, 0)
    courses = {c.code: c for c in ctx.courses}
    assert set(courses) == {"NINF062", "NMAT101"}
    assert courses["NINF062"].scores[0].pass_threshold == 7
    assert courses["NMAT101"].rules == ["Zápočet: 60 %"]
    assert (ctx.credits_total, ctx.credits_missing) == (4, 1)


def test_provider_masks_and_has_no_urls(db: Session, world: dict[str, Course]) -> None:
    blob = SchoolProvider().build(db, WINDOW).model_dump_json()
    assert "905 123 456" not in blob and "+421" not in blob
    assert "token=secret" not in blob and "recodex/u/a" not in blob


def test_person_focus_is_empty(db: Session, world: dict[str, Course]) -> None:
    ctx = SchoolProvider().build(db, WINDOW, ContextFocus(person_id=1))
    assert ctx.lessons == [] and ctx.courses == [] and ctx.workload is None


def test_caps(db: Session, world: dict[str, Course], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_mod, "MAX_LESSONS", 2)
    ctx = SchoolProvider().build(db, WINDOW)
    assert len(ctx.lessons) == 2 and ctx.truncated


def test_snapshot_has_school_after_goals(db: Session, world: dict[str, Course]) -> None:
    snap = build_context(db, WINDOW).model_dump(mode="json")
    modules = [k for k, v in snap.items() if isinstance(v, dict) and "module" in v]
    assert modules[:2] == ["goals", "school"]


def test_budget_trims_lessons_before_exams(db: Session, world: dict[str, Course]) -> None:
    algo = world["algo"]
    for i in range(60):
        start = at(date(2026, 10, 5) + timedelta(days=i % 7), 7 + i % 10)
        db.add(
            Lesson(
                uid=f"extra{i}",
                ticket_code="26aNINF062p1",
                kind=LessonKind.LECTURE,
                course_id=algo.id,
                title=f"Algoritmizace lecture {i} " + "x" * 40,
                start_utc=start,
                end_utc=start + timedelta(minutes=45),
            )
        )
    db.flush()
    full = build_context(db, WINDOW).model_dump(mode="json")
    used = full["budget"]["used_chars"]
    data = build_context(db, WINDOW, budget_chars=used - 3000).model_dump(mode="json")
    assert set(data["budget"]["trimmed"]) == {"school"}
    school = data["school"]
    assert len(school["lessons"]) < len(full["school"]["lessons"])
    assert school["exams_and_deadlines"] == full["school"]["exams_and_deadlines"]
    assert school["truncated"] is True
    assert len(json.dumps(data["school"])) < len(json.dumps(full["school"]))
