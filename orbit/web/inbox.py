"""Inbox pages: proposal cards, the capture box, the edit-then-confirm form, the split form and
the payment form ("Choose…")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.datastructures import FormData

from orbit.deps import CaptureExtractorDep, DbSession, SettingsDep, run_capture
from orbit.domain.bank import Transaction
from orbit.domain.enums import DebtDirection, DebtType, ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt
from orbit.domain.money import parse_amount
from orbit.domain.people import Person
from orbit.services import capture, capture_flags, goals, inbox, meetings, payments, search, split
from orbit.services.capture import CapturePayload, DebtDraft, NewPersonPayload
from orbit.services.errors import InvalidInputError
from orbit.services.payments import DebtSettle, PayerSource, PaymentMatchPayload
from orbit.services.split import SplitPayload, SplitShare
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

SPLIT_SEARCH_LIMIT = 8
RECENT_PEOPLE = 6
REJECTED_DAYS = 30  # how far back the Inbox lists rejected proposals to reopen

PROCESSING_SHOWN = timedelta(minutes=10)  # after this a draft stops saying "reading"
KIND_LABELS = {
    ProposalKind.CAPTURE: "🎙️ Capture",
    ProposalKind.NEW_PERSON: "👤 New person",
    ProposalKind.SPLIT_TRANSACTION: "🧾 Split?",
    ProposalKind.PAYMENT_MATCH: "💶 Payment",
    ProposalKind.FOLLOW_UP: "📌 Follow up",
    ProposalKind.PROMISE_DUE: "📅 New due date",
    ProposalKind.GOAL_RERANK: "🎯 Goal order",
    ProposalKind.GOAL_LINK: "🎯 Goal link",
    ProposalKind.SCHOOL_DEADLINE: "🎓 School deadline",
    ProposalKind.COURSE_FACT: "🎓 Course rule",
    ProposalKind.ADD_ALIAS: "🏷 Another name",
}
BANK_KINDS = (ProposalKind.SPLIT_TRANSACTION, ProposalKind.PAYMENT_MATCH)  # need a form first
PAYER_NOTES = {
    PayerSource.IBAN: "recognized by their IBAN",
    PayerSource.BANK_NAME: "recognized by the name on the statement",
    PayerSource.REFERENCE: "found by the reference code in the payment",
    PayerSource.NAME: "guessed from the name on the statement — check it",
    PayerSource.MANUAL: "picked by you",
}


@dataclass
class Card:
    proposal: Proposal
    label: str
    lines: list[str] = field(default_factory=list)
    # Money debts without an amount: (index into payload.debts, the draft, its line).
    missing: list[tuple[int, DebtDraft, str]] = field(default_factory=list)
    capture: CapturePayload | None = None
    new_person: NewPersonPayload | None = None
    split: SplitPayload | None = None
    payment: PaymentMatchPayload | None = None
    payer: Person | None = None
    payer_note: str | None = None  # how the payer was recognized
    transaction: Transaction | None = None
    group_pending: int = 0  # pending cards from the same dictation, this one included
    processing: bool = False  # Claude is still reading the dictation (SPEC §2.33)
    # What Orbit checked per line of `lines` (SPEC §2.32): an unclear name, a missing amount, ...
    flags: list[list[str]] = field(default_factory=list)

    @property
    def can_confirm(self) -> bool:
        if self.split is not None:
            return bool(self.split.shares)
        if self.payment is not None:
            return self.payer is not None and bool(self.payment.settle)
        return self.proposal.kind not in (
            ProposalKind.SPLIT_TRANSACTION,
            ProposalKind.PAYMENT_MATCH,
        )


def _names(db: DbSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = db.execute(select(Person.id, Person.display_name).where(Person.id.in_(ids)))
    return {pid: name for pid, name in rows}


def _card(db: DbSession, proposal: Proposal) -> Card:
    card = Card(proposal, KIND_LABELS.get(proposal.kind, proposal.kind.value))
    if proposal.status is ProposalStatus.PENDING and proposal.kind in (
        ProposalKind.CAPTURE,
        ProposalKind.NEW_PERSON,
    ):
        card.group_pending = len(capture.pending_in_group(db, proposal.payload_json.get("group")))
    if proposal.kind is ProposalKind.CAPTURE:
        payload = CapturePayload.model_validate(proposal.payload_json)
        names = _names(db, payload.referenced_ids())
        card.capture = payload
        age = datetime.now(UTC) - proposal.created_at
        card.processing = payload.processing and age < PROCESSING_SHOWN
        card.lines = [capture.describe_item(item, payload, names) for item in payload.included()]
        card.flags = capture_flags.flags_for(db, payload)
        card.missing = [
            (i, d, capture.describe_item(d, payload, names))
            for i, d in enumerate(payload.debts)
            if d.include and d.amount_missing
        ]
    elif proposal.kind is ProposalKind.NEW_PERSON:
        card.new_person = NewPersonPayload.model_validate(proposal.payload_json)
    elif proposal.kind is ProposalKind.SPLIT_TRANSACTION:
        card.split = SplitPayload.model_validate(proposal.payload_json)
        card.transaction = db.get(Transaction, card.split.transaction_id)
        if card.split.shares:
            card.lines = split.describe(db, card.split)
    elif proposal.kind is ProposalKind.PAYMENT_MATCH:
        payment = card.payment = PaymentMatchPayload.model_validate(proposal.payload_json)
        card.transaction = db.get(Transaction, payment.transaction_id)
        if payment.person_id is not None:
            card.payer = db.get(Person, payment.person_id)
        if payment.payer_source is not None:
            card.payer_note = PAYER_NOTES[payment.payer_source]
        card.lines = payments.describe(db, payment)
    else:
        card.lines = [proposal.summary]
    return card


@router.get("/inbox")
def inbox_page(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    cards = [_card(db, p) for p in inbox.list_proposals(db)]
    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "cards": cards,
            "rejected": inbox.recently_rejected(db, days=REJECTED_DAYS),
            "rejected_days": REJECTED_DAYS,
            "kind_labels": KIND_LABELS,
            "default_currency": settings.default_currency,
        },
    )


@router.post("/inbox/capture")
def capture_text(
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    settings: SettingsDep,
    extractor: CaptureExtractorDep,
    text: Annotated[str, Form()],
    meeting_id: Annotated[int | None, Form()] = None,
) -> Response:
    """The capture box (Inbox page), or a note after a meeting (its page, with `meeting_id`).
    Saves the text at once; Claude reads it afterwards (SPEC §2.33)."""
    back = f"/meetings/{meeting_id}" if meeting_id is not None else "/inbox"
    try:
        result = run_capture(
            request, background, db, settings, extractor, text, meeting_id=meeting_id
        )
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(back, status_code=303)
    flash(request, result.summary, link="/inbox", link_label="Inbox")
    return RedirectResponse(back if meeting_id is not None else "/inbox", status_code=303)


def _people_options(db: DbSession, payload: CapturePayload) -> tuple[list[Person], list[Person]]:
    """(mentioned, everyone else) for the person pickers; mentioned also covers archived ones."""
    mentioned_ids = payload.referenced_ids()
    mentioned = list(
        db.scalars(
            select(Person).where(Person.id.in_(mentioned_ids)).order_by(Person.search_name_norm)
        )
    )
    others = list(
        db.scalars(
            select(Person)
            .where(Person.archived.is_(False), Person.id.not_in(mentioned_ids))
            .order_by(Person.search_name_norm)
        )
    )
    return mentioned, others


def proposal_context(
    db: DbSession, request: Request, settings: Any, proposal: Proposal
) -> dict[str, Any]:
    """What inbox/detail.html needs to show a proposal (stored, or a split not saved yet)."""
    card = _card(db, proposal)
    context: dict[str, Any] = {
        "card": card,
        "default_currency": settings.default_currency,
        "form_base": f"/inbox/{proposal.id}",
    }
    if card.capture is not None:
        context["mentioned"], context["others"] = _people_options(db, card.capture)
        context["active_goals"] = goals.active_goals(db)
    if card.split is not None:
        shares = card.split.shares
        chosen = {
            p.id: p
            for p in db.scalars(select(Person).where(Person.id.in_(s.person_id for s in shares)))
        }
        context["split_rows"] = [(chosen[s.person_id], s) for s in shares if s.person_id in chosen]
        if not shares and card.transaction is not None:
            # Nobody picked yet: start with the people from meetings around the payment.
            found = (
                meetings.overlapping(db, card.transaction.occurred_at)
                if card.transaction.occurred_at
                else []
            )
            met = list(dict.fromkeys(p for m in found for p in m.people if not p.archived))
            context["split_rows"] = [(p, SplitShare(person_id=p.id)) for p in met]
            context["split_meetings"] = found
            chosen = {p.id: p for p in met}
        context["recent"] = _recent_people(db, set(chosen))
    if card.payment is not None:
        context.update(_payment_context(db, card.payment, card.transaction, card.payer))
    return context


@router.get("/inbox/{proposal_id:int}/card")
def proposal_card(
    request: Request, db: DbSession, settings: SettingsDep, proposal_id: int
) -> Response:
    """Just the card: a card that is still being read polls this until Claude's answer is in."""
    card = _card(db, inbox.get_proposal(db, proposal_id))
    return templates.TemplateResponse(
        request,
        "inbox/_card.html",
        {"card": card, "default_currency": settings.default_currency},
    )


@router.get("/inbox/{proposal_id:int}")
def proposal_page(
    request: Request, db: DbSession, settings: SettingsDep, proposal_id: int
) -> Response:
    context = proposal_context(db, request, settings, inbox.get_proposal(db, proposal_id))
    return templates.TemplateResponse(request, "inbox/detail.html", context)


# --- Payment: who sent it, what it pays ---


def _payment_context(
    db: DbSession,
    payment: PaymentMatchPayload,
    tx: Transaction | None,
    payer: Person | None,
) -> dict[str, Any]:
    owing_ids = select(Debt.person_id).where(
        Debt.settled_at.is_(None),
        Debt.type == DebtType.MONEY,
        Debt.direction == DebtDirection.THEY_OWE_ME,
    )
    owing = list(
        db.scalars(select(Person).where(Person.id.in_(owing_ids)).order_by(Person.search_name_norm))
    )
    others = list(
        db.scalars(
            select(Person)
            .where(Person.archived.is_(False), Person.id.not_in(owing_ids))
            .order_by(Person.search_name_norm)
        )
    )
    if payer is not None and payer not in owing and payer not in others:
        others.insert(0, payer)  # archived
    remember = []
    if tx is not None:
        for _, value in payments.rememberable(db, tx):
            is_iban = value == tx.counterparty_iban
            remember.append(f"the account {tx.iban_masked}" if is_iban else f"“{value}”")
    option_lines = [
        " + ".join(payments.describe(db, payment.model_copy(update={"settle": option.settle})))
        for option in payment.options
    ]
    selected: int | str = next(
        (i for i, option in enumerate(payment.options) if option.settle == payment.settle),
        "custom",
    )
    return {
        "owing": owing,
        "others": others,
        "remember_what": " and ".join(remember),
        "option_lines": option_lines,
        "option_labels": payments.OPTION_LABELS,
        "selected_option": selected,
        "debts": payments.open_debts(db, payer.id) if payer is not None else [],
        "chosen": {s.debt_id: s.amount_minor for s in payment.settle},
        "settles": payments.settles_total(db, payment) if payment.settle else None,
    }


# --- Split: picking people ---


def _recent_people(db: DbSession, exclude: set[int]) -> list[Person]:
    """People I most recently had money debts with: quick picks for a split."""
    last = func.max(Debt.created_at)
    rows = db.execute(
        select(Debt.person_id, last)
        .where(Debt.type == DebtType.MONEY)
        .group_by(Debt.person_id)
        .order_by(last.desc())
        .limit(RECENT_PEOPLE + len(exclude))
    )
    ids = [pid for pid, _ in rows if pid not in exclude][:RECENT_PEOPLE]
    found = {
        p.id: p
        for p in db.scalars(select(Person).where(Person.id.in_(ids), Person.archived.is_(False)))
    }
    return [found[pid] for pid in ids if pid in found]


@router.get("/split/search")
@router.get("/inbox/{proposal_id:int}/split-search")
def split_search(
    request: Request,
    db: DbSession,
    proposal_id: int | None = None,
    q: str = "",
    listed: Annotated[list[int] | None, Query()] = None,
) -> Response:
    """HTMX fragment: people matching `q` who aren't in the split form yet."""
    exclude = set(listed or ())
    found = (
        search.search_people(db, q, limit=SPLIT_SEARCH_LIMIT + len(exclude)) if q.strip() else []
    )
    people = [p for p in found if p.id not in exclude][:SPLIT_SEARCH_LIMIT]
    return templates.TemplateResponse(
        request,
        "inbox/_split_results.html",
        {"people": people, "q": q},
    )


@router.get("/split/row")
@router.get("/inbox/{proposal_id:int}/split-row")
def split_row(
    request: Request, db: DbSession, person_id: int, proposal_id: int | None = None
) -> Response:
    """HTMX fragment: a picked person's row in the split form."""
    person = db.get(Person, person_id)
    if person is None:
        return Response(status_code=204)
    return templates.TemplateResponse(
        request,
        "inbox/_split_row.html",
        {"person": person, "share": SplitShare(person_id=person.id), "currency": None},
    )


@router.post("/split/{transaction_id:int}/preview")
async def split_preview(request: Request, db: DbSession, transaction_id: int) -> Response:
    """HTMX fragment: who would owe what with the split form as it is now (nothing saved)."""
    form = await request.form()
    try:
        payload = merge_split_form(db, SplitPayload(transaction_id=transaction_id), form)
        lines = split.describe(db, payload) if payload.shares else []
        problem = None
    except InvalidInputError as exc:
        lines, problem = [], str(exc)
    return templates.TemplateResponse(
        request, "inbox/_split_preview.html", {"lines": lines, "problem": problem}
    )


# --- Form -> payload ---


def _text(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else None


def _date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidInputError(f"“{value}” isn't a date.") from exc


def _datetime(value: str) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise InvalidInputError(f"“{value}” isn't a date and time.") from exc


def merge_capture_form(
    payload: CapturePayload, form: FormData, default_currency: str
) -> CapturePayload:
    """Apply the submitted fields over the stored payload. Only fields present in the form
    change; `full=1` marks the complete edit form, where an unticked checkbox excludes an item."""
    full = form.get("full") == "1"
    data = payload.model_dump(mode="json")

    def put(section: str, index: int, name: str, value: Any) -> None:
        data[section][index][name] = value

    for i, _ in enumerate(payload.new_people):
        for name in ("name", "action"):
            if (value := _text(form, f"new-{i}-{name}")) is not None:
                put("new_people", i, name, value)
        if (value := _text(form, f"new-{i}-person_id")) is not None:
            put("new_people", i, "person_id", int(value) if value.isdigit() else None)

    sections = {
        "note": ("notes", ("person", "text")),
        "interaction": ("interactions", ("kind", "summary", "occurred_on")),
        "debt": ("debts", ("person", "direction", "type", "currency", "description")),
        "promise": ("promises", ("person", "direction", "text", "due", "goal")),
        "appointment": ("appointments", ("person", "title", "start", "end", "location")),
        "settle": ("settles", ()),
        "pupdate": ("promise_updates", ("status",)),
    }
    for prefix, (section, names) in sections.items():
        for i, _ in enumerate(data[section]):
            key = f"{prefix}-{i}"
            if full:
                put(section, i, "include", form.get(f"{key}-include") == "1")
            for name in names:
                value = _text(form, f"{key}-{name}")
                if value is None:
                    continue
                if name in ("occurred_on", "due"):
                    put(section, i, name, _date(value))
                elif name in ("start", "end"):
                    put(section, i, name, _datetime(value))
                elif name == "title":
                    put(section, i, name, value)
                elif name == "currency":
                    put(section, i, name, value.upper() or None)
                elif name == "text":
                    put(section, i, name, value)
                elif name == "goal":
                    put(section, i, name, int(value) if value.isdigit() else None)
                else:
                    put(section, i, name, value or None)
            if prefix == "interaction" and full:
                put(section, i, "people", [v for v in form.getlist(f"{key}-people") if v])
            if prefix == "settle":
                part = _text(form, f"{key}-amount")
                if part is not None:
                    draft = data[section][i]
                    try:
                        draft["amount_minor"] = (
                            parse_amount(part, draft["currency"] or default_currency)
                            if part
                            else None
                        )
                    except ValueError as exc:
                        raise InvalidInputError(str(exc)) from exc
            if prefix == "debt":
                debt = data[section][i]
                if debt["type"] != "money":
                    debt["amount_minor"] = debt["currency"] = None
                elif (amount := _text(form, f"{key}-amount")) is not None:
                    currency = debt["currency"] = debt["currency"] or default_currency
                    if not amount:
                        debt["amount_minor"] = None
                    else:
                        try:
                            debt["amount_minor"] = parse_amount(amount, currency)
                        except ValueError as exc:
                            raise InvalidInputError(str(exc)) from exc
    try:
        return CapturePayload.model_validate(data)
    except ValidationError as exc:
        raise InvalidInputError("Please check the highlighted fields.") from exc


def merge_split_form(db: DbSession, payload: SplitPayload, form: FormData) -> SplitPayload:
    """The split form: checked people (in order), mode, my share, custom amounts, description."""
    tx = split.get_transaction(db, payload.transaction_id)
    shares: list[SplitShare] = []
    for value in form.getlist("person"):
        if not isinstance(value, str) or not value.isdigit():
            continue
        person_id = int(value)
        if any(s.person_id == person_id for s in shares):
            continue
        amount = None
        if text := _text(form, f"amount-{person_id}"):
            try:
                amount = parse_amount(text, tx.currency)
            except ValueError as exc:
                raise InvalidInputError(str(exc)) from exc
            if amount == 0:
                raise InvalidInputError("Amounts must be above zero.")
        shares.append(SplitShare(person_id=person_id, amount_minor=amount))
    mode = form.get("mode")
    return SplitPayload(
        transaction_id=payload.transaction_id,
        mode="custom" if mode == "custom" else "even" if mode == "even" else payload.mode,
        include_me=form.get("include_me") == "1",
        shares=shares,
        description=_text(form, "description") or None,
    )


def merge_payment_form(
    db: DbSession, payload: PaymentMatchPayload, form: FormData
) -> PaymentMatchPayload:
    """The payment form: who sent it (a different person starts over with their options),
    whether to remember their bank details, and a ranked option or debts picked by hand."""
    value = _text(form, "person")
    person_id = int(value) if value and value.isdigit() else None
    remember = form.get("remember") == "1"
    if person_id != payload.person_id:
        fresh = payments.rematch(db, payload, person_id)
        return fresh.model_copy(update={"remember_payer": remember and fresh.remember_payer})
    settle = payload.settle
    option = form.get("option")
    if isinstance(option, str) and option.isdigit() and int(option) < len(payload.options):
        settle = payload.options[int(option)].settle
    elif option == "custom":
        tx = split.get_transaction(db, payload.transaction_id)
        settle = []
        for item in form.getlist("debt"):
            if not isinstance(item, str) or not item.isdigit():
                continue
            debt_id = int(item)
            if any(s.debt_id == debt_id for s in settle):
                continue
            amount = None
            if text := _text(form, f"amount-{debt_id}"):
                try:
                    amount = parse_amount(text, tx.currency)
                except ValueError as exc:
                    raise InvalidInputError(str(exc)) from exc
                if amount == 0:
                    raise InvalidInputError("Amounts must be above zero.")
            settle.append(DebtSettle(debt_id=debt_id, amount_minor=amount))
    return payload.model_copy(update={"remember_payer": remember, "settle": settle})


def _edited_payload(
    db: DbSession, proposal: Proposal, form: FormData, default_currency: str
) -> dict[str, Any] | None:
    if proposal.kind is ProposalKind.PAYMENT_MATCH:
        if form.get("full") != "1":
            return None  # the Inbox card's buttons
        payment = PaymentMatchPayload.model_validate(proposal.payload_json)
        return merge_payment_form(db, payment, form).model_dump(mode="json")
    if proposal.kind is ProposalKind.SPLIT_TRANSACTION:
        if form.get("full") != "1":
            return None  # the Inbox card's buttons: no form fields to apply
        current = SplitPayload.model_validate(proposal.payload_json)
        return merge_split_form(db, current, form).model_dump(mode="json")
    if proposal.kind is ProposalKind.CAPTURE:
        stored = CapturePayload.model_validate(proposal.payload_json)
        merged = merge_capture_form(stored, form, default_currency)
        return merged.model_dump(mode="json")
    if proposal.kind is ProposalKind.NEW_PERSON:
        data = dict(proposal.payload_json)
        for name in ("name", "context"):
            if (value := _text(form, name)) is not None:
                data[name] = value or None
        return data
    return None


# "+ Debt" etc. on the capture form: something Claude missed, filled in by hand.
BLANK_ITEMS: dict[str, tuple[str, dict[str, Any]]] = {
    "debt": ("debts", {"person": None, "direction": "they_owe_me", "type": "money"}),
    "promise": ("promises", {"person": None, "direction": "i_promised", "text": ""}),
    "appointment": ("appointments", {"person": None, "title": "", "start": ""}),
    "note": ("notes", {"person": None, "text": ""}),
    "interaction": ("interactions", {"people": [], "kind": "meet"}),
}


def _detail_url(proposal: Proposal) -> str:
    return f"/inbox/{proposal.id}"


@router.post("/inbox/{proposal_id:int}/save")
async def proposal_save(
    request: Request, db: DbSession, settings: SettingsDep, proposal_id: int
) -> Response:
    proposal = inbox.get_proposal(db, proposal_id)
    form = await request.form()
    added = form.get("add")
    try:
        payload = _edited_payload(db, proposal, form, settings.default_currency)
        if payload is not None and proposal.kind is ProposalKind.CAPTURE and added in BLANK_ITEMS:
            section, blank = BLANK_ITEMS[str(added)]
            item = dict(blank)
            if added == "appointment":  # tomorrow morning, to be edited
                tomorrow = today_local(request) + timedelta(days=1)
                item["start"] = datetime.combine(tomorrow, time(9)).isoformat()
            payload[section].append(item)
        if payload is not None:
            inbox.update_payload(db, proposal, payload)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(_detail_url(proposal), status_code=303)
    db.commit()
    if added in BLANK_ITEMS:
        return RedirectResponse(f"{_detail_url(proposal)}#{added}-new", status_code=303)
    flash(request, "Saved your changes.")
    return RedirectResponse(_detail_url(proposal), status_code=303)


@router.post("/inbox/{proposal_id:int}/confirm")
async def proposal_confirm(
    request: Request, db: DbSession, settings: SettingsDep, proposal_id: int
) -> Response:
    proposal = inbox.get_proposal(db, proposal_id)
    if proposal.status is not ProposalStatus.PENDING:
        flash(request, f"Already {proposal.status.value}.", "error")
        return RedirectResponse("/inbox", status_code=303)
    form = await request.form()
    payer_before = proposal.payload_json.get("person_id")
    try:
        payload = _edited_payload(db, proposal, form, settings.default_currency)
        if payload is not None:
            # Keep the edits even if confirming fails below (e.g. an amount still missing).
            inbox.update_payload(db, proposal, payload)
            db.commit()
            if proposal.kind is ProposalKind.PAYMENT_MATCH and payload["person_id"] != payer_before:
                # Their debts weren't on the page yet: show them before anything is settled.
                flash(request, "Now check what the payment settles.")
                return RedirectResponse(_detail_url(proposal), status_code=303)
        result = inbox.confirm(
            db, proposal, today=today_local(request), default_currency=settings.default_currency
        )
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(_detail_url(proposal), status_code=303)
    db.commit()
    link = f"/people/{result.person_id}" if result.person_id else None
    flash(request, result.message, link=link, link_label="Open" if link else None)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/{proposal_id:int}/reject")
def proposal_reject(request: Request, db: DbSession, proposal_id: int) -> Response:
    proposal = inbox.get_proposal(db, proposal_id)
    try:
        inbox.reject(db, proposal)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/inbox", status_code=303)
    db.commit()
    flash(request, "Rejected.")
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/{proposal_id:int}/reject-group")
def proposal_reject_group(request: Request, db: DbSession, proposal_id: int) -> Response:
    """ "Reject all N from this capture": every pending card of the same dictation."""
    proposal = inbox.get_proposal(db, proposal_id)
    try:
        count = inbox.reject_group(db, proposal)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/inbox", status_code=303)
    db.commit()
    flash(request, f"Rejected {count} from this capture.")
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/{proposal_id:int}/reopen")
def proposal_reopen(request: Request, db: DbSession, proposal_id: int) -> Response:
    """Undo a reject (a mis-tapped "Not a debt"): back in the Inbox as it was."""
    proposal = inbox.get_proposal(db, proposal_id)
    try:
        inbox.reopen(db, proposal)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse("/inbox", status_code=303)
    db.commit()
    flash(request, "Reopened.")
    return RedirectResponse(_detail_url(proposal), status_code=303)
