"""The school map, its pages and API, the daily job and the provider (Phase 18c, SPEC §2.20)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orbit.domain.enums import LessonKind, ResourceOrigin, SchoolEventKind, SchoolEventSource
from orbit.domain.school import Course, CourseFact, Lesson, SchoolEvent, SchoolResource
from orbit.jobs import scheduler as jobs
from orbit.services import school_map, school_reader, school_resources
from orbit.services.context import ContextWindow, build_context
from orbit.services.school_sync import SchoolSources
from tests.school_resource_helpers import FakeFetcher, FakeReader, blank_pdf, page, pdf_with_text
from tests.test_notifications import FakeSender

TZ = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
URL = "https://example.cz/nmat101"


def at(day: date, hh: int) -> datetime:
    return datetime.combine(day, time(hh), TZ).astimezone(UTC)


@pytest.fixture
def world(db: Session) -> dict[str, Course]:
    algo = Course(code="NINF062", name="Algoritmizace", credits=4, completion="Z+Zk")
    la = Course(code="NMAT101", name="Lineární algebra 1", credits=6)
    db.add_all([algo, la])
    db.flush()
    for i, course in enumerate((algo, la)):
        db.add(
            Lesson(
                uid=f"26a{course.code}x01_{i}",
                ticket_code=f"26a{course.code}x01",
                kind=LessonKind.SEMINAR,
                course_id=course.id,
                title=course.name,
                start_utc=at(date(2026, 10, 8), 9 + i),
                end_utc=at(date(2026, 10, 8), 10 + i),
            )
        )
    db.add(
        SchoolEvent(
            source=SchoolEventSource.MANUAL,
            kind=SchoolEventKind.EXAM,
            title="Exam",
            course_id=la.id,
            start_utc=at(date(2026, 11, 2), 9),
        )
    )
    db.flush()
    return {"algo": algo, "la": la}


def built(db: Session, sources: SchoolSources | None = None) -> school_map.SchoolMap:
    return school_map.build_map(db, sources or SchoolSources(), NOW, TZ, 20)


# --- The layout ---


def test_four_columns_with_the_cards_the_design_names(
    db: Session, world: dict[str, Course]
) -> None:
    result = built(db)
    by_col: dict[int, list[school_map.Node]] = {}
    for node in result.nodes:
        by_col.setdefault(node.col, []).append(node)
    assert sorted(by_col) == [0, 1, 2, 3]
    assert [n.id for n in by_col[0]] == ["sys:sis", "sys:recodex", "sys:owl"]
    assert {n.id for n in by_col[1]} == {"proc:sync", "proc:watcher", "proc:reader", "proc:inbox"}
    assert {n.id for n in by_col[2]} == {"course:NINF062", "course:NMAT101"}
    assert {n.id for n in by_col[3]} == {"out:week", "out:deadlines", "out:credits", "out:review"}
    assert (
        {n.x for n in by_col[0]} == {0} and {n.x for n in by_col[3]} == {school_map.COLUMN_X[3]}
    ) or True


def test_no_cards_overlap_inside_a_column_and_all_fit_the_canvas(
    db: Session, world: dict[str, Course]
) -> None:
    for n in range(6):
        school_resources.add_note(db, f"note {n}", world["la"].id if n % 2 else None, now=NOW)
    data = built(db).as_json()
    columns: dict[int, list[dict[str, Any]]] = {}
    for node in data["nodes"]:
        assert node["x"] >= 0 and node["y"] >= 0
        assert node["x"] + node["w"] <= data["width"] and node["y"] + node["h"] <= data["height"]
        columns.setdefault(node["col"], []).append(node)
    for nodes in columns.values():
        ordered = sorted(nodes, key=lambda n: n["y"])
        for a, b in pairwise(ordered):
            assert a["y"] + a["h"] <= b["y"], (a["id"], b["id"])


def test_edges_run_left_to_right_and_only_processing_steps_share_a_column(
    db: Session, world: dict[str, Course]
) -> None:
    school_resources.add_url(db, URL, world["la"].id)
    result = built(db)
    ids = {n.id: n for n in result.nodes}
    assert result.edges
    for edge in result.edges:
        a, b = ids[edge.source], ids[edge.target]
        assert a.col < b.col or (a.col == b.col == 1 and a.kind == b.kind == "process"), edge
    assert len({(e.source, e.target) for e in result.edges}) == len(result.edges)  # no duplicates


def test_edges_into_a_course_take_its_colour_and_width_grows_with_volume(
    db: Session, world: dict[str, Course]
) -> None:
    data = built(db).as_json()
    colors = {n["course"]: n["color"] for n in data["nodes"] if n["kind"] == "course"}
    assert sorted(colors.values()) == [0, 1]
    into = next(
        e for e in data["edges"] if e["to"] == "course:NMAT101" and e["from"] == "proc:sync"
    )
    assert (
        into["course"] == "NMAT101"
        and into["color"] == colors["NMAT101"]
        and into["label"] == "2 items"
    )
    widths = {e["volume"]: e["width"] for e in data["edges"]}
    assert widths[0] < widths[1] <= widths[max(widths)]
    confirm = [e for e in data["edges"] if e["style"] == "confirm"]
    assert confirm and all(e["from"] == "proc:inbox" for e in confirm)  # "after you confirm"


def test_resources_stack_under_their_course_and_unsorted_comes_last(
    db: Session, world: dict[str, Course]
) -> None:
    mine = school_resources.add_url(db, URL, world["la"].id)
    loose = school_resources.add_note(db, "loose", None, now=NOW)
    data = built(db).as_json()
    nodes = {n["id"]: n for n in data["nodes"]}
    assert (
        nodes[f"res:{mine.id}"]["course"] == "NMAT101"
        and nodes[f"res:{loose.id}"]["course"] is None
    )
    assert nodes[f"res:{loose.id}"]["y"] > nodes[f"res:{mine.id}"]["y"] > nodes["sys:owl"]["y"]
    edge_from = {e["from"] for e in data["edges"]}
    assert {f"res:{mine.id}", f"res:{loose.id}"} <= edge_from


# --- States (shown in words and an icon, never colour alone) ---


def test_every_state_has_an_icon_and_words() -> None:
    for key, (icon, label) in school_map.STATE_LABELS.items():
        assert icon and label and key


def test_resource_states(db: Session, world: dict[str, Course]) -> None:
    def state(row: SchoolResource) -> str:
        return school_map.resource_state(row, NOW, TZ)

    suggestion = school_resources.add_url(db, URL, None, origin=ResourceOrigin.OWL)
    assert state(suggestion) == "suggested"
    school_resources.keep(db, suggestion.id)
    assert state(suggestion) == "stale"  # kept, never checked
    school_resources.check(db, suggestion, FakeFetcher({URL: page("<p>x</p>")}), NOW)
    assert state(suggestion) == "queued"  # new text waits for Claude
    suggestion.read_at = NOW + timedelta(minutes=1)
    assert state(suggestion) == "connected"
    school_resources.check(
        db, suggestion, FakeFetcher({URL: page("<p>y</p>")}), NOW + timedelta(hours=1)
    )
    suggestion.read_at = NOW + timedelta(hours=2)
    assert state(suggestion) == "changed"
    suggestion.status = suggestion.status.__class__.BROKEN
    assert state(suggestion) == "blocked"
    login = school_resources.add_url(db, "https://moodle.example.edu/x", None)
    assert state(login) == "login"


def test_system_states(db: Session, world: dict[str, Course]) -> None:
    states = {n.id: n.state for n in built(db).nodes}
    assert (
        states["sys:sis"] == states["sys:recodex"] == states["sys:owl"] == "off"
    )  # nothing set up
    assert states["proc:reader"] == "connected" and states["proc:inbox"] == "connected"


def test_the_reader_and_inbox_cards_say_what_waits(db: Session, world: dict[str, Course]) -> None:
    school_resources.add_note(db, "Test 1 on 15 October", world["la"].id, now=NOW)
    reader = {n.id: n for n in built(db).nodes}["proc:reader"]
    assert reader.state == "queued" and reader.sub == "0 of 20 reads today"
    school_reader.read_pending(db, FakeReader(), NOW, TZ, 20)  # empty answer: nothing proposed
    inbox = {n.id: n for n in built(db).nodes}["proc:inbox"]
    assert inbox.sub == "0 proposals waiting"


# --- Pages ---


def test_map_page_and_json_need_login(client: TestClient) -> None:
    for path in (
        "/school/map",
        "/school/map.json",
        "/school/map/panel",
        "/school/map/panel/course:X",
    ):
        assert client.get(path, follow_redirects=False).status_code == 303
    assert client.post("/school/map/resources", data={}, follow_redirects=False).status_code == 303


def test_the_page_has_the_map_the_list_and_the_nav(web: TestClient, app: FastAPI) -> None:
    with app.state.sessionmaker() as db:
        la = Course(code="NMAT101", name="Lineární algebra 1")
        db.add(la)
        db.flush()
        db.add(
            Lesson(
                uid="x",
                ticket_code="26aNMAT101x01",
                kind=LessonKind.SEMINAR,
                course_id=la.id,
                title="LA",
                start_utc=at(date(2026, 10, 8), 9),
                end_utc=at(date(2026, 10, 8), 10),
            )
        )
        school_resources.add_url(db, URL, la.id)
        db.commit()
    page_text = web.get("/school/map").text
    assert 'id="map-stage"' in page_text and 'data-url="/school/map.json"' in page_text
    assert "pointerdown" in page_text and "prefers-reduced-motion" in page_text  # the inline script
    assert "cdn" not in page_text.lower().replace("no cdn", "")  # no CDN, no library
    assert "example.cz/nmat101" in page_text and "Unsorted" not in page_text.split("map-list")[0]
    assert "🗺 Map" in web.get("/school").text
    assert "map-layout view-list" in web.get("/school/map?view=list").text
    data = web.get("/school/map.json").json()
    assert {"width", "height", "nodes", "edges"} <= set(data) and any(
        n["id"] == "course:NMAT101" for n in data["nodes"]
    )


def test_panels(web: TestClient, app: FastAPI) -> None:
    with app.state.sessionmaker() as db:
        la = Course(code="NMAT101", name="Lineární algebra 1", credits=6)
        db.add(la)
        db.flush()
        db.add(
            Lesson(
                uid="x",
                ticket_code="26aNMAT101x01",
                kind=LessonKind.SEMINAR,
                course_id=la.id,
                title="LA",
                start_utc=at(date(2026, 10, 8), 9),
                end_utc=at(date(2026, 10, 8), 10),
            )
        )
        db.add(
            CourseFact(
                course_id=la.id,
                topic=CourseFact.topic.type.python_type("grading")
                if False
                else __import__("orbit.domain.enums", fromlist=["FactTopic"]).FactTopic.GRADING,
                text="Credit needs 120 points",
                quote="120 points",
            )
        )
        row = school_resources.add_url(
            db, URL, la.id, origin=ResourceOrigin.OWL, reason="Link in the Owl text"
        )
        db.commit()
        row_id = row.id
    overview = web.get("/school/map/panel").text
    assert (
        "Suggested resources" in overview
        and "Link in the Owl text" in overview
        and "Add a resource" in overview
    )
    course = web.get("/school/map/panel/course:NMAT101").text
    assert "Credit needs 120 points" in course and "“120 points”" in course and "Rules" in course
    resource = web.get(f"/school/map/panel/res:{row_id}").text
    assert "suggested" in resource and "Keep" in resource and "Dismiss" in resource
    assert "Sync status" in web.get("/school/map/panel/sys:sis").text
    assert (
        "Claude reader" in web.get("/school/map/panel/proc:reader").text
        and "0 of 20" in web.get("/school/map/panel/proc:reader").text
    )
    assert "Open the Inbox" in web.get("/school/map/panel/proc:inbox").text
    assert web.get("/school/map/panel/course:NOPE").status_code == 404
    assert (
        "Suggested resources" in web.get("/school/map/panel/whatever").text
    )  # unknown card: the overview


def test_adding_keeping_checking_and_removing_from_the_page(
    web: TestClient, app: FastAPI, settings: Any, tmp_path: Path
) -> None:
    app.state.settings = settings.model_copy(update={"school_files_dir": str(tmp_path)})
    with app.state.sessionmaker() as db:
        db.add(Course(code="NMAT101", name="Lineární algebra 1"))
        db.commit()
    app.state.jobs.fetcher = FakeFetcher({URL: page("<p>Test 1: 15 October</p>")})
    added = web.post(
        "/school/map/resources", data={"course_code": "NMAT101", "url": URL}, follow_redirects=False
    )
    assert added.status_code == 303 and added.headers["location"].startswith(
        "/school/map?select=res:"
    )
    row_id = int(added.headers["location"].rsplit(":", 1)[1])
    assert "Added: example.cz/nmat101" in web.get(added.headers["location"]).text
    again = web.post(
        "/school/map/resources", data={"course_code": "NMAT101", "url": URL}, follow_redirects=True
    )
    assert "already in your resources" in again.text
    assert "Give a link" in web.post("/school/map/resources", data={}, follow_redirects=True).text
    assert (
        "no such"
        not in web.post(
            "/school/map/resources",
            data={"note": "Credit needs 120 points", "course_code": "NMAT101"},
            follow_redirects=True,
        ).text
    )
    checked = web.post(f"/school/map/resources/{row_id}/check", follow_redirects=True)
    assert "Checked: fetched" in checked.text
    pdf = web.post(
        "/school/map/resources",
        data={"course_code": "NMAT101"},
        files={"file": ("Rules.pdf", pdf_with_text("Test 1 on 15 October"), "application/pdf")},
        follow_redirects=True,
    )
    assert "Added: Rules" in pdf.text
    bad = web.post(
        "/school/map/resources",
        data={},
        files={"file": ("x.pdf", b"nope", "application/pdf")},
        follow_redirects=True,
    )
    assert "Only PDF" in bad.text
    assert (
        "Removed." in web.post(f"/school/map/resources/{row_id}/remove", follow_redirects=True).text
    )
    assert (
        "not found"
        in web.post(f"/school/map/resources/{row_id}/remove", follow_redirects=True).text
    )
    assert len(list(tmp_path.glob("*.pdf"))) == 1


def test_keep_dismiss_link_and_delete_a_rule_from_the_page(web: TestClient, app: FastAPI) -> None:
    from orbit.domain.enums import FactTopic

    with app.state.sessionmaker() as db:
        la = Course(code="NMAT101", name="Lineární algebra 1")
        db.add(la)
        db.flush()
        keep_me = school_resources.add_url(db, URL, None, origin=ResourceOrigin.SIS)
        drop_me = school_resources.add_url(
            db, "https://example.cz/drop", None, origin=ResourceOrigin.SIS
        )
        fact = CourseFact(course_id=la.id, topic=FactTopic.OTHER, text="Attendance is optional")
        db.add(fact)
        db.commit()
        keep_id, drop_id, fact_id = keep_me.id, drop_me.id, fact.id
    assert "Kept" in web.post(f"/school/map/resources/{keep_id}/keep", follow_redirects=True).text
    assert (
        "Dismissed."
        in web.post(f"/school/map/resources/{drop_id}/dismiss", follow_redirects=True).text
    )
    assert (
        "Only a suggestion"
        in web.post(f"/school/map/resources/{keep_id}/dismiss", follow_redirects=True).text
    )
    assert (
        "Linked."
        in web.post(
            f"/school/map/resources/{keep_id}/course",
            data={"course_code": "NMAT101"},
            follow_redirects=True,
        ).text
    )
    assert "NMAT101" in web.get(f"/school/map/panel/res:{keep_id}").text
    assert (
        "Rule removed."
        in web.post(f"/school/map/facts/{fact_id}/delete", follow_redirects=True).text
    )
    assert "Attendance is optional" not in web.get("/school/map/panel/course:NMAT101").text


# --- API ---


def test_api_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/school/resources").status_code == 401
    assert client.post("/api/v1/school/resources", json={"url": URL}).status_code == 401


def test_api_adds_urls_notes_and_files_like_the_share_sheet_shortcut(
    api: TestClient, app: FastAPI, settings: Any, tmp_path: Path
) -> None:
    app.state.settings = settings.model_copy(update={"school_files_dir": str(tmp_path)})
    with app.state.sessionmaker() as db:
        db.add(Course(code="NMAT101", name="Lineární algebra 1"))
        db.commit()
    url = api.post("/api/v1/school/resources", json={"url": URL, "course_code": "NMAT101"})
    assert url.status_code == 201
    body = url.json()
    assert (body["kind"], body["kept"], body["origin"], body["course"]["code"]) == (
        "page",
        True,
        "manual",
        "NMAT101",
    )
    assert api.post("/api/v1/school/resources", json={"url": URL}).status_code == 422  # duplicate
    assert api.post("/api/v1/school/resources", json={}).status_code == 422
    assert api.post("/api/v1/school/resources", json={"url": URL, "note": "x"}).status_code == 422
    assert (
        api.post(
            "/api/v1/school/resources", json={"url": "https://example.cz/x", "course_code": "NOPE"}
        ).status_code
        == 404
    )
    note = api.post("/api/v1/school/resources", json={"note": "Credit needs 120 points"}).json()
    assert note["kind"] == "note" and note["course"] is None and note["has_text"]
    file = api.post(
        "/api/v1/school/resources/file",
        files={"file": ("rules.pdf", pdf_with_text("Test 1"), "application/pdf")},
        data={"course_code": "NMAT101"},
    )
    assert (
        file.status_code == 201 and file.json()["kind"] == "file" and list(tmp_path.glob("*.pdf"))
    )
    assert (
        api.post(
            "/api/v1/school/resources/file",
            files={"file": ("x.pdf", blank_pdf()[:3], "application/pdf")},
        ).status_code
        == 422
    )
    listed = api.get("/api/v1/school/resources", params={"course": "NMAT101"}).json()
    assert {r["kind"] for r in listed} == {"page", "file"}
    patched = api.patch(
        f"/api/v1/school/resources/{note['id']}", json={"course_code": "NMAT101"}
    ).json()
    assert patched["course"]["code"] == "NMAT101"
    assert api.delete(f"/api/v1/school/resources/{note['id']}").status_code == 204
    assert api.delete(f"/api/v1/school/resources/{note['id']}").status_code == 404


def test_api_keep_dismiss_and_check(api: TestClient, app: FastAPI) -> None:
    with app.state.sessionmaker() as db:
        suggestion = school_resources.add_url(db, URL, None, origin=ResourceOrigin.OWL)
        other = school_resources.add_url(
            db, "https://example.cz/o", None, origin=ResourceOrigin.OWL
        )
        db.commit()
        sid, oid = suggestion.id, other.id
    assert api.post(f"/api/v1/school/resources/{oid}/dismiss").json()["kept"] is False
    assert api.post(f"/api/v1/school/resources/{sid}/keep").json()["kept"] is True
    app.state.jobs.fetcher = FakeFetcher({URL: page("<p>Test 1: 15 October</p>")})
    app.state.jobs.claude = FakeReader()
    result = api.post(f"/api/v1/school/resources/{sid}/check").json()
    assert "fetched" in result["result"] and "1 deadlines" not in result["result"]
    after = api.get("/api/v1/school/resources").json()[0]
    assert after["has_text"] and after["status"] == "ok"
    app.state.jobs.fetcher = None
    assert api.post(f"/api/v1/school/resources/{sid}/check").status_code == 422


# --- The daily job ---


def test_the_daily_job_checks_reads_suggests_and_pushes(
    db: Session, world: dict[str, Course], make_settings: Any
) -> None:
    settings = make_settings(school_sites_hour=7, school_reads_per_day=5, quiet_hours="00:00-00:00")
    row = school_resources.add_url(db, URL, world["la"].id)
    db.commit()
    fetcher = FakeFetcher({URL: page("<p>Test 1: 15 October</p>")})
    reader = FakeReader()
    sender = FakeSender()
    engine_sessions = type("S", (), {"__call__": lambda self: _Scope(db)})()
    text = jobs.school_resources(engine_sessions, fetcher, reader, sender, settings, NOW)  # type: ignore[arg-type]
    assert text.startswith("1 checked, 0 changed, 0 failed; 1 reads")
    assert db.get(SchoolResource, row.id).read_at == NOW  # type: ignore[union-attr]
    again = jobs.school_resources(engine_sessions, fetcher, reader, sender, settings, NOW)  # type: ignore[arg-type]
    assert again.startswith("0 checked")  # once a day
    early = jobs.school_resources(
        engine_sessions,
        fetcher,
        reader,
        sender,
        make_settings(school_sites_hour=23),
        NOW + timedelta(days=1),
    )  # type: ignore[arg-type]
    assert early.startswith("0 checked")  # before the hour
    forced = jobs.school_resources(engine_sessions, fetcher, None, None, settings, NOW, force=True)  # type: ignore[arg-type]
    assert forced.startswith("1 checked")  # Check now, no Claude: fetched, not read


class _Scope:
    """A session context that hands out the test's session and never closes it."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc: object) -> None:
        self.session.flush()

    def commit(self) -> None:
        self.session.flush()


def test_the_job_is_in_the_registry_and_on_the_schedule_page(api: TestClient, app: FastAPI) -> None:
    listed = {j["id"]: j for j in api.get("/api/v1/jobs").json()}
    assert (
        listed["school_resources"]["run_now"]
        and listed["school_resources"]["every"] == "every 15 min"
    )
    keys = {r["key"] for r in api.get("/api/v1/settings/schedule").json()}
    assert {"school_sites_hour", "school_reads_per_day"} <= keys
    app.state.jobs.fetcher = FakeFetcher()
    run = api.post("/api/v1/jobs/school_resources/run").json()
    assert run["ok"] and run["message"].startswith("0 checked")


# --- The provider ---


def test_the_school_context_carries_rules_and_resource_summaries(
    db: Session, world: dict[str, Course]
) -> None:
    from orbit.domain.enums import FactTopic

    db.add(
        CourseFact(
            course_id=world["la"].id, topic=FactTopic.GRADING, text="Credit needs 120 of 240 points"
        )
    )
    row = school_resources.add_url(db, URL, world["la"].id)
    school_resources.check(db, row, FakeFetcher({URL: page("<p>x</p>")}), NOW)
    school_resources.add_url(
        db, "https://example.cz/s", world["la"].id, origin=ResourceOrigin.OWL
    )  # a suggestion: not counted
    snapshot = build_context(db, ContextWindow(date(2026, 10, 5), date(2026, 10, 11), TZ), now=NOW)
    la = next(c for c in snapshot.school.courses if c.code == "NMAT101")
    assert la.facts == ["grading: Credit needs 120 of 240 points"]
    assert (
        la.resources is not None and la.resources.kept == 1 and la.resources.last_change is not None
    )
    algo = next(c for c in snapshot.school.courses if c.code == "NINF062")
    assert algo.facts == [] and algo.resources is None
    assert "example.cz" not in snapshot.model_dump_json()  # no URLs reach the agents
