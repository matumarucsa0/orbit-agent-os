"""Learning from corrections: original payload, alias proposals, prompt lines (SPEC §2.28)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, DebtType, ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.integrations.claude import (
    CaptureRequest,
    ExtractedDebt,
    ExtractedNote,
    UnknownPerson,
    build_capture_prompt,
)
from orbit.services import capture, corrections, inbox
from orbit.services.capture import CapturePayload
from tests.test_capture import TODAY, FakeExtractor, extraction, person


def make_capture(db: Session, text: str = "Janko mi dlží 5 €") -> Proposal:
    answer = extraction(
        notes=[ExtractedNote(person_ref="new:0", text="nový kolega")],
        unknown_people=[UnknownPerson(name="Janko", context="gym")],
        debts=[
            ExtractedDebt(
                person_ref="new:0",
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.MONEY,
                amount="5",
                currency="EUR",
                description="káva",
            )
        ],
    )
    result = capture.capture(db, FakeExtractor(answer), text, today=TODAY, default_currency="EUR")
    return result.proposals[0]


def test_the_original_payload_is_kept_next_to_the_edited_one(db: Session) -> None:
    proposal = make_capture(db)
    assert proposal.original_json == proposal.payload_json
    edited = dict(proposal.payload_json)
    edited["notes"] = [{**edited["notes"][0], "text": "iný text"}]
    inbox.update_payload(db, proposal, edited)
    assert proposal.original_json["notes"][0]["text"] == "nový kolega"
    assert proposal.payload_json["notes"][0]["text"] == "iný text"


def link_and_confirm(db: Session, proposal: Proposal, kovac_id: int, *, flip: bool = False) -> None:
    payload = dict(proposal.payload_json)
    payload["new_people"] = [{**payload["new_people"][0], "action": "link", "person_id": kovac_id}]
    if flip:
        payload["debts"] = [{**payload["debts"][0], "direction": "i_owe_them"}]
    inbox.confirm(db, proposal, today=TODAY, default_currency="EUR", payload=payload)


def test_linking_a_new_name_proposes_an_alias_and_confirming_it_adds_it(db: Session) -> None:
    kovac = person(db, "Janko Kováč")
    proposal = make_capture(db)
    link_and_confirm(db, proposal, kovac.id)
    (alias,) = db.scalars(select(Proposal).where(Proposal.kind == ProposalKind.ADD_ALIAS)).all()
    assert alias.status is ProposalStatus.PENDING
    assert alias.summary == "Add “Janko” as another name of Janko Kováč?"
    inbox.confirm(db, alias, today=TODAY, default_currency="EUR")
    assert [a.value for a in kovac.aliases] == ["Janko"]
    assert capture.match_people(db, "videl som Janka")[0] == kovac


def test_no_alias_is_proposed_twice_or_for_a_name_already_known(db: Session) -> None:
    kovac = person(db, "Janko Kováč", "Janko")  # already answers to it
    link_and_confirm(db, make_capture(db), kovac.id)
    assert db.scalars(select(Proposal).where(Proposal.kind == ProposalKind.ADD_ALIAS)).all() == []
    other = person(db, "Ján Nový")
    link_and_confirm(db, make_capture(db, "Janko 2"), other.id)
    link_and_confirm(db, make_capture(db, "Janko 3"), other.id)
    kinds = db.scalars(select(Proposal).where(Proposal.kind == ProposalKind.ADD_ALIAS)).all()
    assert len(kinds) == 1
    inbox.reject(db, kinds[0])
    link_and_confirm(db, make_capture(db, "Janko 4"), other.id)  # rejected once: not asked again
    assert (
        len(db.scalars(select(Proposal).where(Proposal.kind == ProposalKind.ADD_ALIAS)).all()) == 1
    )


def test_corrections_become_prompt_lines(db: Session) -> None:
    kovac = person(db, "Janko Kováč")
    link_and_confirm(db, make_capture(db), kovac.id, flip=True)
    lines = corrections.recent_corrections(db)
    assert 'the unknown name "Janko" was an existing person: Janko Kováč' in lines
    assert 'a debt "káva" was read as they_owe_me, it is i_owe_them' in lines
    fake = FakeExtractor(extraction())
    capture.capture(db, fake, "niečo nové", today=TODAY, default_currency="EUR")
    prompt = build_capture_prompt(fake.requests[0])
    assert "Corrections the owner made" in prompt and "Janko Kováč" in prompt


def test_untouched_cards_teach_nothing(db: Session) -> None:
    kovac = person(db, "Janko Kováč")
    proposal = make_capture(db)
    payload = dict(proposal.payload_json)
    payload["new_people"] = [{**payload["new_people"][0], "action": "create"}]
    inbox.confirm(db, proposal, today=TODAY, default_currency="EUR", payload=payload)
    assert corrections.recent_corrections(db) == []
    assert kovac


def test_old_corrections_are_forgotten(db: Session) -> None:
    kovac = person(db, "Janko Kováč")
    link_and_confirm(db, make_capture(db), kovac.id)
    later = datetime.now(UTC) + timedelta(days=corrections.LOOKBACK_DAYS + 1)
    assert corrections.recent_corrections(db, now=later) == []
    assert corrections.recent_rejections(db, now=later) == []


def test_rejected_cards_are_told_to_claude(db: Session) -> None:
    proposal = make_capture(db, "Janko mi dlží 5 €")
    inbox.reject(db, proposal)
    assert "Janko owes you 5.00 € (káva)" in corrections.recent_rejections(db)[0]
    request = CaptureRequest(
        text="x", today=TODAY, candidates=(), default_currency="EUR", rejected=("a thing",)
    )
    assert "- a thing" in build_capture_prompt(request)


def test_old_rows_without_an_original_still_work(db: Session) -> None:
    proposal = make_capture(db)
    proposal.original_json = None
    inbox.confirm(db, proposal, today=TODAY, default_currency="EUR")
    assert corrections.recent_corrections(db) == []
    assert CapturePayload.model_validate(proposal.payload_json)
