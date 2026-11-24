"""Debts (money / items / favors), settling, and per-currency balances. Callers commit.

Money balances are per currency and never converted. Items and favors never touch balances.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import DebtDirection, DebtType
from orbit.domain.ledger import Debt
from orbit.domain.people import Person
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.people import get_person

REFERENCE_PREFIX = "D-"

Currency = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{3}$")
]


class DebtUpdate(BaseModel):
    """PATCH semantics for an open debt: only fields that were set are applied."""

    model_config = ConfigDict(str_strip_whitespace=True)

    direction: DebtDirection | None = None
    amount_minor: int | None = Field(default=None, gt=0)
    currency: Currency | None = None
    description: str | None = Field(default=None, max_length=500)
    due: date | None = None
    reminder_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class DebtCreate(DebtUpdate):
    person_id: int
    direction: DebtDirection
    type: DebtType = DebtType.MONEY


@dataclass(frozen=True)
class SettleResult:
    settled: Debt
    remainder: Debt | None


@dataclass(frozen=True)
class PersonBalance:
    person: Person
    currency: str
    amount_minor: int  # > 0: they owe me; < 0: I owe them


@dataclass(frozen=True)
class CurrencyTotal:
    currency: str
    owed_to_me: int  # sum of positive per-person balances
    i_owe: int  # sum of negative per-person balances, as a positive number

    @property
    def net(self) -> int:
        return self.owed_to_me - self.i_owe


def _check(
    debt_type: DebtType,
    amount_minor: int | None,
    currency: str | None,
    description: str | None,
    reminder_days: int | None,
) -> None:
    if debt_type is DebtType.MONEY:
        if amount_minor is None or amount_minor <= 0:
            raise InvalidInputError("A money debt needs an amount above zero.")
        if currency is None:
            raise InvalidInputError("A money debt needs a currency.")
    else:
        if amount_minor is not None or currency is not None:
            raise InvalidInputError(f"An {debt_type.value} debt has no amount.")
        if not description:
            raise InvalidInputError(f"Describe the {debt_type.value}, e.g. “charger”.")
    if reminder_days is not None and debt_type is not DebtType.ITEM:
        raise InvalidInputError("Only lent items have a reminder.")


def next_reference_code(session: Session) -> str:
    """D-0001, D-0002, …; past D-9999 the number just grows (D-10000)."""
    last = session.scalar(
        select(Debt.reference_code)
        .where(Debt.reference_code.is_not(None))
        .order_by(func.length(Debt.reference_code).desc(), Debt.reference_code.desc())
        .limit(1)
    )
    number = int(last.removeprefix(REFERENCE_PREFIX)) + 1 if last else 1
    return f"{REFERENCE_PREFIX}{number:04d}"


def get_debt(session: Session, debt_id: int) -> Debt:
    """The debt with this id; raises NotFoundError."""
    debt = session.get(Debt, debt_id)
    if debt is None:
        raise NotFoundError(f"Debt {debt_id} not found.")
    return debt


def create_debt(session: Session, data: DebtCreate, default_currency: str) -> Debt:
    """Create a debt (money, item or favor) for a person; a money debt uses the default currency
    when none is given and gets a reference code."""
    person = get_person(session, data.person_id)
    currency = data.currency
    if data.type is DebtType.MONEY and currency is None:
        currency = default_currency
    _check(data.type, data.amount_minor, currency, data.description, data.reminder_days)
    debt = Debt(
        direction=data.direction,
        type=data.type,
        amount_minor=data.amount_minor,
        currency=currency,
        description=data.description,
        due=data.due,
        reminder_days=data.reminder_days,
    )
    if data.type is DebtType.MONEY:
        debt.reference_code = next_reference_code(session)
    person.debts.append(debt)
    session.flush()
    return debt


def update_debt(session: Session, debt: Debt, data: DebtUpdate) -> Debt:
    """Edit an open debt (PATCH semantics); a settled debt must have its settlement undone first."""
    if not debt.is_open:
        raise InvalidInputError("A settled debt can't be edited; undo the settlement first.")
    fields = data.model_fields_set
    if "direction" in fields:
        if data.direction is None:
            raise InvalidInputError("A debt needs a direction.")
        debt.direction = data.direction
    changes = {name: getattr(data, name) for name in fields if name != "direction"}
    merged = {
        name: changes.get(name, getattr(debt, name))
        for name in ("amount_minor", "currency", "description", "reminder_days")
    }
    _check(debt.type, **merged)
    for name, value in changes.items():
        setattr(debt, name, value)
    session.flush()
    return debt


def delete_debt(session: Session, debt: Debt) -> None:
    """Delete a debt entered by mistake. Settled debts are history and stay."""
    if not debt.is_open:
        raise InvalidInputError("A settled debt can't be deleted; undo the settlement first.")
    if debt.parent is not None:
        # Deleting the rest would make the earlier settlement look like a full payment.
        raise InvalidInputError(
            "This is what's left after a partial payment: settle it, or undo that payment."
        )
    debt.person.debts.remove(debt)  # delete-orphan cascade deletes the row
    session.flush()


def settle(
    session: Session, debt: Debt, amount_minor: int | None = None, now: datetime | None = None
) -> SettleResult:
    """Settle a debt fully, or part of a money debt: the original is settled and an open
    remainder debt (new reference code, same details) is created for the rest."""
    if not debt.is_open:
        raise InvalidInputError("This debt is already settled.")
    now = now or utcnow()
    if amount_minor is None or amount_minor == debt.amount_minor:
        debt.settled_at = now
        session.flush()
        return SettleResult(debt, None)
    if debt.type is not DebtType.MONEY or debt.amount_minor is None:
        raise InvalidInputError(f"An {debt.type.value} can only be settled in full.")
    if amount_minor <= 0:
        raise InvalidInputError("The settled amount must be above zero.")
    if amount_minor > debt.amount_minor:
        raise InvalidInputError("That's more than is owed; settle the debt in full instead.")
    debt.settled_at = now
    remainder = Debt(
        direction=debt.direction,
        type=debt.type,
        amount_minor=debt.amount_minor - amount_minor,
        currency=debt.currency,
        description=debt.description,
        due=debt.due,
        reference_code=next_reference_code(session),
    )
    # Into the session (via the person) before linking, so no autoflush sees a loose object.
    debt.person.debts.append(remainder)
    remainder.parent = debt
    session.flush()
    return SettleResult(debt, remainder)


def unsettle(session: Session, debt: Debt) -> Debt:
    """Undo a settlement (a mis-tap): reopen the debt and drop the remainder it created."""
    if debt.is_open:
        raise InvalidInputError("This debt isn't settled.")
    remainder = debt.remainder
    if remainder is not None:
        if not remainder.is_open:
            raise InvalidInputError(
                f"The rest ({remainder.reference_code}) was settled since; undo that first."
            )
        remainder.person.debts.remove(remainder)
    debt.settled_at = None
    debt.settled_by_transaction_id = None
    session.flush()
    session.expire(debt, ["remainder"])
    return debt


def paid_amount(debt: Debt) -> int | None:
    """For a settled money debt: how much this settlement covered (the rest moved on)."""
    if debt.is_open or debt.amount_minor is None:
        return None
    rest = debt.remainder.amount_minor if debt.remainder is not None else 0
    return debt.amount_minor - (rest or 0)


def list_debts(
    session: Session,
    person_id: int | None = None,
    is_open: bool | None = True,
    debt_type: DebtType | None = None,
    direction: DebtDirection | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[Debt]:
    """Open debts oldest first (what's been waiting longest); settled ones newest first."""
    query = select(Debt)
    if person_id is not None:
        query = query.where(Debt.person_id == person_id)
    if is_open is True:
        query = query.where(Debt.settled_at.is_(None)).order_by(Debt.created_at, Debt.id)
    elif is_open is False:
        query = query.where(Debt.settled_at.is_not(None)).order_by(
            Debt.settled_at.desc(), Debt.id.desc()
        )
    else:
        query = query.order_by(Debt.created_at.desc(), Debt.id.desc())
    if debt_type is not None:
        query = query.where(Debt.type == debt_type)
    if direction is not None:
        query = query.where(Debt.direction == direction)
    return list(session.scalars(query.limit(limit).offset(offset)))


def balances(session: Session, person_id: int | None = None) -> list[PersonBalance]:
    """Net open money per (person, currency), zero balances left out; largest first."""
    signed = case(
        (Debt.direction == DebtDirection.THEY_OWE_ME, Debt.amount_minor),
        else_=-Debt.amount_minor,
    )
    total = func.sum(signed)
    query = (
        select(Debt.person_id, Debt.currency, total)
        .where(Debt.type == DebtType.MONEY, Debt.settled_at.is_(None))
        .group_by(Debt.person_id, Debt.currency)
    )
    if person_id is not None:
        query = query.where(Debt.person_id == person_id)
    rows = [(pid, cur, int(amount)) for pid, cur, amount in session.execute(query) if amount]
    people = {
        p.id: p for p in session.scalars(select(Person).where(Person.id.in_([r[0] for r in rows])))
    }
    found = [PersonBalance(people[pid], cur, amount) for pid, cur, amount in rows]
    found.sort(key=lambda b: (-abs(b.amount_minor), b.person.search_name_norm, b.currency))
    return found


def totals(found: list[PersonBalance]) -> list[CurrencyTotal]:
    """Sum per-person balances into one total per currency: what is owed to me and what I owe.
    Currencies are never mixed."""
    owed: dict[str, int] = defaultdict(int)
    owing: dict[str, int] = defaultdict(int)
    for balance in found:
        if balance.amount_minor > 0:
            owed[balance.currency] += balance.amount_minor
        else:
            owing[balance.currency] -= balance.amount_minor
    return [CurrencyTotal(c, owed[c], owing[c]) for c in sorted(owed.keys() | owing.keys())]


def item_reminder_on(debt: Debt, default_days: int, tz: ZoneInfo) -> date | None:
    """When to nudge about an open item I lent: the day it was logged + its reminder days."""
    if not debt.is_open or debt.type is not DebtType.ITEM:
        return None
    if debt.direction is not DebtDirection.THEY_OWE_ME:
        return None
    since = debt.created_at.astimezone(tz).date()
    return since + timedelta(days=debt.reminder_days or default_days)
