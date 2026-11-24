"""Appointments from capture (SPEC §2.23): "haircut Friday 15:00" becomes a meeting, not a note."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import ProposalStatus
from orbit.domain.meetings import Meeting
from orbit.domain.people import Note, Person
from orbit.integrations.claude import (
    CaptureExtraction,
    CaptureRequest,
    ExtractedAppointment,
    ExtractedNote,
    build_capture_prompt,
)
from orbit.services import capture, people
from orbit.services.capture import AppointmentDraft, CapturePayload, apply_capture
from orbit.services.errors import InvalidInputError
from orbit.services.ics import build_calendar
from orbit.services.meetings import ORBIT_CALENDAR

TODAY = date(2026, 10, 7)  # a Wednesday
PRAGUE = ZoneInfo("Europe/Prague")
TEXT = "v piatok o 15:00 mám holiča"


class FakeExtractor:
    def __init__(self, result: CaptureExtraction) -> None:
        self.result = result
        self.requests: list[CaptureRequest] = []

    def extract_capture(self, request: CaptureRequest) -> CaptureExtraction:
        self.requests.append(request)
        return self.result


def extraction(**items: Any) -> CaptureExtraction:
    values: dict[str, Any] = {
        "notes": [],
        "interactions": [],
        "debts": [],
        "promises": [],
        "unknown_people": [],
        "confidence": 0.9,
    }
    values.update(items)
    return CaptureExtraction(**values)


@pytest.fixture
def barber(db: Session) -> Person:
    return people.create_person(db, people.PersonCreate(display_name="Roman Holič"))


def haircut(barber: Person, **fields: Any) -> ExtractedAppointment:
    values: dict[str, Any] = {
        "person_ref": str(barber.id),
        "title": "Haircut",
        "start": "2026-10-09T15:00",
    }
    values.update(fields)
    return ExtractedAppointment(**values)


def run(db: Session, answer: CaptureExtraction) -> Any:
    return capture.capture(db, FakeExtractor(answer), TEXT, today=TODAY, default_currency="EUR")


def confirm(db: Session, proposal: Any) -> Any:
    payload = CapturePayload.model_validate(proposal.payload_json)
    return apply_capture(db, payload, today=TODAY, default_currency="EUR")


def test_haircut_becomes_an_appointment_not_a_note(db: Session, barber: Person) -> None:
    result = run(db, extraction(appointments=[haircut(barber)]))

    (proposal,) = result.proposals
    payload = CapturePayload.model_validate(proposal.payload_json)
    assert payload.notes == []
    assert payload.appointments == [
        AppointmentDraft(person=str(barber.id), title="Haircut", start=datetime(2026, 10, 9, 15))
    ]
    assert proposal.status is ProposalStatus.PENDING
    assert "Appointment: Haircut with Roman Holič, Fri 9 Oct 15:00" in proposal.summary
    assert db.scalars(select(Meeting)).first() is None  # nothing before confirming


def test_confirm_creates_a_linked_meeting_in_utc(db: Session, barber: Person) -> None:
    (proposal,) = run(db, extraction(appointments=[haircut(barber, location="Hlavná 1")])).proposals

    applied = confirm(db, proposal)

    assert str(applied) == "1 appointment"
    meeting = db.scalars(select(Meeting)).one()
    assert meeting.calendar_name == ORBIT_CALENDAR
    assert meeting.calendar_uid.startswith("orbit-appt:")
    assert meeting.title == "Haircut"
    assert meeting.location == "Hlavná 1"
    assert meeting.start_utc == datetime(2026, 10, 9, 13, 0, tzinfo=UTC)  # CEST, UTC+2
    assert meeting.end_utc == datetime(2026, 10, 9, 14, 0, tzinfo=UTC)  # default: one hour
    assert meeting.people == [barber]
    assert db.scalars(select(Note)).first() is None


def test_person_is_optional(db: Session) -> None:
    (proposal,) = run(
        db,
        extraction(
            appointments=[
                ExtractedAppointment(person_ref=None, title="Dentist", start="2026-10-12T10:00")
            ]
        ),
    ).proposals

    confirm(db, proposal)

    meeting = db.scalars(select(Meeting)).one()
    assert meeting.people == []


def test_without_a_time_the_text_stays_a_note(db: Session, barber: Person) -> None:
    answer = extraction(appointments=[haircut(barber, start="piatok")])
    (proposal,) = run(db, answer).proposals

    payload = CapturePayload.model_validate(proposal.payload_json)
    assert payload.appointments == []
    assert [n.text for n in payload.notes] == [TEXT]  # the draft fallback keeps what was said


def test_unknown_person_ref_is_dropped_not_guessed(db: Session, barber: Person) -> None:
    (proposal,) = run(db, extraction(appointments=[haircut(barber, person_ref="999")])).proposals

    payload = CapturePayload.model_validate(proposal.payload_json)
    assert payload.appointments[0].person is None


def test_a_past_date_is_refused_on_confirm(db: Session, barber: Person) -> None:
    (proposal,) = run(
        db, extraction(appointments=[haircut(barber, start="2026-10-02T15:00")])
    ).proposals

    with pytest.raises(InvalidInputError, match="isn't past"):
        confirm(db, proposal)
    assert db.scalars(select(Meeting)).first() is None


def test_end_before_start_is_ignored_from_claude_and_refused_by_hand(
    db: Session, barber: Person
) -> None:
    (proposal,) = run(
        db, extraction(appointments=[haircut(barber, end="2026-10-09T14:00")])
    ).proposals
    payload = CapturePayload.model_validate(proposal.payload_json)
    assert payload.appointments[0].end is None

    payload.appointments[0].end = datetime(2026, 10, 9, 14, 0)
    with pytest.raises(InvalidInputError, match="end is before"):
        apply_capture(db, payload, today=TODAY, default_currency="EUR")


def test_same_appointment_twice_is_made_once(db: Session, barber: Person) -> None:
    (first,) = run(db, extraction(appointments=[haircut(barber)])).proposals
    confirm(db, first)
    payload = CapturePayload.model_validate(first.payload_json)

    applied = apply_capture(db, payload, today=TODAY, default_currency="EUR")

    assert str(applied) == "nothing"
    assert len(db.scalars(select(Meeting)).all()) == 1


def test_excluded_appointment_is_not_made(db: Session, barber: Person) -> None:
    (proposal,) = run(db, extraction(appointments=[haircut(barber)])).proposals
    payload = CapturePayload.model_validate(proposal.payload_json)
    payload.appointments[0].include = False

    apply_capture(db, payload, today=TODAY, default_currency="EUR")

    assert db.scalars(select(Meeting)).first() is None


def test_appointment_beside_a_note_keeps_both(db: Session, barber: Person) -> None:
    answer = extraction(
        appointments=[haircut(barber)],
        notes=[ExtractedNote(person_ref=str(barber.id), text="nový salón")],
    )
    (proposal,) = run(db, answer).proposals

    assert str(confirm(db, proposal)) == "1 note, 1 appointment"


def test_the_prompt_tells_claude_about_appointments() -> None:
    from orbit.integrations.claude import CAPTURE_SYSTEM

    assert "appointments:" in CAPTURE_SYSTEM
    assert "never a note" in CAPTURE_SYSTEM
    prompt = build_capture_prompt(
        CaptureRequest(text=TEXT, today=TODAY, candidates=(), default_currency="EUR")
    )
    assert "Today is 2026-10-07 (Wednesday)" in prompt


def test_ics_feed_has_a_timed_event_until_cancelled(db: Session, barber: Person) -> None:
    (proposal,) = run(
        db, extraction(appointments=[haircut(barber, location="Hlavná, 1")])
    ).proposals
    confirm(db, proposal)
    meeting = db.scalars(select(Meeting)).one()

    feed = build_calendar(db, 21, PRAGUE).replace("\r\n ", "")

    assert f"UID:appointment-{meeting.id}@orbit" in feed
    assert "DTSTART:20261009T130000Z" in feed
    assert "DTEND:20261009T140000Z" in feed
    assert "SUMMARY:🗓 Haircut" in feed
    assert "LOCATION:Hlavná\\, 1" in feed
    assert "DESCRIPTION:With Roman Holič" in feed

    meeting.cancelled = True
    db.flush()
    assert "appointment-" not in build_calendar(db, 21, PRAGUE)
