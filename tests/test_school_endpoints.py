"""School API, web pages, export and scheduler wiring (SPEC §2.20). Sources are fakes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from orbit.app import build_school_sources
from orbit.domain.enums import SchoolEventKind, SchoolEventSource, ScoreSource
from orbit.domain.school import Course, CourseScore, Lesson, OwlCourse, SchoolEvent
from orbit.jobs.scheduler import school_pushes, sis_sync, start_scheduler
from orbit.services import export
from orbit.services.school_sync import SchoolSources
from tests.conftest import TEST_PASSWORD
from tests.school_helpers import ROZVRHNG, FakeSis, ok

SECRET = "sis-token-" + "s" * 30


def seed(app: FastAPI) -> dict[str, int]:
    now = datetime.now(UTC)
    with app.state.sessionmaker() as db:
        course = Course(code="NINF062", name="Algoritmizace", credits=4)
        db.add(course)
        db.flush()
        lesson = Lesson(
            uid="26aNINF062x03_1",
            ticket_code="26aNINF062x03",
            kind="seminar",
            course_id=course.id,
            title="Algoritmizace",
            room="N8",
            start_utc=now + timedelta(hours=2),
            end_utc=now + timedelta(hours=3, minutes=30),
        )
        feed = SchoolEvent(
            source=SchoolEventSource.RECODEX,
            uid="recodex/u/a",
            kind=SchoolEventKind.DEADLINE,
            title="Hello World",
            course_id=course.id,
            start_utc=now + timedelta(days=3),
            points=Decimal("0.50"),
            max_points=Decimal(1),
        )
        db.add_all([lesson, feed])
        db.add(
            CourseScore(
                course_id=course.id,
                source=ScoreSource.RECODEX,
                gained=Decimal(2),
                total=Decimal(10),
            )
        )
        db.add(OwlCourse(path="ls2526/la1-svobodova", name="Lineární algebra 1"))
        db.commit()
        return {"course": course.id, "lesson": lesson.id, "feed": feed.id}


# --- API ---


def test_api_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/school/lessons").status_code == 401


def test_api_reads(api: TestClient, app: FastAPI) -> None:
    ids = seed(app)
    now = datetime.now(UTC)
    lessons = api.get(
        "/api/v1/school/lessons",
        params={"start": now.isoformat(), "end": (now + timedelta(days=1)).isoformat()},
    ).json()
    assert [(x["id"], x["course"]["code"], x["kind"]) for x in lessons] == [
        (ids["lesson"], "NINF062", "seminar")
    ]
    courses = api.get("/api/v1/school/courses").json()
    assert [c["code"] for c in courses] == ["NINF062"]
    assert courses[0]["sis_url"].endswith("do=predmet&kod=NINF062&skr=2026")
    assert courses[0]["scores"][0]["gained"] == 2
    detail = api.get("/api/v1/school/courses/ninf062").json()
    assert detail["course"]["credits"] == 4 and len(detail["events"]) == 1
    assert api.get("/api/v1/school/courses/NOPE001").status_code == 404
    events = api.get("/api/v1/school/events").json()
    assert [(e["title"], e["points"], e["max_points"]) for e in events] == [
        ("Hello World", 0.5, 1.0)
    ]


def test_api_manual_events(api: TestClient, app: FastAPI) -> None:
    ids = seed(app)
    created = api.post(
        "/api/v1/school/events",
        json={"title": "Úkol 2", "day": "2026-10-20", "at": "23:59", "course_code": "NINF062"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["start_utc"].startswith("2026-10-20T21:59")
    assert (body["source"], body["kind"], body["course"]["code"]) == (
        "manual",
        "deadline",
        "NINF062",
    )
    patched = api.patch(f"/api/v1/school/events/{body['id']}", json={"at": None, "kind": "exam"})
    assert patched.status_code == 200
    assert patched.json()["all_day"] is True and patched.json()["kind"] == "exam"
    assert patched.json()["title"] == "Úkol 2"
    assert api.patch(f"/api/v1/school/events/{ids['feed']}", json={"title": "x"}).status_code == 422
    assert api.delete(f"/api/v1/school/events/{ids['feed']}").status_code == 422
    assert (
        api.post(
            "/api/v1/school/events", json={"title": "x", "day": "2026-10-20", "kind": "term"}
        ).status_code
        == 422
    )
    assert api.delete(f"/api/v1/school/events/{body['id']}").status_code == 204
    assert api.delete(f"/api/v1/school/events/{body['id']}").status_code == 404


def test_api_sync_and_status(api: TestClient, app: FastAPI) -> None:
    assert api.post("/api/v1/school/sync").status_code == 503
    app.state.school = SchoolSources(sis=FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    result = api.post("/api/v1/school/sync").json()
    assert result["results"][0] == {
        "source": "rozvrhng",
        "status": "ok",
        "seen": 11,
        "changed": 11,
        "error": None,
    }
    status = api.get("/api/v1/school/status").json()
    assert status["sis"] is True and status["owl"] is False
    feeds = {f["source"]: f for f in status["feeds"]}
    assert feeds["rozvrhng"]["last"]["items_seen"] == 11


def test_status_never_shows_a_token(make_settings) -> None:  # type: ignore[no-untyped-def]
    from orbit.app import create_app
    from tests.conftest import AUTH, reset_schema

    app = create_app(make_settings(sis_ical_token=SECRET, owl_session_cookie="c" * 40))
    reset_schema(app.state.engine)
    with TestClient(app) as api:
        api.headers.update(AUTH)
        body = api.get("/api/v1/school/status").text
        assert SECRET not in body and "c" * 40 not in body
        api.post("/login", data={"password": TEST_PASSWORD})
        page = api.get("/school/status").text
        assert SECRET not in page and "c" * 40 not in page
        assert "rozvrhng, term_st2, stev" in page


def test_build_school_sources(make_settings) -> None:  # type: ignore[no-untyped-def]
    sources, client = build_school_sources(make_settings())
    assert not sources.configured and client is None
    sources, client = build_school_sources(
        make_settings(sis_ical_token=SECRET, sis_timetable_key="k" * 40)
    )
    assert client is not None and sources.sis is not None and sources.owl is None
    assert sources.sis.feeds == ["roz_externi", "term_st2", "stev"]
    assert client.redact(f"x {SECRET} y") == "x [redacted] y"
    client.close()


# --- Web ---


def test_pages_need_login(client: TestClient) -> None:
    for path in ("/school", "/school/upcoming", "/school/courses", "/school/status"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login")


def test_week_page_and_navigation(web: TestClient, app: FastAPI) -> None:
    page = web.get("/school")
    assert page.status_code == 200
    assert "SIS feed not set up" in page.text
    seed(app)
    page = web.get("/school")
    assert "Algoritmizace" in page.text and "N8" in page.text
    assert "SIS feed not set up" not in page.text
    later = web.get("/school?week=2027-01-04")
    assert "Nothing this week." in later.text and "This week" in later.text
    assert "4 Jan 2027" in later.text
    assert web.get("/school?week=nonsense").status_code == 200


def test_add_and_delete_a_manual_deadline(web: TestClient, app: FastAPI) -> None:
    seed(app)
    day = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    response = web.post(
        "/school/events",
        data={"title": "Domácí úkol", "day": day, "kind": "deadline", "at": "", "course_id": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = web.get("/school/upcoming")
    assert "Domácí úkol" in page.text and "in 5 days" in page.text
    with app.state.sessionmaker() as db:
        event = db.scalars(select(SchoolEvent).where(SchoolEvent.title == "Domácí úkol")).one()
        feed = db.scalars(
            select(SchoolEvent).where(SchoolEvent.source == SchoolEventSource.RECODEX)
        ).one()
    web.post(f"/school/events/{event.id}/delete")
    web.post(f"/school/events/{feed.id}/delete")  # refused: not added by hand
    with app.state.sessionmaker() as db:
        assert db.get(SchoolEvent, event.id) is None
        assert db.get(SchoolEvent, feed.id) is not None
    bad = web.post(
        "/school/events", data={"title": "x", "day": "not a date"}, follow_redirects=False
    )
    assert bad.status_code == 303


def test_courses_course_and_status_pages(web: TestClient, app: FastAPI) -> None:
    ids = seed(app)
    courses = web.get("/school/courses").text
    assert "Algoritmizace" in courses and "4 credits" in courses
    course = web.get("/school/courses/NINF062").text
    assert "SIS course page" in course and "Hello World" in course
    assert "0.5/1 points" in course
    assert web.get("/school/courses/NOPE001").status_code == 404
    status = web.get("/school/status").text
    assert "Lineární algebra 1" in status and "Not one of my courses" in status
    with app.state.sessionmaker() as db:
        owl = db.scalars(select(OwlCourse)).one()
    web.post(f"/school/owl/{owl.id}/link", data={"course_id": str(ids["course"])})
    with app.state.sessionmaker() as db:
        owl = db.scalars(select(OwlCourse)).one()
        assert owl.course_id == ids["course"] and owl.link_manual


def test_sync_now_button(web: TestClient, app: FastAPI) -> None:
    app.state.school = SchoolSources(sis=FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    response = web.post("/school/sync", follow_redirects=False)
    assert response.status_code == 303
    page = web.get("/school/status").text
    assert "Timetable (ical.php)" in page and "11 items" in page


def test_home_strip_and_nav(web: TestClient, app: FastAPI) -> None:
    assert "🎓 School" not in web.get("/").text
    seed(app)
    home = web.get("/").text
    assert "🎓 School" in home and "Algoritmizace" in home and "due in 3 days: Hello World" in home
    assert 'href="/school"' in home


# --- Export and scheduler ---


def test_export_has_school_tables(app: FastAPI) -> None:
    seed(app)
    with app.state.sessionmaker() as db:
        data = export.export_data(db)
    for table in ("course", "lesson", "school_event", "course_score", "owl_course", "sis_fetch"):
        assert table in data["tables"]
    (event,) = data["tables"]["school_event"]
    assert event["points"] == "0.50"
    json.loads(export.to_json(data))


def test_scheduler_jobs(make_settings) -> None:  # type: ignore[no-untyped-def]
    from orbit.db import make_engine, make_sessionmaker

    engine = make_engine("sqlite://")
    settings = make_settings()
    sources = SchoolSources(sis=FakeSis({"rozvrhng": ok(ROZVRHNG)}))
    scheduler = start_scheduler(make_sessionmaker(engine), None, settings, school=sources)
    try:
        assert {"sis_sync", "school_pushes"} <= {job.id for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)
    scheduler = start_scheduler(make_sessionmaker(engine), None, settings, school=SchoolSources())
    try:
        assert "sis_sync" not in {job.id for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)
        engine.dispose()


@pytest.mark.parametrize("broken", [False, True])
def test_sync_and_push_jobs_commit(make_settings, broken: bool) -> None:  # type: ignore[no-untyped-def]
    from orbit.db import make_engine, make_sessionmaker
    from orbit.domain import Base

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    settings = make_settings()

    def explode() -> None:
        raise RuntimeError("bug")

    answer = explode if broken else ok(ROZVRHNG)
    report = sis_sync(sessions, SchoolSources(sis=FakeSis({"rozvrhng": answer})), None, settings)
    with sessions() as db:
        assert (db.scalar(select(Lesson.id).limit(1)) is None) is broken
    assert (report is None) is broken
    assert school_pushes(sessions, None, settings, SchoolSources()) >= 0
    engine.dispose()
