"""Ledger: what is owed between me and people: per-currency balances and open debts.

Balances are per currency and never converted. Without a focus the debts are all open ones, those
due soonest first, plus what was settled in the last 90 days; with a person focus, that person's.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, DebtType
from orbit.domain.ledger import Debt
from orbit.services import ledger
from orbit.services.context.base import (
    NAME_CHARS,
    ContextFocus,
    ContextWindow,
    ModuleContext,
    text,
)

MAX_DEBTS = 500
MAX_BALANCES = 100
MAX_SETTLED = 100
SETTLED_DAYS = 90  # settled debts this recent are history worth reading


class BalanceContext(BaseModel):
    person_id: int
    person: str
    currency: str
    amount_minor: int  # > 0: they owe me; < 0: I owe them


class TotalContext(BaseModel):
    currency: str
    owed_to_me_minor: int
    i_owe_minor: int


class DebtContext(BaseModel):
    id: int
    person_id: int
    person: str
    direction: DebtDirection
    type: DebtType
    amount_minor: int | None  # money only
    currency: str | None
    description: str | None
    due: date | None
    overdue: bool  # due before the window starts
    due_in_window: bool
    settled_on: date | None = None  # settled debts only


class LedgerContext(ModuleContext):
    trimmable: ClassVar[tuple[str, ...]] = ("debts", "recently_settled", "balances")
    module: Literal["ledger"] = "ledger"
    totals: list[TotalContext]  # over everyone, or over the focus person
    balances: list[BalanceContext]  # largest first
    debts: list[DebtContext]  # open
    recently_settled: list[DebtContext]  # newest first, the last 90 days


def _debt_context(debt: Debt, window: ContextWindow) -> DebtContext:
    return DebtContext(
        id=debt.id,
        person_id=debt.person_id,
        person=text(debt.person.display_name, NAME_CHARS),
        direction=debt.direction,
        type=debt.type,
        amount_minor=debt.amount_minor,
        currency=debt.currency,
        description=text(debt.description) if debt.description else None,
        due=debt.due,
        overdue=debt.due is not None and debt.due < window.start,
        due_in_window=debt.due is not None and window.contains(debt.due),
        settled_on=debt.settled_at.astimezone(window.tz).date() if debt.settled_at else None,
    )


class LedgerProvider:
    name = "ledger"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> LedgerContext:
        person_id = focus.person_id if focus is not None else None
        found = ledger.balances(session, person_id)
        query = select(Debt).where(Debt.settled_at.is_(None))
        if person_id is not None:
            query = query.where(Debt.person_id == person_id)
        debts = list(
            session.scalars(
                query.order_by(Debt.due.is_(None), Debt.due, Debt.created_at, Debt.id).limit(
                    MAX_DEBTS + 1
                )
            )
        )
        settled_query = select(Debt).where(
            Debt.settled_at.is_not(None),
            Debt.settled_at >= window.start_utc - timedelta(days=SETTLED_DAYS),
        )
        if person_id is not None:
            settled_query = settled_query.where(Debt.person_id == person_id)
        settled = list(
            session.scalars(
                settled_query.order_by(Debt.settled_at.desc(), Debt.id.desc()).limit(
                    MAX_SETTLED + 1
                )
            )
        )
        return LedgerContext(
            totals=[
                TotalContext(
                    currency=t.currency, owed_to_me_minor=t.owed_to_me, i_owe_minor=t.i_owe
                )
                for t in ledger.totals(found)
            ],
            balances=[
                BalanceContext(
                    person_id=b.person.id,
                    person=text(b.person.display_name, NAME_CHARS),
                    currency=b.currency,
                    amount_minor=b.amount_minor,
                )
                for b in found[:MAX_BALANCES]
            ],
            debts=[_debt_context(d, window) for d in debts[:MAX_DEBTS]],
            recently_settled=[_debt_context(d, window) for d in settled[:MAX_SETTLED]],
            truncated=(
                len(debts) > MAX_DEBTS or len(found) > MAX_BALANCES or len(settled) > MAX_SETTLED
            ),
        )
