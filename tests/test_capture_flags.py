"""Retry with the validation error, and flags Orbit computes instead of Claude's confidence
(SPEC §2.32)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection
from orbit.integrations.claude import (
    CaptureExtraction,
    ClaudeClient,
    ExtractedDebt,
    ExtractedNote,
    ExtractedPromise,
    UnknownPerson,
    rejection_note,
)
from orbit.services import capture, capture_flags
from orbit.services.capture import CapturePayload
from tests.test_capture import TODAY, FakeExtractor, extraction, person
from tests.test_claude import EMPTY, FakeMessages, answer, request
from tests.test_web_inbox import proposal_ids
from tests.test_web_ledger import post, text
from tests.test_web_people import add

# --- Retry with the error ---


def client_for(*outcomes: object) -> tuple[ClaudeClient, FakeMessages]:
    messages = FakeMessages(*outcomes)
    sdk = SimpleNamespace(messages=messages, close=lambda: None)
    sdk.with_options = lambda **_: sdk  # type: ignore[attr-defined]
    return ClaudeClient("key", "m", client=sdk), messages  # type: ignore[arg-type]


def bad_answer() -> ValidationError:
    try:
        CaptureExtraction.model_validate({"notes": "not a list"})
    except ValidationError as exc:
        return exc
    raise AssertionError("it validated")


def test_the_retry_names_the_fields_that_failed() -> None:
    client, messages = client_for(bad_answer(), answer(EMPTY))
    assert client.extract_capture(request()) is EMPTY
    first, second = (call["messages"][0]["content"] for call in messages.calls)
    assert "rejected by validation" not in first
    assert second.startswith(first)
    assert "Your previous answer was rejected by validation: notes:" in second
    assert "Answer again, fixing exactly these." in second


def test_the_note_is_short_even_for_many_errors() -> None:
    try:
        CaptureExtraction.model_validate({})
    except ValidationError as exc:
        note = rejection_note(exc)
    assert note.count(";") <= 5 and note.endswith("fixing exactly these.")


def test_two_bad_answers_still_fail_cleanly() -> None:
    from orbit.integrations.claude import ClaudeError

    client, _ = client_for(bad_answer(), bad_answer())
    with pytest.raises(ClaudeError):
        client.extract_capture(request())


# --- Flags ---


def payload_for(db: Session, text_: str, answer_: CaptureExtraction) -> CapturePayload:
    result = capture.capture(db, FakeExtractor(answer_), text_, today=TODAY, default_currency="EUR")
    return CapturePayload.model_validate(result.proposals[0].payload_json)


def test_a_name_that_fits_two_people_is_flagged(db: Session) -> None:
    malá = person(db, "Jana Malá")
    person(db, "Jana Veľká")
    payload = payload_for(
        db,
        "Jana mi dlží 5 €",
        extraction(
            debts=[
                ExtractedDebt(
                    person_ref=str(malá.id),
                    direction=DebtDirection.THEY_OWE_ME,
                    type=DebtType.MONEY,
                    amount="5",
                    currency="EUR",
                    description="káva",
                )
            ]
        ),
    )
    (flags,) = capture_flags.flags_for(db, payload)
    assert flags == ["Jana Malá could also be Jana Veľká"]


def test_a_unique_name_has_no_flag(db: Session) -> None:
    pato = person(db, "Paťo Veselý")
    person(db, "Jana Malá")
    payload = payload_for(
        db,
        "Paťo mi dlží 5 €",
        extraction(
            notes=[ExtractedNote(person_ref=str(pato.id), text="dlží 5 €")],
        ),
    )
    assert capture_flags.flags_for(db, payload) == [[]]


def test_a_missing_amount_and_a_new_person_are_flagged_until_fixed(db: Session) -> None:
    payload = payload_for(
        db,
        "Janko mi dlží za kávu",
        extraction(
            debts=[
                ExtractedDebt(
                    person_ref="new:0",
                    direction=DebtDirection.THEY_OWE_ME,
                    type=DebtType.MONEY,
                    amount=None,
                    currency=None,
                    description="káva",
                )
            ],
            unknown_people=[UnknownPerson(name="Janko", context="")],
        ),
    )
    (flags,) = capture_flags.flags_for(db, payload)
    assert flags == ["New person: Janko", "Amount missing"]
    payload.debts[0].amount_minor = 300
    payload.new_people[0].action = "skip"
    assert capture_flags.flags_for(db, payload) == [[]]


def test_a_date_read_from_a_relative_word_is_flagged(db: Session) -> None:
    pato = person(db, "Paťo")
    promise = ExtractedPromise(
        person_ref=str(pato.id),
        direction=PromiseDirection.I_PROMISED,
        text="poslať rozvrh",
        due=date(2026, 9, 24),
        goal_ref=None,
    )
    payload = payload_for(db, "Paťovi pošlem rozvrh zajtra", extraction(promises=[promise]))
    assert capture_flags.flags_for(db, payload) == [["Date read from “zajtra”: check it"]]
    dated = payload_for(db, "Paťovi pošlem rozvrh do 24.9.", extraction(promises=[promise]))
    assert capture_flags.flags_for(db, dated) == [[]]


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("v piatok o piatej", "piatok"),
        ("budúci týždeň", "buduci"),
        ("next Friday", "next"),
        ("15. októbra", None),
    ],
)
def test_relative_words(words: str, expected: str | None) -> None:
    assert capture_flags.relative_word(words) == expected


def test_the_card_shows_flags_and_a_quiet_confidence(web: TestClient, app: FastAPI) -> None:
    add(web, "Jana Malá")
    add(web, "Jana Veľká")
    app.state.claude = FakeExtractor(
        extraction(
            notes=[ExtractedNote(person_ref="new:0", text="nový kolega")],
            unknown_people=[UnknownPerson(name="Janko", context="")],
            confidence=0.4,
        )
    )
    page = post(web, "/inbox/capture", text="Janko je nový kolega")
    assert "⚠ New person: Janko" in text(page)
    assert "Claude's own confidence: 40 %" in text(page)
    assert "wasn't sure" not in page
    (proposal,) = proposal_ids(page)
    detail = web.get(f"/inbox/{proposal}").text
    assert "Check these" in detail and "New person: Janko" in detail
