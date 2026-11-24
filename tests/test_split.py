from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from orbit.domain.bank import BankImport, Transaction
from orbit.domain.enums import (
    Bank,
    DebtDirection,
    DebtType,
    ProposalKind,
    ProposalStatus,
    TransactionDirection,
    TransactionKind,
)
from orbit.domain.people import Person
from orbit.services import inbox, ledger, split
from orbit.services.errors import InvalidInputError
from orbit.services.split import SplitPayload, SplitShare, even_amounts
from tests.test_ledger import make


def payment(
    db: Session,
    amount_minor: int = 4800,
    merchant: str = "Lokál",
    direction: TransactionDirection = TransactionDirection.OUT,
    currency: str = "EUR",
) -> Transaction:
    tx = Transaction(
        bank_import=BankImport(bank=Bank.TATRA),
        bank=Bank.TATRA,
        booking_date=date(2026, 9, 8),
        amount_minor=amount_minor,
        currency=currency,
        direction=direction,
        kind=TransactionKind.CARD,
        counterparty=merchant,
        counterparty_norm=merchant.lower(),
        hash=f"{merchant}-{amount_minor}-{direction}",
        raw_json={},
    )
    db.add(tx)
    db.flush()
    return tx


def shares(*people: Person, amounts: tuple[int | None, ...] = ()) -> list[SplitShare]:
    amounts = amounts or (None,) * len(people)
    return [
        SplitShare(person_id=p.id, amount_minor=a) for p, a in zip(people, amounts, strict=True)
    ]


# --- Even amounts ---


@pytest.mark.parametrize(
    ("total", "people", "include_me", "expected"),
    [
        (4800, 2, True, [1600, 1600]),  # 48 € between me + 2: my share isn't a debt
        (4800, 2, False, [2400, 2400]),  # I paid only for them
        (1000, 2, True, [333, 333]),  # I keep the leftover cent
        (1000, 3, False, [334, 333, 333]),  # someone has to take the cent
        (1, 1, False, [1]),
    ],
)
def test_even_amounts(total: int, people: int, include_me: bool, expected: list[int]) -> None:
    assert even_amounts(total, people, include_me) == expected


def test_even_amounts_needs_people_and_money() -> None:
    with pytest.raises(InvalidInputError, match="at least one person"):
        even_amounts(4800, 0, True)
    with pytest.raises(InvalidInputError, match="too small"):
        even_amounts(2, 2, True)


# --- Applying ---


def test_even_split_creates_money_debts_linked_to_the_payment(db: Session) -> None:
    """ROADMAP Phase 6: splitting one creates the right debts."""
    tx = payment(db)
    pato, terka = make(db, "Patrik Veselý"), make(db, "Terka")
    payload = SplitPayload(transaction_id=tx.id, shares=shares(pato, terka))
    assert split.describe(db, payload) == [
        "Patrik Veselý owes you 16.00 €",
        "Terka owes you 16.00 €",
    ]
    debts = split.apply_split(db, payload, "EUR")
    assert [(d.person_id, d.amount_minor, d.currency) for d in debts] == [
        (pato.id, 1600, "EUR"),
        (terka.id, 1600, "EUR"),
    ]
    for debt in debts:
        assert debt.direction is DebtDirection.THEY_OWE_ME and debt.type is DebtType.MONEY
        assert debt.description == "Lokál"
        assert debt.source_transaction_id == tx.id
        assert debt.reference_code is not None
    (balance,) = ledger.balances(db, pato.id)
    assert (balance.currency, balance.amount_minor) == ("EUR", 1600)


def test_custom_split_uses_the_amounts_and_the_payments_currency(db: Session) -> None:
    tx = payment(db, 120000, "Restaurace U Fleků", currency="CZK")
    pato, terka = make(db, "Pato"), make(db, "Terka")
    payload = SplitPayload(
        transaction_id=tx.id,
        mode="custom",
        shares=shares(pato, terka, amounts=(50000, 20000)),
        description="dinner",
    )
    debts = split.apply_split(db, payload, "EUR")
    assert [(d.amount_minor, d.currency, d.description) for d in debts] == [
        (50000, "CZK", "dinner"),
        (20000, "CZK", "dinner"),
    ]


def test_custom_split_checks_the_amounts(db: Session) -> None:
    tx = payment(db)
    pato, terka = make(db, "Pato"), make(db, "Terka")
    too_much = SplitPayload(
        transaction_id=tx.id, mode="custom", shares=shares(pato, terka, amounts=(2400, 2401))
    )
    with pytest.raises(InvalidInputError, match=r"more than the payment \(48.00 €\)"):
        split.plan(db, too_much)
    missing = SplitPayload(
        transaction_id=tx.id, mode="custom", shares=shares(pato, terka, amounts=(2400, None))
    )
    with pytest.raises(InvalidInputError, match="amount for everyone"):
        split.plan(db, missing)
    whole = SplitPayload(
        transaction_id=tx.id, mode="custom", shares=shares(pato, terka, amounts=(2400, 2400))
    )
    assert [p.amount_minor for p in split.plan(db, whole)[1]] == [2400, 2400]


def test_split_validation(db: Session) -> None:
    tx = payment(db)
    pato = make(db, "Pato")
    with pytest.raises(InvalidInputError, match="at least one person"):
        split.plan(db, SplitPayload(transaction_id=tx.id))
    with pytest.raises(InvalidInputError, match="only once"):
        split.plan(db, SplitPayload(transaction_id=tx.id, shares=shares(pato, pato)))
    with pytest.raises(InvalidInputError, match="Person 999 no longer exists"):
        split.plan(db, SplitPayload(transaction_id=tx.id, shares=[SplitShare(person_id=999)]))
    with pytest.raises(InvalidInputError, match="no longer exists"):
        split.plan(db, SplitPayload(transaction_id=12345, shares=shares(pato)))
    incoming = payment(db, 2000, "Pato", direction=TransactionDirection.IN)
    with pytest.raises(InvalidInputError, match="Only outgoing"):
        split.plan(db, SplitPayload(transaction_id=incoming.id, shares=shares(pato)))
    assert split.describe(db, SplitPayload(transaction_id=tx.id)) == [
        "Pick at least one person to split with."
    ]


# --- Proposals ---


def test_propose_split_reuses_an_open_proposal(db: Session) -> None:
    tx = payment(db)
    proposal = split.propose_split(db, tx, 0.7)
    assert proposal.kind is ProposalKind.SPLIT_TRANSACTION
    assert proposal.summary == "Lokál, 48.00 € — split?"
    assert proposal.payload_json == {
        "transaction_id": tx.id,
        "mode": "even",
        "include_me": True,
        "shares": [],
        "description": None,
    }
    assert (proposal.confidence, proposal.source_type, proposal.source_id) == (
        0.7,
        "bank_transaction",
        str(tx.id),
    )
    assert split.propose_split(db, tx) is proposal
    inbox.reject(db, proposal)
    again = split.propose_split(db, tx)  # I changed my mind: split it after all
    assert again.id != proposal.id and again.status is ProposalStatus.PENDING
    with pytest.raises(InvalidInputError, match="Only outgoing"):
        split.propose_split(db, payment(db, 500, "x", direction=TransactionDirection.IN))


def test_confirming_a_split_proposal_through_the_inbox(db: Session) -> None:
    tx = payment(db)
    pato, terka = make(db, "Patrik Veselý"), make(db, "Terka")
    proposal = split.propose_split(db, tx)
    with pytest.raises(InvalidInputError, match="at least one person"):
        inbox.confirm(db, proposal, today=date(2026, 9, 24), default_currency="EUR")
    assert proposal.status is ProposalStatus.PENDING
    edited = SplitPayload(transaction_id=tx.id, include_me=False, shares=shares(pato, terka))
    result = inbox.confirm(
        db,
        proposal,
        today=date(2026, 9, 24),
        default_currency="EUR",
        payload=edited.model_dump(mode="json"),
    )
    assert result.message == (
        "Split into 2 debts: Patrik Veselý owes you 24.00 €; Terka owes you 24.00 €."
    )
    assert proposal.status is ProposalStatus.CONFIRMED
    assert split.propose_split(db, tx) is proposal  # a confirmed split isn't proposed again
    with pytest.raises(InvalidInputError, match="already confirmed"):
        inbox.confirm(db, proposal, today=date(2026, 9, 24), default_currency="EUR")


def test_invalid_split_payloads_are_refused(db: Session) -> None:
    tx = payment(db)
    proposal = split.propose_split(db, tx)
    with pytest.raises(InvalidInputError, match="Invalid proposal data: mode"):
        inbox.update_payload(db, proposal, {"transaction_id": tx.id, "mode": "weird"})


def test_draft_proposal_is_not_stored(db: Session) -> None:
    tx = payment(db)
    draft = split.draft_proposal(tx)
    assert draft.id is None and draft not in db
    assert draft.payload_json["transaction_id"] == tx.id
    assert draft.summary == "Lokál, 48.00 € — split?"
    assert split.open_proposal(db, tx) is None
    with pytest.raises(InvalidInputError, match="Only outgoing"):
        split.draft_proposal(payment(db, 500, "x", direction=TransactionDirection.IN))
