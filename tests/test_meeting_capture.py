"""Post-meeting capture: dictation with a meeting_id -> one proposal holding the meeting's
interaction; confirming links the interaction, notes and promises to the meeting."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import InteractionKind, PromiseDirection
from orbit.domain.ledger import Promise
from orbit.domain.meetings import Meeting
from orbit.domain.people import Interaction, Note, Person
from orbit.integrations.claude import (
    ExtractedInteraction,
    ExtractedNote,
    ExtractedPromise,
)
from orbit.services import capture, inbox, meetings
from orbit.services.capture import CapturePayload, InteractionDraft
from tests.test_capture import FakeExtractor, extraction
from tests.test_meetings import PRAGUE, FakeSource, event, person, sync

TODAY = date(2026, 9, 24)
JOB_NEWS = "má novú prácu v Brne, sľúbil som mu poslať rozvrh"


@pytest.fixture
def pato(db: Session) -> Person:
    return person(db, "Patrik Veselý", "Paťo", first_name="Patrik")


@pytest.fixture
def meeting(db: Session, pato: Person) -> Meeting:
    sync(db, FakeSource(event(hours=-2)))  # "kafe s Paťom", 08:00-09:00 local today
    return db.scalars(select(Meeting)).one()


def run(
    db: Session, text: str, meeting_id: int | None, extractor: FakeExtractor | None = None
) -> capture.CaptureResult:
    result = capture.capture(
        db,
        extractor,
        text,
        today=TODAY,
        default_currency="EUR",
        meeting_id=meeting_id,
        tz=PRAGUE,
    )
    db.commit()
    return result


def payload(result: capture.CaptureResult) -> CapturePayload:
    return CapturePayload.model_validate(result.proposals[0].payload_json)


def news(pato: Person) -> FakeExtractor:
    return FakeExtractor(
        extraction(
            notes=[ExtractedNote(person_ref=str(pato.id), text="Má novú prácu v Brne")],
            promises=[
                ExtractedPromise(
                    person_ref=str(pato.id),
                    direction=PromiseDirection.I_PROMISED,
                    text="poslať rozvrh",
                    due=None,
                    goal_ref=None,
                )
            ],
        )
    )


def test_meeting_people_and_context_go_to_claude(
    db: Session, pato: Person, meeting: Meeting
) -> None:
    other = person(db, "Tereza Nováková", "Terka")
    extractor = news(pato)
    run(db, "a Terka tam bola tiež, " + JOB_NEWS, meeting.id, extractor)
    (request,) = extractor.requests
    # Paťo isn't named in the text, but he was at the meeting: first candidate.
    assert [c.id for c in request.candidates] == [pato.id, other.id]
    assert request.context == (
        "Dictated right after the meeting “kafe s Paťom” on 2026-09-24 08:00 with "
        f"Patrik Veselý (id {pato.id}). Unless the text says otherwise, it's about them."
    )


def test_the_proposal_holds_the_meeting_interaction(
    db: Session, pato: Person, meeting: Meeting
) -> None:
    result = run(db, JOB_NEWS, meeting.id, news(pato))
    (proposal,) = result.proposals
    assert proposal.source_id == str(meeting.id)
    found = payload(result)
    assert found.meeting_id == meeting.id
    assert found.interactions == [
        InteractionDraft(
            people=[str(pato.id)],
            kind=InteractionKind.MEET,
            summary="kafe s Paťom",
            occurred_on=TODAY,
            meeting=True,
        )
    ]
    assert "Meet with Patrik Veselý: kafe s Paťom" in result.summary


def test_claudes_meet_interaction_becomes_the_meetings(
    db: Session, pato: Person, meeting: Meeting
) -> None:
    terka = person(db, "Tereza Nováková", "Terka")
    extractor = FakeExtractor(
        extraction(
            interactions=[
                ExtractedInteraction(
                    person_refs=[str(terka.id)],
                    kind=InteractionKind.MEET,
                    summary="kávička, Terka prišla neskôr",
                    occurred_on=None,
                ),
                ExtractedInteraction(
                    person_refs=[str(terka.id)],
                    kind=InteractionKind.CALL,
                    summary="volali sme včera",
                    occurred_on=TODAY - timedelta(days=1),
                ),
            ]
        )
    )
    found = payload(run(db, "Terka prišla neskôr", meeting.id, extractor))
    meet, call = found.interactions
    assert meet.people == [str(pato.id), str(terka.id)]
    assert (meet.summary, meet.occurred_on, meet.meeting) == (
        "kávička, Terka prišla neskôr",
        TODAY,
        True,
    )
    assert (call.kind, call.meeting) == (InteractionKind.CALL, False)


def test_without_claude_the_note_goes_to_the_one_person_met(
    db: Session, pato: Person, meeting: Meeting
) -> None:
    found = payload(run(db, "bolo fajn", meeting.id))
    assert [(n.person, n.text) for n in found.notes] == [(str(pato.id), "bolo fajn")]
    assert [i.meeting for i in found.interactions] == [True]


def test_confirming_links_everything_to_the_meeting(
    db: Session, pato: Person, meeting: Meeting
) -> None:
    result = run(db, JOB_NEWS, meeting.id, news(pato))
    confirmed = inbox.confirm(db, result.proposals[0], today=TODAY, default_currency="EUR")
    db.commit()
    assert confirmed.message == "Saved: 1 note, 1 interaction, 1 promise."
    interaction = db.scalars(select(Interaction)).one()
    assert (interaction.meeting_id, interaction.kind, interaction.occurred_on) == (
        meeting.id,
        InteractionKind.MEET,
        TODAY,
    )
    assert [p.id for p in interaction.people] == [pato.id]
    assert db.scalars(select(Note.meeting_id)).all() == [meeting.id]
    assert db.scalars(select(Promise.meeting_id)).all() == [meeting.id]


def test_a_meeting_yields_at_most_one_interaction(
    db: Session, pato: Person, meeting: Meeting
) -> None:
    first = run(db, "bolo fajn", meeting.id)
    second = run(db, "a ešte: má novú prácu", meeting.id)  # dictated before confirming the first
    inbox.confirm(db, first.proposals[0], today=TODAY, default_currency="EUR")
    applied = inbox.confirm(db, second.proposals[0], today=TODAY, default_currency="EUR")
    db.commit()
    assert applied.message == "Saved: 1 note."
    assert len(db.scalars(select(Interaction)).all()) == 1
    third = payload(run(db, "a ešte niečo", meeting.id))  # once it exists, none is proposed
    assert third.interactions == []


def test_unknown_meeting_is_ignored(db: Session, pato: Person) -> None:
    result = run(db, "Paťo má novú prácu", 999)
    (proposal,) = result.proposals
    assert proposal.source_id is None
    found = payload(result)
    assert (found.meeting_id, found.interactions) == (None, [])
    assert [n.person for n in found.notes] == [str(pato.id)]


def test_a_meeting_nobody_is_linked_to_gets_no_interaction(db: Session, pato: Person) -> None:
    sync(db, FakeSource(event(title="Porada", hours=-2)))
    meeting = db.scalars(select(Meeting)).one()
    found = payload(run(db, "nič nové", meeting.id))
    assert (found.meeting_id, found.interactions) == (meeting.id, [])


def test_context_text_for_a_meeting_with_several_people(db: Session, pato: Person) -> None:
    terka = person(db, "Tereza Nováková", "Terka")
    sync(db, FakeSource(event(title="obed s Paťom a Terkou", hours=3)))
    meeting = db.scalars(select(Meeting)).one()
    assert meetings.names(meeting) == "Patrik Veselý and Tereza Nováková"
    assert f"Tereza Nováková (id {terka.id})" in meetings.context_text(meeting, PRAGUE)
