"""Splitting a bank payment: a `split_transaction` proposal becomes money debts on confirm.

Even split: the amount is divided between the chosen people and, unless I paid only for them,
me; my share is never a debt, and I take any leftover cent so nobody is overcharged.
Custom split: each person's amount is set explicitly; together at most the payment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.bank import Transaction
from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    ProposalKind,
    ProposalStatus,
    TransactionDirection,
)
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt
from orbit.domain.money import format_amount
from orbit.domain.people import Person
from orbit.services import ledger
from orbit.services.errors import InvalidInputError
from orbit.services.proposal_kinds import Applied, ApplyContext, ProposalHandler

SOURCE_TYPE = "bank_transaction"


class SplitShare(BaseModel):
    person_id: int
    amount_minor: int | None = Field(default=None, gt=0)  # custom split only


class SplitPayload(BaseModel):
    transaction_id: int
    mode: Literal["even", "custom"] = "even"
    # Even split: am I one of the people it's divided between? (False: I paid only for them.)
    include_me: bool = True
    shares: list[SplitShare] = Field(default_factory=list)
    # The debts' description; defaults to the merchant.
    description: str | None = Field(default=None, max_length=500)


@dataclass(frozen=True)
class PlannedDebt:
    person: Person
    amount_minor: int


def get_transaction(session: Session, transaction_id: int) -> Transaction:
    """The bank row with this id; raises InvalidInputError when it no longer exists."""
    tx = session.get(Transaction, transaction_id)
    if tx is None:
        raise InvalidInputError(f"Bank transaction {transaction_id} no longer exists.")
    return tx


def even_amounts(total: int, people: int, include_me: bool) -> list[int]:
    """Each person's share of `total` (minor units)."""
    parts = people + (1 if include_me else 0)
    if people < 1:
        raise InvalidInputError("Pick at least one person to split with.")
    base, leftover = divmod(total, parts)
    if base == 0:
        raise InvalidInputError("The amount is too small to split between that many people.")
    if include_me:
        return [base] * people  # I keep the leftover cents
    return [base + (1 if i < leftover else 0) for i in range(people)]


def plan(session: Session, payload: SplitPayload) -> tuple[Transaction, list[PlannedDebt]]:
    """Validate the payload and work out each person's debt. Raises InvalidInputError."""
    tx = get_transaction(session, payload.transaction_id)
    if tx.direction is not TransactionDirection.OUT:
        raise InvalidInputError("Only outgoing payments can be split.")
    ids = [share.person_id for share in payload.shares]
    if not ids:
        raise InvalidInputError("Pick at least one person to split with.")
    if len(set(ids)) != len(ids):
        raise InvalidInputError("Each person can be in the split only once.")
    people = {p.id: p for p in session.scalars(select(Person).where(Person.id.in_(ids)))}
    if missing := [pid for pid in ids if pid not in people]:
        raise InvalidInputError(f"Person {missing[0]} no longer exists.")
    if payload.mode == "even":
        amounts = even_amounts(tx.amount_minor, len(ids), payload.include_me)
    else:
        if any(share.amount_minor is None for share in payload.shares):
            raise InvalidInputError("Enter an amount for everyone in a custom split.")
        amounts = [share.amount_minor or 0 for share in payload.shares]
        if sum(amounts) > tx.amount_minor:
            total = format_amount(tx.amount_minor, tx.currency)
            raise InvalidInputError(f"The shares add up to more than the payment ({total}).")
    return tx, [PlannedDebt(people[pid], amount) for pid, amount in zip(ids, amounts, strict=True)]


def describe(session: Session, payload: SplitPayload) -> list[str]:
    """ "Patrik owes you 16.00 €" per share, or why it can't be applied yet."""
    try:
        tx, planned = plan(session, payload)
    except InvalidInputError as exc:
        return [str(exc)]
    return [
        f"{p.person.display_name} owes you {format_amount(p.amount_minor, tx.currency)}"
        for p in planned
    ]


def apply_split(session: Session, payload: SplitPayload, default_currency: str) -> list[Debt]:
    """Create the money debts a confirmed `split_transaction` describes and return them (runs inside
    `inbox.confirm`)."""
    tx, planned = plan(session, payload)
    description = payload.description or tx.counterparty or tx.message
    debts: list[Debt] = []
    for item in planned:
        debt = ledger.create_debt(
            session,
            ledger.DebtCreate(
                person_id=item.person.id,
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.MONEY,
                amount_minor=item.amount_minor,
                currency=tx.currency,
                description=description,
            ),
            default_currency,
        )
        debt.source_transaction_id = tx.id
        debts.append(debt)
    session.flush()
    return debts


def split_summary(tx: Transaction) -> str:
    """Inbox summary for a bank row proposed for splitting."""
    return f"{tx.label}, {format_amount(tx.amount_minor, tx.currency)} — split?"


def open_proposal(
    session: Session, tx: Transaction, kind: ProposalKind = ProposalKind.SPLIT_TRANSACTION
) -> Proposal | None:
    """The pending or confirmed proposal of this kind for this bank row, if there is one."""
    return session.scalars(
        select(Proposal)
        .where(
            Proposal.kind == kind,
            Proposal.source_type == SOURCE_TYPE,
            Proposal.source_id == str(tx.id),
            Proposal.status != ProposalStatus.REJECTED,
        )
        .order_by(Proposal.id.desc())
        .limit(1)
    ).first()


def propose_split(session: Session, tx: Transaction, confidence: float | None = None) -> Proposal:
    """A `split_transaction` proposal for an outgoing payment (reuses an open one)."""
    if tx.direction is not TransactionDirection.OUT:
        raise InvalidInputError("Only outgoing payments can be split.")
    existing = open_proposal(session, tx)
    if existing is not None:
        return existing
    proposal = draft_proposal(tx, confidence)
    session.add(proposal)
    session.flush()
    return proposal


def draft_proposal(tx: Transaction, confidence: float | None = None) -> Proposal:
    """A new, unsaved `split_transaction` proposal for an outgoing payment (for the split form
    before anything is stored)."""
    if tx.direction is not TransactionDirection.OUT:
        raise InvalidInputError("Only outgoing payments can be split.")
    return Proposal(
        kind=ProposalKind.SPLIT_TRANSACTION,
        status=ProposalStatus.PENDING,
        summary=split_summary(tx),
        payload_json=SplitPayload(transaction_id=tx.id).model_dump(mode="json"),
        confidence=confidence,
        source_type=SOURCE_TYPE,
        source_id=str(tx.id),
    )


def check_reopen(session: Session, proposal: Proposal) -> None:
    """A bank row gets at most one open split and one open payment match: a rejected proposal
    can't be reopened when its row is gone or a newer one of the same kind exists."""
    tx = session.get(Transaction, int(proposal.source_id or 0))
    if tx is None:
        raise InvalidInputError("Its bank row no longer exists.")
    newer = open_proposal(session, tx, proposal.kind)
    if newer is not None:
        raise InvalidInputError(
            f"This payment already has another proposal ({newer.status.value}): #{newer.id}."
        )


def _confirm(session: Session, payload: SplitPayload, ctx: ApplyContext) -> Applied:
    lines = describe(session, payload)
    debts = apply_split(session, payload, ctx.default_currency)
    noun = "debt" if len(debts) == 1 else "debts"
    return Applied(f"Split into {len(debts)} {noun}: " + "; ".join(lines) + ".")


HANDLER = ProposalHandler(
    ProposalKind.SPLIT_TRANSACTION, SplitPayload, _confirm, check_reopen=check_reopen
)
