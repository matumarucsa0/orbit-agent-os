"""Incoming bank payments -> `payment_match` proposals: who paid, and which debts it settles.

Nothing is settled here. A proposal lists ranked options, and debts are settled only when I
confirm it: one tap on the push's Settle button, or in the Inbox. Callers commit.

Payer: the IBAN handle, else the `bank_name` handle, else reference codes in the payment whose
debts all belong to one person, else a unique fuzzy name match (first + last name, or initial +
last name). Unknown payers still get a proposal: I pick the person, and confirming remembers the
bank name / IBAN as their handles, so their next payment is recognized.

Options come from the payer's open `they_owe_me` money debts in the payment's currency, oldest
first: the debts named by reference code -> one debt of the exact amount -> up to 4 debts adding
up to it -> otherwise all of them in order (the last one in part, or with money left over
when it's less than double what they owe).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from enum import StrEnum
from itertools import combinations
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.bank import Transaction
from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    HandleType,
    ProposalKind,
    ProposalStatus,
    TransactionDirection,
)
from orbit.domain.inbox import Notification, Proposal
from orbit.domain.ledger import Debt
from orbit.domain.money import format_amount
from orbit.domain.normalize import normalize_text
from orbit.domain.people import Handle, Person
from orbit.integrations.ntfy import Action, PushMessage, PushSender, http_action, view_action
from orbit.services import ledger, notifications, people
from orbit.services.action_tokens import action_url
from orbit.services.errors import InvalidInputError
from orbit.services.proposal_kinds import Applied, ApplyContext, ProposalHandler
from orbit.services.split import SOURCE_TYPE, check_reopen, get_transaction, open_proposal

MAX_SUBSET = 4  # debts one payment may add up to
SUBSET_POOL = 20  # oldest open debts tried for sums: C(20, 4) = 4845 combinations at most
MAX_OPTIONS = 5
FUZZY_RATIO = 0.85
FUZZY_MIN_LENGTH = 5
PUSH_KIND = "payment_match"
PUSH_LISTED = 3  # unknown senders named in a grouped push
# "D-0142", "d0142", "D 0142" in the (normalized) message; codes always have 4+ digits.
_REFERENCE_RE = re.compile(r"(?<![a-z0-9])d[-\s]?(\d{4,})(?![0-9])")
_WORD_RE = re.compile(r"[a-z0-9]+")


class PayerSource(StrEnum):
    """How the payer was recognized."""

    IBAN = "iban"
    BANK_NAME = "bank_name"
    REFERENCE = "reference"
    NAME = "name"
    MANUAL = "manual"


HANDLE_SOURCES = frozenset({PayerSource.IBAN, PayerSource.BANK_NAME})

OptionKind = Literal["reference", "exact", "subset", "all", "partial", "overpaid"]
OPTION_LABELS: dict[str, str] = {
    "reference": "Reference code",
    "exact": "Exact amount",
    "subset": "Adds up",
    "all": "All open debts",
    "partial": "Part payment, oldest first",
    "overpaid": "All open debts (paid more)",
}


class DebtSettle(BaseModel):
    debt_id: int
    amount_minor: int | None = Field(default=None, gt=0)  # None: in full


class MatchOption(BaseModel):
    kind: OptionKind
    settle: list[DebtSettle]


class PaymentMatchPayload(BaseModel):
    transaction_id: int
    person_id: int | None = None  # None: unknown payer, to be picked
    payer_source: PayerSource | None = None
    # On confirm, save the payment's bank name / IBAN as the person's handles (if nobody has them).
    remember_payer: bool = False
    options: list[MatchOption] = Field(default_factory=list)  # ranked, best first
    settle: list[DebtSettle] = Field(default_factory=list)  # what confirming settles


@dataclass(frozen=True)
class Payer:
    person: Person
    source: PayerSource


@dataclass(frozen=True)
class PlannedSettle:
    debt: Debt
    amount_minor: int | None  # None: in full


@dataclass(frozen=True)
class Plan:
    tx: Transaction
    person: Person | None
    items: list[PlannedSettle]


@dataclass(frozen=True)
class MatchResult:
    person: Person
    settled: list[ledger.SettleResult]
    remembered: list[Handle]
    refreshed: int  # other pending payments from unknown senders that now match someone


# --- Payer ---


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize_text(text))


def _same(word: str, name: str) -> bool:
    if word == name:
        return True
    return (
        min(len(word), len(name)) >= FUZZY_MIN_LENGTH
        and SequenceMatcher(None, word, name).ratio() >= FUZZY_RATIO
    )


def _name_pairs(person: Person) -> set[tuple[str, str]]:
    """(first, last) name words a bank could print for this person; aliases count as first names."""
    firsts: set[str] = set()
    lasts: set[str] = set()
    first = _words(person.first_name or "")
    last = _words(person.last_name or "")
    if first and last:
        firsts.add(first[0])
        lasts.add(last[-1])
    display = _words(person.display_name)
    if len(display) >= 2:
        firsts.add(display[0])
        lasts.add(display[-1])
    for alias in person.aliases:
        words = _words(alias.value)
        if len(words) == 1:
            firsts.add(words[0])
    return {(f, la) for f in firsts for la in lasts if f != la}


def _name_score(words: Sequence[str], person: Person) -> int:
    """2: first and last name both there; 1: last name and the first name's initial; 0: no."""
    best = 0
    for first, last in _name_pairs(person):
        at = {i for i, w in enumerate(words) if _same(w, last)}
        if not at:
            continue
        rest = [w for i, w in enumerate(words) if i not in at]
        if any(_same(w, first) for w in rest):
            return 2
        if any(w == first[0] for w in rest if len(w) == 1):
            best = 1
    return best


def match_payer_name(session: Session, name: str) -> Person | None:
    """The one person a bank's name for the payer ("VESELY PATRIK", "P. Veselý") points to."""
    words = _words(name)
    if len(words) < 2:
        return None
    scored: list[tuple[int, Person]] = []
    everyone = session.scalars(
        select(Person).where(Person.archived.is_(False)).options(selectinload(Person.aliases))
    )
    for person in everyone:
        if score := _name_score(words, person):
            scored.append((score, person))
    if not scored:
        return None
    top = max(score for score, _ in scored)
    best = [person for score, person in scored if score == top]
    return best[0] if len(best) == 1 else None


def reference_codes(tx: Transaction) -> list[str]:
    """Debt reference codes written in the payment ("D-0142"), in order, deduplicated."""
    text = " ".join(p for p in (tx.message, tx.counterparty, tx.bank_reference) if p)
    codes: list[str] = []
    for match in _REFERENCE_RE.finditer(normalize_text(text)):
        code = f"{ledger.REFERENCE_PREFIX}{int(match.group(1)):04d}"
        if code not in codes:
            codes.append(code)
    return codes


def _payable(debt: Debt) -> bool:
    return (
        debt.is_open and debt.type is DebtType.MONEY and debt.direction is DebtDirection.THEY_OWE_ME
    )


def referenced_debts(session: Session, tx: Transaction) -> list[Debt]:
    """Open money debts owed to me that the payment names by code. A code partly paid since
    points to what's left of it (the remainder has a code of its own)."""
    codes = reference_codes(tx)
    if not codes:
        return []
    found: list[Debt] = []
    query = select(Debt).where(Debt.reference_code.in_(codes))
    by_code = {d.reference_code: d for d in session.scalars(query)}
    for code in codes:
        debt = by_code.get(code)
        while debt is not None and not debt.is_open:
            debt = debt.remainder
        if debt is not None and _payable(debt) and debt not in found:
            found.append(debt)
    return found


def identify_payer(session: Session, tx: Transaction) -> Payer | None:
    """Work out who sent an incoming payment (an IBAN handle match first); None when nobody fits."""
    if tx.counterparty_iban:
        owner = people.find_person_by_handle(session, HandleType.IBAN, tx.counterparty_iban, None)
        if owner is not None:
            return Payer(owner, PayerSource.IBAN)
    if tx.counterparty:
        owner = people.find_person_by_handle(session, HandleType.BANK_NAME, tx.counterparty, None)
        if owner is not None:
            return Payer(owner, PayerSource.BANK_NAME)
    owners = {d.person_id: d.person for d in referenced_debts(session, tx)}
    if len(owners) == 1:
        return Payer(next(iter(owners.values())), PayerSource.REFERENCE)
    if tx.counterparty:
        person = match_payer_name(session, tx.counterparty)
        if person is not None:
            return Payer(person, PayerSource.NAME)
    return None


def rememberable(session: Session, tx: Transaction) -> list[tuple[HandleType, str]]:
    """The payment's IBAN and bank name, where nobody has them as a handle yet."""
    found: list[tuple[HandleType, str]] = []
    for handle_type, value in (
        (HandleType.IBAN, tx.counterparty_iban),
        (HandleType.BANK_NAME, tx.counterparty),
    ):
        if value and people.find_person_by_handle(session, handle_type, value, None) is None:
            found.append((handle_type, value))
    return found


# --- Options ---


def open_debts(session: Session, person_id: int, currency: str | None = None) -> list[Debt]:
    """Money this person owes me, oldest first (optionally in one currency)."""
    query = select(Debt).where(
        Debt.person_id == person_id,
        Debt.settled_at.is_(None),
        Debt.type == DebtType.MONEY,
        Debt.direction == DebtDirection.THEY_OWE_ME,
    )
    if currency is not None:
        query = query.where(Debt.currency == currency)
    return list(session.scalars(query.order_by(Debt.created_at, Debt.id)))


def _cover(debts: Sequence[Debt], amount: int) -> list[DebtSettle]:
    """Settle debts in order with `amount`: whole ones while it lasts, the next one in part."""
    settle: list[DebtSettle] = []
    left = amount
    for debt in debts:
        if left <= 0:
            break
        owed = debt.amount_minor or 0
        if left >= owed:
            settle.append(DebtSettle(debt_id=debt.id))
            left -= owed
        else:
            settle.append(DebtSettle(debt_id=debt.id, amount_minor=left))
            left = 0
    return settle


def match_options(session: Session, tx: Transaction, person: Person) -> list[MatchOption]:
    """The ways this payment could settle the person's open debts in the payment's currency, for the
    `payment_match` proposal."""
    debts = open_debts(session, person.id, tx.currency)
    amount = tx.amount_minor
    options: list[MatchOption] = []

    def add(kind: OptionKind, settle: list[DebtSettle]) -> None:
        if settle and len(options) < MAX_OPTIONS and all(o.settle != settle for o in options):
            options.append(MatchOption(kind=kind, settle=settle))

    referenced = [
        d
        for d in referenced_debts(session, tx)
        if d.person_id == person.id and d.currency == tx.currency
    ]
    if referenced:
        add("reference", _cover(referenced, amount))
    for debt in debts:
        if debt.amount_minor == amount:
            add("exact", [DebtSettle(debt_id=debt.id)])
    pool = debts[:SUBSET_POOL]
    for size in range(2, MAX_SUBSET + 1):
        for combo in combinations(pool, size):
            if len(options) >= MAX_OPTIONS:
                break
            if sum(d.amount_minor or 0 for d in combo) == amount:
                add("subset", [DebtSettle(debt_id=d.id) for d in combo])
    total = sum(d.amount_minor or 0 for d in debts)
    # Paid more than owed: rounding up, not a rent payment that happens to come from them.
    if debts and amount < 2 * total and not any(o.kind in ("exact", "subset") for o in options):
        kind: OptionKind = "partial" if amount < total else "all" if amount == total else "overpaid"
        add(kind, _cover(debts, amount))
    return options


def build_payload(session: Session, tx: Transaction, payer: Payer | None) -> PaymentMatchPayload:
    """A fresh payload: the payer (if known), ranked options, and the best one pre-selected."""
    remember = bool(rememberable(session, tx))
    if payer is None:
        return PaymentMatchPayload(transaction_id=tx.id, remember_payer=remember)
    options = match_options(session, tx, payer.person)
    return PaymentMatchPayload(
        transaction_id=tx.id,
        person_id=payer.person.id,
        payer_source=payer.source,
        remember_payer=remember,
        options=options,
        settle=options[0].settle if options else [],
    )


def rematch(
    session: Session, payload: PaymentMatchPayload, person_id: int | None
) -> PaymentMatchPayload:
    """A fresh payload after I pick (or clear) the payer myself: options for that person."""
    tx = get_transaction(session, payload.transaction_id)
    if person_id is None:
        return build_payload(session, tx, None)
    person = people.get_person(session, person_id)
    return build_payload(session, tx, Payer(person, PayerSource.MANUAL))


# --- Describing ---


def debt_label(debt: Debt) -> str:
    """ "pizza 12.00 €" (or the reference code when there's no description)."""
    name = debt.description or debt.reference_code or "debt"
    return f"{name} {format_amount(debt.amount_minor or 0, debt.currency or '')}"


def _settle_line(item: PlannedSettle) -> str:
    if item.amount_minor is None:
        return debt_label(item.debt)
    part = format_amount(item.amount_minor, item.debt.currency or "")
    return f"{part} of {debt_label(item.debt)}"


def plan(session: Session, payload: PaymentMatchPayload) -> Plan:
    """Validate the payload against the current ledger. Raises InvalidInputError."""
    tx = get_transaction(session, payload.transaction_id)
    if tx.direction is not TransactionDirection.IN:
        raise InvalidInputError("Only incoming payments settle debts.")
    person = None
    if payload.person_id is not None:
        person = session.get(Person, payload.person_id)
        if person is None:
            raise InvalidInputError(f"Person {payload.person_id} no longer exists.")
    items: list[PlannedSettle] = []
    for choice in payload.settle:
        if person is None:
            raise InvalidInputError("Pick who sent this payment first.")
        if any(item.debt.id == choice.debt_id for item in items):
            raise InvalidInputError("Each debt can be settled only once.")
        debt = session.get(Debt, choice.debt_id)
        if debt is None:
            raise InvalidInputError(f"Debt {choice.debt_id} no longer exists.")
        code = debt.reference_code or f"Debt {debt.id}"
        if debt.person_id != person.id:
            raise InvalidInputError(f"{code} isn't {person.display_name}'s debt.")
        if not debt.is_open:
            raise InvalidInputError(f"{code} was already settled.")
        if not _payable(debt):
            raise InvalidInputError(f"{code} isn't money {person.display_name} owes you.")
        amount = choice.amount_minor
        if amount is not None and amount >= (debt.amount_minor or 0):
            if amount > (debt.amount_minor or 0):
                raise InvalidInputError(f"That's more than {code} is for; settle it in full.")
            amount = None
        if amount is not None and debt.currency != tx.currency:
            raise InvalidInputError(
                f"{code} is in {debt.currency}: settle it in full or not at all."
            )
        items.append(PlannedSettle(debt, amount))
    return Plan(tx, person, items)


def describe(session: Session, payload: PaymentMatchPayload) -> list[str]:
    """What confirming would do, a line per debt, or why it can't be applied yet."""
    try:
        planned = plan(session, payload)
    except InvalidInputError as exc:
        return [str(exc)]
    if planned.person is None:
        return ["Who sent it? Pick a person."]
    lines = [_settle_line(item) for item in planned.items]
    if not lines:
        return [_nothing(planned.tx, payload)]
    return lines


def settles_total(session: Session, payload: PaymentMatchPayload) -> int | None:
    """How much of the payment's currency the chosen debts settle, if they can be applied."""
    try:
        planned = plan(session, payload)
    except InvalidInputError:
        return None
    return sum(
        item.amount_minor if item.amount_minor is not None else item.debt.amount_minor or 0
        for item in planned.items
        if item.debt.currency == planned.tx.currency
    )


def _nothing(tx: Transaction, payload: PaymentMatchPayload) -> str:
    if payload.options:
        return "Nothing picked to settle."
    return f"No open {tx.currency} debts match."


def proposal_summary(session: Session, payload: PaymentMatchPayload) -> str:
    """One-line Inbox summary of a `payment_match` payload."""
    tx = get_transaction(session, payload.transaction_id)
    amount = format_amount(tx.amount_minor, tx.currency)
    person = session.get(Person, payload.person_id) if payload.person_id is not None else None
    if person is None:
        return f"Who sent {amount}? “{tx.label}”"
    head = f"Received {amount} from {person.display_name}"
    try:
        items = plan(session, payload).items
    except InvalidInputError:
        items = []
    if not items:
        return f"{head} — {_nothing(tx, payload)}"
    return f"{head} — " + " + ".join(_settle_line(item) for item in items)


# --- Proposals ---


def propose_match(session: Session, tx: Transaction, *, force: bool = False) -> Proposal | None:
    """A `payment_match` proposal for an incoming payment (reuses an open one).

    A known payer who owes me no money gets none (there's nothing to settle) unless `force`:
    I asked for it myself from the Import page.
    """
    if tx.direction is not TransactionDirection.IN:
        raise InvalidInputError("Only incoming payments settle debts.")
    existing = open_proposal(session, tx, ProposalKind.PAYMENT_MATCH)
    if existing is not None:
        return existing
    payer = identify_payer(session, tx)
    if payer is not None and not force and not open_debts(session, payer.person.id):
        return None
    proposal = _new_proposal(session, tx, payer)
    session.add(proposal)
    session.flush()
    return proposal


def _new_proposal(session: Session, tx: Transaction, payer: Payer | None) -> Proposal:
    payload = build_payload(session, tx, payer)
    return Proposal(
        kind=ProposalKind.PAYMENT_MATCH,
        status=ProposalStatus.PENDING,
        summary=proposal_summary(session, payload),
        payload_json=payload.model_dump(mode="json"),
        source_type=SOURCE_TYPE,
        source_id=str(tx.id),
    )


def draft_match(session: Session, tx: Transaction) -> Proposal:
    """A new, unsaved `payment_match` proposal for an incoming payment (for the payment form
    before anything is stored)."""
    if tx.direction is not TransactionDirection.IN:
        raise InvalidInputError("Only incoming payments settle debts.")
    return _new_proposal(session, tx, identify_payer(session, tx))


def refresh_unknown(session: Session) -> int:
    """Pending proposals from unknown senders that now match someone (e.g. after I linked
    their IBAN) get that payer and fresh options. Returns how many changed."""
    changed = 0
    pending = session.scalars(
        select(Proposal).where(
            Proposal.kind == ProposalKind.PAYMENT_MATCH,
            Proposal.status == ProposalStatus.PENDING,
        )
    )
    for proposal in list(pending):
        payload = PaymentMatchPayload.model_validate(proposal.payload_json)
        tx = session.get(Transaction, payload.transaction_id)
        if payload.person_id is not None or tx is None:
            continue
        payer = identify_payer(session, tx)
        if payer is None:
            continue
        fresh = build_payload(session, tx, payer)
        proposal.payload_json = fresh.model_dump(mode="json")
        proposal.summary = proposal_summary(session, fresh)
        changed += 1
    session.flush()
    return changed


def apply_match(
    session: Session, payload: PaymentMatchPayload, now: datetime | None = None
) -> MatchResult:
    """Settle the chosen debts (linked to the payment) and remember the payer's bank details."""
    planned = plan(session, payload)
    if planned.person is None:
        raise InvalidInputError("Pick who sent this payment first.")
    handles = rememberable(session, planned.tx) if payload.remember_payer else []
    if not planned.items and not handles:
        raise InvalidInputError(
            "Nothing to settle: pick the debts it pays, or reject it if it isn't a debt."
        )
    settled: list[ledger.SettleResult] = []
    for item in planned.items:
        result = ledger.settle(session, item.debt, item.amount_minor, now)
        item.debt.settled_by_transaction_id = planned.tx.id
        settled.append(result)
    remembered = [
        people.add_handle(session, planned.person, handle_type, value)
        for handle_type, value in handles
    ]
    refreshed = refresh_unknown(session) if remembered else 0
    session.flush()
    return MatchResult(planned.person, settled, remembered, refreshed)


def result_message(result: MatchResult) -> str:
    """ "Settled 2 debts from Patrik: pizza 12.00 €, kino 12.00 €." and what was remembered."""
    name = result.person.display_name
    parts: list[str] = []
    if result.settled:
        lines = []
        for item in result.settled:
            debt = item.settled
            if item.remainder is None:
                lines.append(debt_label(debt))
            else:
                paid = ledger.paid_amount(debt) or 0
                rest = format_amount(item.remainder.amount_minor or 0, debt.currency or "")
                lines.append(
                    f"{format_amount(paid, debt.currency or '')} of {debt_label(debt)}"
                    f" ({rest} still open as {item.remainder.reference_code})"
                )
        count = len(result.settled)
        noun = "debt" if count == 1 else "debts"
        parts.append(f"Settled {count} {noun} from {name}: " + "; ".join(lines) + ".")
    if result.remembered:
        what = " and ".join(
            "the IBAN" if h.type is HandleType.IBAN else f"“{h.value}”" for h in result.remembered
        )
        parts.append(f"{name} will be recognized by {what} from now on.")
    if result.refreshed:
        more = result.refreshed
        noun = "payment" if more == 1 else "payments"
        parts.append(f"{more} more {noun} in the Inbox matched too.")
    return " ".join(parts)


# --- Pushes ---


def _unknown_label(tx: Transaction) -> str:
    return f"{tx.label} {format_amount(tx.amount_minor, tx.currency)}"


def payment_push(
    session: Session, proposal: Proposal, *, base_url: str, secret: str, now: datetime | None = None
) -> PushMessage:
    """ "Received 24.00 € from Patrik" + [Settle] [Choose…] [Not a debt] (or who sent it)."""
    payload = PaymentMatchPayload.model_validate(proposal.payload_json)
    tx = get_transaction(session, payload.transaction_id)
    url = f"{base_url}/inbox/{proposal.id}"
    amount = format_amount(tx.amount_minor, tx.currency)
    reject = http_action("Not a debt", action_url(base_url, secret, proposal.id, "reject", now))
    person = session.get(Person, payload.person_id) if payload.person_id is not None else None
    if person is None:
        body = f"“{tx.label}”" + (f" · {tx.message}" if tx.message and tx.counterparty else "")
        return PushMessage(
            title=f"💶 Who sent {amount}?",
            body=body,
            click=url,
            tags=("moneybag",),
            actions=(view_action("Who is it?", url), reject),
        )
    parts: list[str] = []
    if tx.message:
        parts.append(f"“{tx.message}”")
    if payload.payer_source not in HANDLE_SOURCES:
        parts.append(f"from “{tx.label}”")  # how the bank named them: check the match
    lines = describe(session, payload)
    parts.append(("Matches: " + " + ".join(lines)) if payload.settle else lines[0])
    actions: list[Action] = []
    if payload.settle:
        settle_url = action_url(base_url, secret, proposal.id, "confirm", now)
        actions.append(http_action("Settle", settle_url))
    actions += [view_action("Choose…", url), reject]
    return PushMessage(
        title=f"💶 Received {amount} from {person.display_name}",
        body=" · ".join(parts),
        click=url,
        tags=("moneybag",),
        actions=tuple(actions),
    )


def unknown_senders_push(
    session: Session, proposals: Sequence[Proposal], base_url: str
) -> PushMessage:
    """Several payments from unknown senders in one import: one push listing a few."""
    labels = []
    for proposal in proposals[:PUSH_LISTED]:
        tx = session.get(Transaction, int(proposal.source_id or 0))
        if tx is not None:
            labels.append(_unknown_label(tx))
    more = len(proposals) - len(labels)
    url = f"{base_url}/inbox"
    return PushMessage(
        title=f"💶 {len(proposals)} payments from unknown senders",
        body=", ".join(labels) + (f" and {more} more" if more > 0 else "") + ". Who are they?",
        click=url,
        tags=("moneybag",),
        actions=(view_action("Open Inbox", url),),
    )


def notify_payments(
    session: Session,
    sender: PushSender | None,
    proposals: Sequence[Proposal],
    *,
    base_url: str,
    secret: str,
    quiet: notifications.QuietHours,
    import_id: int,
    now: datetime | None = None,
) -> list[Notification]:
    """A push per payment from a known sender; the unknown ones together, one push per import."""
    sent: list[Notification] = []
    unknown: list[Proposal] = []

    def notify(message: PushMessage, dedupe_key: str) -> None:
        record = notifications.notify(
            session, sender, message, dedupe_key=dedupe_key, kind=PUSH_KIND, quiet=quiet, now=now
        )
        if record is not None:
            sent.append(record)

    for proposal in proposals:
        if proposal.status is not ProposalStatus.PENDING:
            continue
        if PaymentMatchPayload.model_validate(proposal.payload_json).person_id is None:
            unknown.append(proposal)
            continue
        push = payment_push(session, proposal, base_url=base_url, secret=secret, now=now)
        notify(push, f"payment_match:{proposal.id}")
    if len(unknown) == 1:
        push = payment_push(session, unknown[0], base_url=base_url, secret=secret, now=now)
        notify(push, f"payment_match:{unknown[0].id}")
    elif unknown:
        notify(unknown_senders_push(session, unknown, base_url), f"bank_import:{import_id}:senders")
    return sent


def _confirm(session: Session, payload: PaymentMatchPayload, ctx: ApplyContext) -> Applied:
    match = apply_match(session, payload, now=ctx.now)
    return Applied(result_message(match), match.person.id)


HANDLER = ProposalHandler(
    ProposalKind.PAYMENT_MATCH,
    PaymentMatchPayload,
    _confirm,
    summary=proposal_summary,
    check_reopen=check_reopen,
)
