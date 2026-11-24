from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt, Promise
from orbit.domain.people import Person
from orbit.services import capture, inbox, split
from orbit.services.errors import InvalidInputError, NotFoundError
from tests.test_capture import FakeExtractor, person, slovak_answer
from tests.test_split import payment

TODAY = date(2026, 9, 23)


def slovak_proposal(db: Session) -> tuple[Proposal, Person]:
    pato = person(db, "Patrik Veselý", "Paťo")
    result = capture.capture(
        db,
        FakeExtractor(slovak_answer(pato.id)),
        "zaplatil som Paťovi pizzu 12€, sľúbil som mu poslať rozvrh",
        today=TODAY,
        default_currency="EUR",
    )
    return result.proposals[0], pato


def confirm(db: Session, proposal: Proposal, **kwargs: Any) -> inbox.ConfirmResult:
    return inbox.confirm(db, proposal, today=TODAY, default_currency="EUR", **kwargs)


def test_confirm_applies_the_capture(db: Session) -> None:
    proposal, pato = slovak_proposal(db)
    result = confirm(db, proposal)
    assert result.message == "Saved: 1 debt, 1 promise."
    assert proposal.status is ProposalStatus.CONFIRMED and proposal.resolved_at is not None
    debt = db.scalars(select(Debt)).one()
    assert (debt.person_id, debt.amount_minor, debt.currency, debt.description) == (
        pato.id,
        1200,
        "EUR",
        "pizza",
    )
    assert db.scalars(select(Promise)).one().text == "poslať rozvrh"
    assert inbox.pending_count(db) == 0


def test_edit_then_confirm(db: Session) -> None:
    proposal, _ = slovak_proposal(db)
    payload = dict(proposal.payload_json)
    payload["debts"] = [{**payload["debts"][0], "amount_minor": 1500}]
    payload["promises"] = [{**payload["promises"][0], "include": False}]
    result = confirm(db, proposal, payload=payload)
    assert result.message == "Saved: 1 debt."
    assert db.scalars(select(Debt)).one().amount_minor == 1500
    assert db.scalars(select(Promise)).all() == []
    assert proposal.payload_json["debts"][0]["amount_minor"] == 1500  # the edit is kept
    assert proposal.summary == "Patrik Veselý owes you 15.00 € (pizza)"


def test_missing_amount_blocks_confirm_until_filled_in(db: Session) -> None:
    proposal, _ = slovak_proposal(db)
    payload = dict(proposal.payload_json)
    payload["debts"] = [{**payload["debts"][0], "amount_minor": None}]
    inbox.update_payload(db, proposal, payload)
    assert "an amount (missing)" in proposal.summary
    with pytest.raises(InvalidInputError, match="Amount missing"):
        confirm(db, proposal)
    db.rollback()
    assert proposal.status is ProposalStatus.PENDING
    assert db.scalars(select(Debt)).all() == []


def test_invalid_edited_payload_is_refused(db: Session) -> None:
    proposal, _ = slovak_proposal(db)
    payload = {**proposal.payload_json, "debts": [{"person": "Pato", "direction": "sideways"}]}
    with pytest.raises(InvalidInputError, match="Invalid proposal data"):
        confirm(db, proposal, payload=payload)


def test_reject_keeps_the_proposal_but_hides_it(db: Session) -> None:
    proposal, _ = slovak_proposal(db)
    inbox.reject(db, proposal)
    assert proposal.status is ProposalStatus.REJECTED and proposal.resolved_at is not None
    assert inbox.list_proposals(db) == []
    assert inbox.list_proposals(db, status=ProposalStatus.REJECTED) == [proposal]
    assert inbox.list_proposals(db, status=None) == [proposal]
    assert db.scalars(select(Debt)).all() == []


def test_resolved_proposals_cant_be_resolved_again(db: Session) -> None:
    proposal, _ = slovak_proposal(db)
    confirm(db, proposal)
    with pytest.raises(InvalidInputError, match="already confirmed"):
        confirm(db, proposal)
    with pytest.raises(InvalidInputError, match="already confirmed"):
        inbox.reject(db, proposal)
    with pytest.raises(InvalidInputError, match="already confirmed"):
        inbox.update_payload(db, proposal, proposal.payload_json)
    assert len(db.scalars(select(Debt)).all()) == 1


def test_new_person_proposal(db: Session) -> None:
    proposal = Proposal(
        kind=ProposalKind.NEW_PERSON,
        status=ProposalStatus.PENDING,
        summary="New person? Janko",
        payload_json={"name": "Janko Kováč", "context": "Pato's brother"},
        source_type="capture",
    )
    db.add(proposal)
    db.flush()
    result = confirm(db, proposal, payload={"name": "Ján Kováč", "context": "Pato's brother"})
    janko = db.get(Person, result.person_id)
    assert janko is not None
    assert (janko.display_name, janko.first_name, janko.last_name) == ("Ján Kováč", "Ján", "Kováč")
    assert [n.text for n in janko.notes] == ["Pato's brother"]
    assert result.message == "Added Ján Kováč."


def test_payment_payload_is_validated(db: Session) -> None:
    proposal = Proposal(
        kind=ProposalKind.PAYMENT_MATCH,
        status=ProposalStatus.PENDING,
        summary="Received 24.00 € from Patrik Veselý",
        payload_json={},
        source_type="bank_transaction",
    )
    db.add(proposal)
    db.flush()
    with pytest.raises(InvalidInputError, match="Invalid proposal data: transaction_id"):
        confirm(db, proposal)
    inbox.reject(db, proposal)


def test_list_newest_first_and_count(db: Session) -> None:
    first, _ = slovak_proposal(db)
    second = capture.capture(db, None, "niečo", today=TODAY, default_currency="EUR").proposals[0]
    assert inbox.list_proposals(db) == [second, first]
    assert inbox.list_proposals(db, kind=ProposalKind.NEW_PERSON) == []
    assert inbox.pending_count(db) == 2


def test_get_missing_proposal(db: Session) -> None:
    with pytest.raises(NotFoundError):
        inbox.get_proposal(db, 42)


# --- Reopening a rejected proposal ---


def test_reopen_puts_a_rejected_proposal_back(db: Session) -> None:
    proposal, _ = slovak_proposal(db)
    payload = proposal.payload_json
    inbox.reject(db, proposal)
    assert inbox.recently_rejected(db) == [proposal]

    inbox.reopen(db, proposal)
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.resolved_at is None and proposal.reopened_at is not None
    assert proposal.payload_json == payload  # as it was
    assert inbox.recently_rejected(db) == []
    assert confirm(db, proposal).message == "Saved: 1 debt, 1 promise."


def test_only_rejected_proposals_can_be_reopened(db: Session) -> None:
    proposal, _ = slovak_proposal(db)
    with pytest.raises(InvalidInputError, match="this one is pending"):
        inbox.reopen(db, proposal)
    confirm(db, proposal)
    with pytest.raises(InvalidInputError, match="this one is confirmed"):
        inbox.reopen(db, proposal)


def test_recently_rejected_is_limited_to_the_last_days(db: Session) -> None:
    old, _ = slovak_proposal(db)
    inbox.reject(db, old, now=datetime(2026, 8, 1, tzinfo=UTC))
    new = capture.capture(db, None, "poznámka", today=TODAY, default_currency="EUR").proposals[0]
    inbox.reject(db, new, now=datetime(2026, 9, 20, tzinfo=UTC))
    now = datetime(2026, 9, 23, tzinfo=UTC)
    assert inbox.recently_rejected(db, now=now) == [new]
    assert inbox.recently_rejected(db, now=now, days=90) == [new, old]


def test_a_split_cant_be_reopened_once_the_payment_has_a_newer_one(db: Session) -> None:
    tx = payment(db)
    first = split.propose_split(db, tx)
    inbox.reject(db, first)
    second = split.propose_split(db, tx)  # split it by hand from the Import page since
    with pytest.raises(InvalidInputError, match=f"another proposal \\(pending\\): #{second.id}"):
        inbox.reopen(db, first)
    inbox.reject(db, second)
    inbox.reopen(db, first)
    assert split.open_proposal(db, tx) is first
