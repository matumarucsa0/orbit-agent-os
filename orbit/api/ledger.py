from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from orbit.api.schemas import (
    BalancesOut,
    CurrencyTotalOut,
    DebtOut,
    PersonBalanceOut,
    PersonRef,
    PromiseOut,
    SettleIn,
    SettleOut,
)
from orbit.deps import DbSession, SettingsDep
from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection, PromiseStatus
from orbit.domain.ledger import Debt, Promise
from orbit.services import ledger, promises

router = APIRouter(tags=["ledger"])

Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]

_DEBT_STATUS: dict[str, bool | None] = {"open": True, "settled": False, "all": None}


# --- Debts ---


@router.get("/debts", response_model=list[DebtOut])
def list_debts(
    db: DbSession,
    person_id: int | None = None,
    status: Literal["open", "settled", "all"] = "open",
    type: DebtType | None = None,
    direction: DebtDirection | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Debt]:
    return ledger.list_debts(
        db,
        person_id=person_id,
        is_open=_DEBT_STATUS[status],
        debt_type=type,
        direction=direction,
        limit=limit,
        offset=offset,
    )


@router.post("/debts", response_model=DebtOut, status_code=status.HTTP_201_CREATED)
def create_debt(body: ledger.DebtCreate, db: DbSession, settings: SettingsDep) -> Debt:
    debt = ledger.create_debt(db, body, settings.default_currency)
    db.commit()
    return debt


@router.get("/debts/{debt_id}", response_model=DebtOut)
def get_debt(debt_id: int, db: DbSession) -> Debt:
    return ledger.get_debt(db, debt_id)


@router.patch("/debts/{debt_id}", response_model=DebtOut)
def update_debt(debt_id: int, body: ledger.DebtUpdate, db: DbSession) -> Debt:
    debt = ledger.update_debt(db, ledger.get_debt(db, debt_id), body)
    db.commit()
    return debt


@router.delete("/debts/{debt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_debt(debt_id: int, db: DbSession) -> None:
    ledger.delete_debt(db, ledger.get_debt(db, debt_id))
    db.commit()


@router.post("/debts/{debt_id}/settle", response_model=SettleOut)
def settle_debt(debt_id: int, db: DbSession, body: SettleIn | None = None) -> SettleOut:
    amount = body.amount_minor if body else None
    result = ledger.settle(db, ledger.get_debt(db, debt_id), amount)
    db.commit()
    return SettleOut(
        settled=DebtOut.model_validate(result.settled),
        remainder=DebtOut.model_validate(result.remainder) if result.remainder else None,
    )


@router.post("/debts/{debt_id}/unsettle", response_model=DebtOut)
def unsettle_debt(debt_id: int, db: DbSession) -> Debt:
    debt = ledger.unsettle(db, ledger.get_debt(db, debt_id))
    db.commit()
    return debt


@router.get("/balances", response_model=BalancesOut)
def balances(db: DbSession, person_id: int | None = None) -> BalancesOut:
    found = ledger.balances(db, person_id)
    return BalancesOut(
        totals=[
            CurrencyTotalOut(currency=t.currency, owed_to_me=t.owed_to_me, i_owe=t.i_owe, net=t.net)
            for t in ledger.totals(found)
        ],
        people=[
            PersonBalanceOut(
                person=PersonRef.model_validate(b.person),
                currency=b.currency,
                amount_minor=b.amount_minor,
            )
            for b in found
        ],
    )


# --- Promises ---


@router.get("/promises", response_model=list[PromiseOut])
def list_promises(
    db: DbSession,
    person_id: int | None = None,
    status: PromiseStatus | Literal["all"] = PromiseStatus.OPEN,
    direction: PromiseDirection | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[Promise]:
    return promises.list_promises(
        db,
        person_id=person_id,
        status=None if status == "all" else PromiseStatus(status),
        direction=direction,
        limit=limit,
        offset=offset,
    )


@router.post("/promises", response_model=PromiseOut, status_code=status.HTTP_201_CREATED)
def create_promise(body: promises.PromiseCreate, db: DbSession, settings: SettingsDep) -> Promise:
    today = datetime.now(settings.timezone).date()
    promise = promises.create_promise(db, body, today)
    db.commit()
    return promise


@router.get("/promises/{promise_id}", response_model=PromiseOut)
def get_promise(promise_id: int, db: DbSession) -> Promise:
    return promises.get_promise(db, promise_id)


@router.patch("/promises/{promise_id}", response_model=PromiseOut)
def update_promise(promise_id: int, body: promises.PromiseUpdate, db: DbSession) -> Promise:
    promise = promises.update_promise(db, promises.get_promise(db, promise_id), body)
    db.commit()
    return promise


@router.delete("/promises/{promise_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_promise(promise_id: int, db: DbSession) -> None:
    promises.delete_promise(db, promises.get_promise(db, promise_id))
    db.commit()
