"""Ledger page, plus the debt and promise forms used on it and on person pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from orbit.deps import DbSession, SettingsDep
from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection, PromiseStatus
from orbit.domain.ledger import Debt
from orbit.domain.money import format_amount, parse_amount
from orbit.services import goals, ledger, people, promises
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login, safe_next
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])


def _back(next_url: str, fallback: str) -> RedirectResponse:
    return RedirectResponse(safe_next(next_url or fallback), status_code=303)


def _person_url(person_id: int) -> str:
    return f"/people/{person_id}#ledger"


@dataclass(frozen=True)
class DebtGroups:
    """Open non-money debts split by kind and direction, for the Ledger page."""

    lent: list[Debt]
    borrowed: list[Debt]
    favors_owed_to_me: list[Debt]
    favors_i_owe: list[Debt]


def _groups(debts: list[Debt]) -> DebtGroups:
    def pick(debt_type: DebtType, direction: DebtDirection) -> list[Debt]:
        return [d for d in debts if d.type is debt_type and d.direction is direction]

    return DebtGroups(
        lent=pick(DebtType.ITEM, DebtDirection.THEY_OWE_ME),
        borrowed=pick(DebtType.ITEM, DebtDirection.I_OWE_THEM),
        favors_owed_to_me=pick(DebtType.FAVOR, DebtDirection.THEY_OWE_ME),
        favors_i_owe=pick(DebtType.FAVOR, DebtDirection.I_OWE_THEM),
    )


LEDGER_PAGE = (
    20  # rows per Ledger section; "Show all" (`/ledger?all=<key>`) lifts it for one section
)
ALL_ROWS = 100_000  # the Ledger lists every open row; the service default of 500 would hide some


@dataclass(frozen=True)
class Section:
    """One Ledger list: the rows to draw and how many were left out."""

    key: str
    rows: list[Any]
    total: int
    hidden: int


def _section(key: str, rows: list[Any], expanded: str) -> Section:
    if key == expanded:
        return Section(key, rows, len(rows), 0)
    return Section(key, rows[:LEDGER_PAGE], len(rows), max(0, len(rows) - LEDGER_PAGE))


@router.get("/ledger")
def ledger_page(request: Request, db: DbSession, all: str = "") -> Response:
    balances = ledger.balances(db)
    groups = _groups(ledger.list_debts(db, limit=ALL_ROWS))
    sections: dict[str, list[Any]] = {
        "owed_to_me": [b for b in balances if b.amount_minor > 0],
        "i_owe": [b for b in balances if b.amount_minor < 0],
        "lent": groups.lent,
        "borrowed": groups.borrowed,
        "favors_owed_to_me": groups.favors_owed_to_me,
        "favors_i_owe": groups.favors_i_owe,
        "promises": promises.list_promises(db, limit=ALL_ROWS),
    }
    return templates.TemplateResponse(
        request,
        "ledger.html",
        {
            "totals": ledger.totals(balances),  # from every balance, not the visible rows
            "sections": {key: _section(key, rows, all) for key, rows in sections.items()},
            "active_goals": goals.active_goals(db),
            "today": today_local(request),
        },
    )


# --- Debts ---


@router.post("/people/{person_id:int}/debts")
def debt_add(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    person_id: int,
    direction: Annotated[DebtDirection, Form()],
    type: Annotated[DebtType, Form()],
    amount: Annotated[str, Form()] = "",
    currency: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    due: Annotated[date | None, Form()] = None,
    reminder_days: Annotated[int | None, Form(ge=1, le=3650)] = None,
) -> Response:
    people.get_person(db, person_id)
    back = _person_url(person_id)
    currency = (currency.strip() or settings.default_currency).upper()
    try:
        amount_minor = None
        if type is DebtType.MONEY:
            if not amount.strip():
                raise InvalidInputError("Enter an amount, e.g. 12.50.")
            amount_minor = parse_amount(amount, currency)
        data = ledger.DebtCreate(
            person_id=person_id,
            direction=direction,
            type=type,
            amount_minor=amount_minor,
            currency=currency if type is DebtType.MONEY else None,
            description=description,
            due=due,
            reminder_days=reminder_days if type is DebtType.ITEM else None,
        )
        debt = ledger.create_debt(db, data, settings.default_currency)
    except ValidationError:
        db.rollback()
        flash(request, "Check the amount and currency (3 letters, e.g. EUR).", "error")
        return _back("", back)
    except (InvalidInputError, ValueError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back("", back)
    db.commit()
    if debt.amount_minor is not None and debt.currency is not None:
        flash(
            request,
            f"Added {format_amount(debt.amount_minor, debt.currency)} ({debt.reference_code}).",
        )
    else:
        flash(request, f"Added {debt.type.value}: {debt.description}.")
    return _back("", back)


@router.post("/debts/{debt_id:int}/settle")
def debt_settle(
    request: Request,
    db: DbSession,
    debt_id: int,
    amount: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
) -> Response:
    debt = ledger.get_debt(db, debt_id)
    back = _person_url(debt.person_id)
    try:
        amount_minor = None
        if amount.strip():
            if debt.currency is None:
                raise InvalidInputError(f"An {debt.type.value} can only be settled in full.")
            amount_minor = parse_amount(amount, debt.currency)
        result = ledger.settle(db, debt, amount_minor)
    except (InvalidInputError, ValueError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(next, back)
    db.commit()
    rest = result.remainder
    if rest is not None and rest.amount_minor is not None and rest.currency is not None:
        left = format_amount(rest.amount_minor, rest.currency)
        message = f"Partly settled; {left} still open ({rest.reference_code})."
    else:
        message = "Settled."
    undo = f"/debts/{debt.id}/unsettle"
    flash(request, message, action=undo, fields={"next": next or back})
    return _back(next, back)


@router.post("/debts/{debt_id:int}/edit")
def debt_edit(
    request: Request,
    db: DbSession,
    debt_id: int,
    amount: Annotated[str, Form()] = "",
    currency: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    due: Annotated[date | None, Form()] = None,
    next: Annotated[str, Form()] = "",
) -> Response:
    """Fix a typo in an open debt; its reference code stays."""
    debt = ledger.get_debt(db, debt_id)
    back = _person_url(debt.person_id)
    fields: dict[str, object] = {"description": description, "due": due}
    try:
        if debt.type is DebtType.MONEY:
            code = (currency.strip() or debt.currency or "").upper()
            if not amount.strip():
                raise InvalidInputError("Enter an amount, e.g. 12.50.")
            fields |= {"amount_minor": parse_amount(amount, code), "currency": code}
        elif not description.strip():
            raise InvalidInputError(f"Say which {debt.type.value} it is.")
        ledger.update_debt(db, debt, ledger.DebtUpdate.model_validate(fields))
    except ValidationError:
        db.rollback()
        flash(request, "Check the amount and currency (3 letters, e.g. EUR).", "error")
        return _back(next, back)
    except (InvalidInputError, ValueError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(next, back)
    db.commit()
    flash(request, f"Saved {debt.reference_code}." if debt.reference_code else "Saved.")
    return _back(next, back)


@router.post("/debts/{debt_id:int}/unsettle")
def debt_unsettle(
    request: Request, db: DbSession, debt_id: int, next: Annotated[str, Form()] = ""
) -> Response:
    debt = ledger.get_debt(db, debt_id)
    try:
        ledger.unsettle(db, debt)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(next, _person_url(debt.person_id))
    db.commit()
    flash(request, "Settlement undone.")
    return _back(next, _person_url(debt.person_id))


@router.post("/debts/{debt_id:int}/delete")
def debt_delete(
    request: Request, db: DbSession, debt_id: int, next: Annotated[str, Form()] = ""
) -> Response:
    debt = ledger.get_debt(db, debt_id)
    back = _person_url(debt.person_id)
    try:
        ledger.delete_debt(db, debt)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _back(next, back)
    db.commit()
    flash(request, "Deleted.")
    return _back(next, back)


# --- Promises ---


def _promise_url(person_id: int) -> str:
    return f"/people/{person_id}#promises"


@router.post("/people/{person_id:int}/promises")
def promise_add(
    request: Request,
    db: DbSession,
    person_id: int,
    direction: Annotated[PromiseDirection, Form()],
    text: Annotated[str, Form()],
    due: Annotated[date | None, Form()] = None,
    goal: Annotated[str, Form()] = "",
) -> Response:
    people.get_person(db, person_id)
    try:
        data = promises.PromiseCreate(
            person_id=person_id,
            direction=direction,
            text=text,
            due=due,
            goal_id=int(goal) if goal.isdigit() else None,
        )
    except ValidationError:
        flash(request, "A promise needs some text.", "error")
        return _back("", _promise_url(person_id))
    promise = promises.create_promise(db, data, today_local(request))
    db.commit()
    due_text = f" (due {promise.due.day} {promise.due:%b})" if promise.due else ""
    flash(request, f"Promise added{due_text}.")
    return _back("", _promise_url(person_id))


_STATUS_DONE = {
    PromiseStatus.DONE: "Marked done.",
    PromiseStatus.DROPPED: "Dropped.",
    PromiseStatus.OPEN: "Reopened.",
}


@router.post("/promises/{promise_id:int}/status")
def promise_status(
    request: Request,
    db: DbSession,
    promise_id: int,
    status: Annotated[PromiseStatus, Form()],
    next: Annotated[str, Form()] = "",
) -> Response:
    promise = promises.get_promise(db, promise_id)
    before = promise.status
    promises.update_promise(db, promise, promises.PromiseUpdate(status=status))
    db.commit()
    if before is not status:
        flash(
            request,
            _STATUS_DONE[status],
            action=f"/promises/{promise.id}/status",
            fields={"status": before.value, "next": next or _promise_url(promise.person_id)},
        )
    return _back(next, _promise_url(promise.person_id))


@router.post("/promises/{promise_id:int}/edit")
def promise_edit(
    request: Request,
    db: DbSession,
    promise_id: int,
    direction: Annotated[PromiseDirection, Form()],
    text: Annotated[str, Form()],
    due: Annotated[date | None, Form()] = None,
    goal: Annotated[str, Form()] = "",
    goal_set: Annotated[str, Form()] = "",  # "1" when the form had the goal picker
    next: Annotated[str, Form()] = "",
) -> Response:
    promise = promises.get_promise(db, promise_id)
    back = _promise_url(promise.person_id)
    try:
        change = promises.PromiseUpdate(direction=direction, text=text, due=due)
        if goal_set:
            change = promises.PromiseUpdate(
                direction=direction,
                text=text,
                due=due,
                goal_id=int(goal) if goal.isdigit() else None,
            )
        promises.update_promise(db, promise, change)
    except (InvalidInputError, ValidationError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, InvalidInputError) else "A promise needs some text."
        flash(request, message, "error")
        return _back(next, back)
    db.commit()
    flash(request, "Promise saved.")
    return _back(next, back)


@router.post("/promises/{promise_id:int}/delete")
def promise_delete(
    request: Request, db: DbSession, promise_id: int, next: Annotated[str, Form()] = ""
) -> Response:
    promise = promises.get_promise(db, promise_id)
    person_id = promise.person_id
    promises.delete_promise(db, promise)
    db.commit()
    flash(request, "Promise deleted.")
    return _back(next, _promise_url(person_id))
