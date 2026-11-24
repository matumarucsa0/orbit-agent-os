"""The intake router and school deadlines from capture (SPEC §2.29)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.app import create_app
from orbit.domain.enums import LessonKind, ProposalKind
from orbit.domain.inbox import Proposal
from orbit.domain.school import Course, Lesson
from orbit.integrations.claude import (
    CaptureRequest,
    ExtractedSchoolDeadline,
    build_capture_prompt,
)
from orbit.services import capture, intake
from orbit.services.errors import InvalidInputError
from tests.conftest import AUTH, reset_schema
from tests.school_resource_helpers import pdf_with_text
from tests.test_bank_parsers import tatra_bytes
from tests.test_capture import FakeExtractor, extraction

TODAY = date(2026, 10, 8)


def add_course(db: Session, code: str, name: str) -> Course:
    course = Course(code=code, name=name)
    db.add(course)
    db.flush()
    db.add(
        Lesson(
            uid=f"26a{code}_1",
            ticket_code="26a" + code,
            kind=LessonKind.LECTURE,
            course_id=course.id,
            title=name,
            start_utc=datetime(2026, 10, 9, 8, tzinfo=UTC),
            end_utc=datetime(2026, 10, 9, 9, tzinfo=UTC),
        )
    )
    db.flush()
    return course


# --- Deciding ---


def decide(**kw: Any) -> intake.Decision:
    args: dict[str, Any] = {"text": None, "url": None, "filename": None, "data": None}
    return intake.decide(**{**args, **kw})


def test_plain_text_is_a_capture() -> None:
    assert decide(text="Paťo mi vrátil 12 €").route is intake.Route.CAPTURE


def test_a_link_alone_or_in_the_url_field_is_a_resource() -> None:
    assert decide(text="https://example.cz/ninf030").route is intake.Route.URL
    found = decide(url="https://example.cz/a", text="pravidlá NINF030")
    assert found.route is intake.Route.URL and found.course_hint == "pravidlá NINF030"
    with pytest.raises(InvalidInputError):
        decide(url="not a link")


def test_files_are_told_apart_by_what_is_in_them() -> None:
    assert decide(filename="x", data=pdf_with_text("hi")).route is intake.Route.PDF
    assert decide(filename="x", data=tatra_bytes()).route is intake.Route.BANK
    assert decide(filename="x", data=b"BEGIN:VCARD\nVERSION:3.0\nEND:VCARD").route is (
        intake.Route.CONTACTS
    )
    with pytest.raises(InvalidInputError):
        decide(filename="x", data=b"hello,world\n1,2\n")
    with pytest.raises(InvalidInputError):
        decide(filename="x", data=b"\x00\x01\x02")


def test_nothing_shared_is_an_error() -> None:
    with pytest.raises(InvalidInputError):
        decide()
    with pytest.raises(InvalidInputError):
        decide(text="   ")


def test_a_course_is_found_by_code_or_name_when_exactly_one_fits(db: Session) -> None:
    add_course(db, "NINF030", "Programování 1")
    add_course(db, "NMAT101", "Lineární algebra 1")
    assert intake.match_course(db, "NMAT101-rules.pdf").code == "NMAT101"
    assert intake.match_course(db, "pravidla programovani 1").code == "NINF030"
    assert intake.match_course(db, "NINF030 and NMAT101") is None
    assert intake.match_course(db, "something else") is None
    assert intake.match_course(db, "") is None


# --- The endpoint ---


@pytest.fixture
def shared_app(make_settings: Any, tmp_path: Path) -> FastAPI:
    app = create_app(make_settings(school_files_dir=str(tmp_path / "files")))
    reset_schema(app.state.engine)
    return app


@pytest.fixture
def intake_client(shared_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(shared_app) as client:
        client.headers.update(AUTH)
        yield client


def test_intake_needs_the_token(client: TestClient) -> None:
    assert client.post("/api/v1/intake", data={"text": "x"}).status_code == 401


def test_text_goes_to_capture(intake_client: TestClient) -> None:
    response = intake_client.post("/api/v1/intake", data={"text": "dôležitá poznámka"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route"] == "capture" and len(body["ids"]) == 1


def test_a_pdf_is_filed_under_its_course_or_left_unsorted(
    intake_client: TestClient, shared_app: FastAPI
) -> None:
    with shared_app.state.sessionmaker() as db:
        add_course(db, "NMAT101", "Lineární algebra 1")
        db.commit()
    pdf = pdf_with_text("Test 1 on 15 October")
    filed = intake_client.post(
        "/api/v1/intake", files={"file": ("NMAT101 rules.pdf", pdf, "application/pdf")}
    )
    assert filed.status_code == 200, filed.text
    assert (filed.json()["route"], filed.json()["course"]) == ("school_resource", "NMAT101")
    other = intake_client.post(
        "/api/v1/intake",
        files={"file": ("rules.pdf", pdf_with_text("Another text"), "application/pdf")},
    )
    assert other.json()["course"] is None and "Unsorted" in other.json()["summary"]


def test_a_link_with_a_course_name_is_filed(intake_client: TestClient, shared_app: FastAPI) -> None:
    with shared_app.state.sessionmaker() as db:
        add_course(db, "NINF030", "Programování 1")
        db.commit()
    response = intake_client.post(
        "/api/v1/intake", data={"url": "https://example.cz/ninf030", "text": "NINF030 web"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["course"] == "NINF030"


def test_a_bank_statement_is_imported(intake_client: TestClient) -> None:
    response = intake_client.post(
        "/api/v1/intake", files={"file": ("s.csv", tatra_bytes(), "text/csv")}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["route"] == "bank_import" and body["summary"].startswith("31 new")


def test_an_unknown_file_is_refused_and_nothing_is_saved(
    intake_client: TestClient, shared_app: FastAPI
) -> None:
    response = intake_client.post(
        "/api/v1/intake", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}
    )
    assert response.status_code == 422
    with shared_app.state.sessionmaker() as db:
        assert db.scalars(select(Proposal)).all() == []


# --- School deadlines from a dictation ---


def dictated(db: Session, *items: ExtractedSchoolDeadline, text: str = "zápočet z NINF030") -> Any:
    fake = FakeExtractor(extraction(school_deadlines=list(items)))
    return fake, capture.capture(db, fake, text, today=TODAY, default_currency="EUR")


def test_a_dictated_exam_becomes_a_school_deadline_proposal(db: Session) -> None:
    course = add_course(db, "NINF030", "Programování 1")
    item = ExtractedSchoolDeadline(
        course_code="NINF030", title="Zápočet", kind="test", due="2026-10-20T10:00"
    )
    fake, result = dictated(db, item)
    (proposal,) = result.proposals
    assert proposal.kind is ProposalKind.SCHOOL_DEADLINE
    assert proposal.payload_json["course_id"] == course.id
    assert proposal.payload_json["source_kind"] == "dictation"
    assert "NINF030" in proposal.summary and "Zápočet" in proposal.summary
    assert "waiting in Inbox" in result.summary
    prompt = build_capture_prompt(fake.requests[0])
    assert "NINF030: Programování 1" in prompt


def test_unlisted_course_past_date_and_known_deadlines_are_handled(db: Session) -> None:
    add_course(db, "NINF030", "Programování 1")
    ghost = ExtractedSchoolDeadline(
        course_code="XXXX999", title="Test", kind="test", due="2026-11-02"
    )
    past = ExtractedSchoolDeadline(
        course_code="NINF030", title="Old", kind="test", due="2026-09-01"
    )
    garbage = ExtractedSchoolDeadline(course_code="NINF030", title="Odd", kind="test", due="soon")
    _, result = dictated(db, ghost, past, garbage, text="test 2. novembra")
    (proposal,) = result.proposals
    assert proposal.payload_json["course_id"] is None  # not listed: no course
    again = dictated(db, ghost, text="test 2. novembra again")[1]
    assert [p.kind for p in again.proposals] == [ProposalKind.CAPTURE]  # known: note draft


def test_the_request_lists_no_courses_without_any() -> None:
    request = CaptureRequest(text="x", today=TODAY, candidates=(), default_currency="EUR")
    assert "school courses" not in build_capture_prompt(request)
