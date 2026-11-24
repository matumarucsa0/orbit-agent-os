"""Import page: upload a bank statement, see what came in, split or match a payment by hand."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from orbit.deps import (
    DbSession,
    PushSenderDep,
    RateSourceDep,
    SettingsDep,
    TransactionClassifierDep,
)
from orbit.domain.bank import BankImport, Transaction
from orbit.domain.enums import Bank, Classification, ProposalKind
from orbit.domain.inbox import Proposal
from orbit.services import bank_import, notifications, payments, split, transactions
from orbit.services.classify import ClassifyContext
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.transactions import Show
from orbit.web.auth import require_login, safe_next
from orbit.web.inbox import proposal_confirm, proposal_context, proposal_save
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

RECENT_IMPORTS = 10
PAGE_SIZE = 50
CLASS_LABELS = {
    Classification.LIKELY_SHARED: "shared?",
    Classification.PERSONAL: "personal",
    Classification.INCOMING_FROM_PERSON: "from a person",
    Classification.IGNORE: "own money",
}


@router.get("/import")
def import_page(request: Request, db: DbSession) -> Response:
    imports = list(
        db.scalars(
            select(BankImport).order_by(BankImport.imported_at.desc(), BankImport.id.desc())
        ).fetchmany(RECENT_IMPORTS)
    )
    return templates.TemplateResponse(
        request, "bank/import.html", {"imports": imports, "banks": list(Bank)}
    )


@router.post("/import")
def import_upload(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    classifier: TransactionClassifierDep,
    rates: RateSourceDep,
    sender: PushSenderDep,
    file: UploadFile,
    bank: Annotated[str, Form()] = "auto",
) -> Response:
    try:
        report = bank_import.import_statement(
            db,
            file.file.read(),
            filename=file.filename,
            bank=Bank(bank) if bank in {b.value for b in Bank} else None,
            context=ClassifyContext.from_settings(settings, today_local(request)),
            classifier=classifier,
            rates=rates,
        )
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/import", status_code=303)
    db.commit()
    bank_import.notify_import(
        db,
        sender,
        report,
        base_url=settings.orbit_url,
        secret=settings.orbit_secret_key.get_secret_value(),
        quiet=notifications.quiet_hours(settings),
    )
    db.commit()
    flash(request, report.summary)
    return RedirectResponse(f"/import/{report.bank_import.id}", status_code=303)


@router.get("/import/{import_id:int}")
def import_detail(
    request: Request, db: DbSession, import_id: int, show: Show | None = None
) -> Response:
    record = db.get(BankImport, import_id)
    if record is None:
        raise NotFoundError(f"Import {import_id} not found.")
    counts = transactions.count_by_show(db, import_id)
    if show is None:
        # Open on what needs a look; when nothing looks shared, just show everything.
        show = "shared" if counts["shared"] else "all"
    query = transactions.filtered(
        select(Transaction).where(Transaction.import_id == import_id), show
    )
    rows = list(db.scalars(query.order_by(Transaction.booking_date.desc(), Transaction.id)))
    return templates.TemplateResponse(
        request,
        "bank/detail.html",
        {
            "record": record,
            "show": show,
            "counts": counts,
            "back": _here(request),
            **_rows_context(db, rows),
        },
    )


@router.get("/transactions")
def transaction_list(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    q: str = "",
    show: Show = "all",
    offset: int = 0,
) -> Response:
    """Every bank row across all imports, searchable, to split or match an older payment."""
    found = transactions.search_transactions(
        db,
        q,
        show=show,
        currency=settings.default_currency,
        limit=PAGE_SIZE + 1,
        offset=offset,
    )
    context = {
        "q": q,
        "show": show,
        "counts": transactions.count_by_show(db),
        "has_more": len(found) > PAGE_SIZE,
        "next_offset": offset + PAGE_SIZE,
        "back": _here(request),
        **_rows_context(db, found[:PAGE_SIZE]),
    }
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "results":
        return templates.TemplateResponse(request, "bank/_tx_results.html", context)
    return templates.TemplateResponse(request, "bank/transactions.html", context)


def _here(request: Request) -> str:
    """This page's path and query: where Split / Match go back to on an error."""
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def _rows_context(db: DbSession, rows: list[Transaction]) -> dict[str, Any]:
    """The rows plus, per row, its newest split and payment-match proposals."""
    proposals: dict[int, Proposal] = {}
    matches: dict[int, Proposal] = {}
    for p in db.scalars(
        select(Proposal)
        .where(
            Proposal.kind.in_([ProposalKind.SPLIT_TRANSACTION, ProposalKind.PAYMENT_MATCH]),
            Proposal.source_type == split.SOURCE_TYPE,
            Proposal.source_id.in_([str(tx.id) for tx in rows]),
        )
        .order_by(Proposal.id)
    ):
        found = proposals if p.kind is ProposalKind.SPLIT_TRANSACTION else matches
        found[int(p.source_id or 0)] = p
    return {
        "transactions": rows,
        "proposals": proposals,
        "matches": matches,
        "class_labels": CLASS_LABELS,
    }


def _transaction(db: DbSession, transaction_id: int) -> Transaction:
    tx = db.get(Transaction, transaction_id)
    if tx is None:
        raise NotFoundError(f"Transaction {transaction_id} not found.")
    return tx


@router.get("/transactions/{transaction_id:int}/split")
def split_page(
    request: Request, db: DbSession, settings: SettingsDep, transaction_id: int, back: str = ""
) -> Response:
    """The split form for any outgoing payment, before anything is stored: backing out leaves
    no Inbox item behind. An open split for it goes straight to that Inbox item."""
    tx = _transaction(db, transaction_id)
    existing = split.open_proposal(db, tx)
    if existing is not None:
        return RedirectResponse(f"/inbox/{existing.id}", status_code=303)
    try:
        draft = split.draft_proposal(tx)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(safe_next(back or f"/import/{tx.import_id}"), status_code=303)
    context = proposal_context(db, request, settings, draft)
    context["form_base"] = f"/transactions/{tx.id}/split"
    context["cancel_url"] = safe_next(back) if back else f"/import/{tx.import_id}"
    return templates.TemplateResponse(request, "inbox/detail.html", context)


@router.post("/transactions/{transaction_id:int}/split")
def split_transaction(
    request: Request,
    db: DbSession,
    transaction_id: int,
    back: Annotated[str | None, Form()] = None,
) -> Response:
    """Open a split for any outgoing payment, flagged or not (my own choice, not automation)."""
    tx = _transaction(db, transaction_id)
    try:
        proposal = split.propose_split(db, tx)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(safe_next(back or f"/import/{tx.import_id}"), status_code=303)
    db.commit()
    return RedirectResponse(f"/inbox/{proposal.id}", status_code=303)


@router.post("/transactions/{transaction_id:int}/split/{op}")
async def split_submit(
    request: Request, db: DbSession, settings: SettingsDep, transaction_id: int, op: str
) -> Response:
    """Save or Confirm from the not-yet-stored split form: now it becomes an Inbox item."""
    if op not in ("save", "confirm"):
        raise NotFoundError(f"Unknown action {op}.")
    tx = _transaction(db, transaction_id)
    proposal = split.propose_split(db, tx)
    db.commit()
    handler = proposal_confirm if op == "confirm" else proposal_save
    return await handler(request, db, settings, proposal.id)


@router.get("/transactions/{transaction_id:int}/match")
def match_page(
    request: Request, db: DbSession, settings: SettingsDep, transaction_id: int, back: str = ""
) -> Response:
    """The payment form for any incoming payment, before anything is stored (like Split)."""
    tx = _transaction(db, transaction_id)
    existing = split.open_proposal(db, tx, ProposalKind.PAYMENT_MATCH)
    if existing is not None:
        return RedirectResponse(f"/inbox/{existing.id}", status_code=303)
    try:
        draft = payments.draft_match(db, tx)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(safe_next(back or f"/import/{tx.import_id}"), status_code=303)
    context = proposal_context(db, request, settings, draft)
    context["form_base"] = f"/transactions/{tx.id}/match"
    context["cancel_url"] = safe_next(back) if back else f"/import/{tx.import_id}"
    return templates.TemplateResponse(request, "inbox/detail.html", context)


@router.post("/transactions/{transaction_id:int}/match/{op}")
async def match_submit(
    request: Request, db: DbSession, settings: SettingsDep, transaction_id: int, op: str
) -> Response:
    """Save or Confirm from the not-yet-stored payment form: now it becomes an Inbox item."""
    if op not in ("save", "confirm"):
        raise NotFoundError(f"Unknown action {op}.")
    proposal = payments.propose_match(db, _transaction(db, transaction_id), force=True)
    assert proposal is not None  # force: always one
    db.commit()
    handler = proposal_confirm if op == "confirm" else proposal_save
    return await handler(request, db, settings, proposal.id)


@router.post("/transactions/{transaction_id:int}/match")
def match_transaction(
    request: Request,
    db: DbSession,
    transaction_id: int,
    back: Annotated[str | None, Form()] = None,
) -> Response:
    """Match any incoming payment to debts by hand, whoever sent it (my own choice)."""
    tx = db.get(Transaction, transaction_id)
    if tx is None:
        raise NotFoundError(f"Transaction {transaction_id} not found.")
    try:
        proposal = payments.propose_match(db, tx, force=True)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(safe_next(back or f"/import/{tx.import_id}"), status_code=303)
    assert proposal is not None  # force: always one
    db.commit()
    return RedirectResponse(f"/inbox/{proposal.id}", status_code=303)
