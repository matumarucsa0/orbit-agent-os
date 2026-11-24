"""Bank statement import: parse, dedupe, store, classify, propose splits and payment matches.

Nothing is settled or split here; it all waits in the Inbox. Callers commit.

Dedupe: each row's hash covers bank, booking date, amount, currency, direction, normalized
counterparty, bank reference and counterparty IBAN, so re-importing an overlapping file adds
nothing. Rows that are identical in all of these (two same-day coffees at the same café) get an
occurrence number within the file, so both are kept and an overlapping file still matches them.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.bank import BankImport, Transaction
from orbit.domain.enums import Bank, Classification, TransactionDirection
from orbit.domain.inbox import Notification, Proposal
from orbit.domain.money import format_amount
from orbit.domain.normalize import mask_iban, normalize_text
from orbit.integrations.bank_parsers import BankFormatError, ParsedTransaction, parse_statement
from orbit.integrations.claude import TransactionClassifier
from orbit.integrations.ecb import RateSource
from orbit.integrations.ntfy import PushMessage, PushSender, view_action
from orbit.services import meetings, notifications
from orbit.services.classify import ClassifyContext, classify
from orbit.services.errors import InvalidInputError
from orbit.services.payments import notify_payments, propose_match
from orbit.services.split import propose_split

HASH_LOOKUP_CHUNK = 500
PUSH_KIND = "split_transaction"
PUSH_LISTED = 3  # payments named in the push body


def transaction_hash(bank: Bank, parsed: ParsedTransaction, occurrence: int = 1) -> str:
    """The dedupe hash of a parsed row (bank, date, amount, currency, direction, counterparty,
    reference, IBAN, plus the occurrence number when identical rows repeat in one file)."""
    parts = [
        bank.value,
        parsed.booking_date.isoformat(),
        str(parsed.amount_minor),
        parsed.currency,
        parsed.direction.value,
        normalize_text(parsed.counterparty or ""),
        parsed.bank_reference or "",
        parsed.counterparty_iban or "",
    ]
    if occurrence > 1:
        parts.append(str(occurrence))
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _hashes(bank: Bank, parsed: Iterable[ParsedTransaction]) -> list[str]:
    seen: Counter[str] = Counter()
    hashes: list[str] = []
    for row in parsed:
        base = transaction_hash(bank, row)
        seen[base] += 1
        hashes.append(transaction_hash(bank, row, seen[base]))
    return hashes


def _existing(session: Session, hashes: list[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(hashes), HASH_LOOKUP_CHUNK):
        chunk = hashes[start : start + HASH_LOOKUP_CHUNK]
        found.update(session.scalars(select(Transaction.hash).where(Transaction.hash.in_(chunk))))
    return found


@dataclass
class ImportReport:
    bank_import: BankImport
    new: list[Transaction] = field(default_factory=list)
    flagged: list[Proposal] = field(default_factory=list)
    # `payment_match` proposals for incoming payments from people (never settled here).
    payments: list[Proposal] = field(default_factory=list)
    unclassified: int = 0
    note: str | None = None

    @property
    def summary(self) -> str:
        """ "12 new, 3 duplicates, 2 flagged for splitting" (for the Shortcut notification)."""
        record = self.bank_import
        parts = [f"{record.rows_new} new"]
        if record.rows_duplicate:
            parts.append(f"{record.rows_duplicate} already imported")
        parts.append(f"{len(self.flagged)} flagged for splitting")
        if self.payments:
            count = len(self.payments)
            parts.append(f"{count} {'payment' if count == 1 else 'payments'} received")
        if self.unclassified:
            parts.append(f"{self.unclassified} unclassified")
        text = ", ".join(parts)
        return f"{text}. {self.note}" if self.note else text


def _transaction(
    bank: Bank, parsed: ParsedTransaction, digest: str, record: BankImport
) -> Transaction:
    return Transaction(
        bank_import=record,
        bank=bank,
        booking_date=parsed.booking_date,
        occurred_at=parsed.occurred_at,
        amount_minor=parsed.amount_minor,
        currency=parsed.currency,
        direction=parsed.direction,
        kind=parsed.kind,
        counterparty=parsed.counterparty,
        counterparty_norm=normalize_text(parsed.counterparty) if parsed.counterparty else None,
        counterparty_iban=parsed.counterparty_iban,
        iban_masked=mask_iban(parsed.counterparty_iban) if parsed.counterparty_iban else None,
        message=parsed.message,
        message_norm=normalize_text(parsed.message) if parsed.message else None,
        bank_reference=parsed.bank_reference,
        hash=digest,
        raw_json=parsed.raw,
    )


def import_statement(
    session: Session,
    raw: bytes,
    *,
    filename: str | None,
    bank: Bank | None,
    context: ClassifyContext,
    classifier: TransactionClassifier | None,
    rates: RateSource | None,
) -> ImportReport:
    """Import a statement file (`bank=None`: detect it). Raises InvalidInputError."""
    try:
        parsed = parse_statement(raw, context.tz, bank)
    except BankFormatError as exc:
        raise InvalidInputError(str(exc)) from exc
    hashes = _hashes(parsed.bank, parsed.transactions)
    existing = _existing(session, hashes)
    record = BankImport(
        bank=parsed.bank,
        filename=(filename or "")[:255] or None,
        rows_total=parsed.rows_total,
        rows_skipped=parsed.skipped,
    )
    session.add(record)
    new = [
        _transaction(parsed.bank, row, digest, record)
        for row, digest in zip(parsed.transactions, hashes, strict=True)
        if digest not in existing
    ]
    record.rows_new = len(new)
    record.rows_duplicate = len(parsed.transactions) - len(new)
    session.add_all(new)
    session.flush()

    outcome = classify(
        new, classifier, rates, context, lambda tx: meetings.overlap_text(session, tx.occurred_at)
    )
    flagged = [
        propose_split(session, tx, outcome.confidence.get(tx.id))
        for tx in new
        if tx.classification is Classification.LIKELY_SHARED
        and tx.direction is TransactionDirection.OUT
    ]
    payments = [
        proposal
        for tx in new
        if tx.classification is Classification.INCOMING_FROM_PERSON
        and tx.direction is TransactionDirection.IN
        and (proposal := propose_match(session, tx)) is not None
    ]
    session.flush()
    return ImportReport(record, new, flagged, payments, outcome.unclassified, outcome.note)


def split_push(report: ImportReport, base_url: str) -> PushMessage | None:
    """One push per import: "Lokál, 48.00 € — split?" or a short list of several."""
    if not report.flagged:
        return None
    if len(report.flagged) == 1:
        proposal = report.flagged[0]
        url = f"{base_url}/inbox/{proposal.id}"
        return PushMessage(
            title="🧾 Split?",
            body=proposal.summary,
            click=url,
            tags=("receipt",),
            actions=(view_action("Split…", url),),
        )
    new_by_id = {tx.id: tx for tx in report.new}
    names = []
    for proposal in report.flagged[:PUSH_LISTED]:
        tx = new_by_id.get(int(proposal.source_id or 0))
        if tx is not None:
            names.append(f"{tx.label} {format_amount(tx.amount_minor, tx.currency)}")
    more = len(report.flagged) - len(names)
    body = ", ".join(names) + (f" and {more} more" if more > 0 else "")
    url = f"{base_url}/inbox"
    return PushMessage(
        title=f"🧾 {len(report.flagged)} payments to split?",
        body=body,
        click=url,
        tags=("receipt",),
        actions=(view_action("Open Inbox", url),),
    )


def notify_splits(
    session: Session,
    sender: PushSender | None,
    report: ImportReport,
    *,
    base_url: str,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> Notification | None:
    """Push once per import about the rows flagged for splitting (dedupe key `bank_import:<id>`);
    None when there is nothing to say."""
    message = split_push(report, base_url)
    if message is None:
        return None
    return notifications.notify(
        session,
        sender,
        message,
        dedupe_key=f"bank_import:{report.bank_import.id}",
        kind=PUSH_KIND,
        quiet=quiet,
        now=now,
    )


def notify_import(
    session: Session,
    sender: PushSender | None,
    report: ImportReport,
    *,
    base_url: str,
    secret: str,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> list[Notification]:
    """The import's pushes: payments to split (one push) and payments received."""
    sent = notify_payments(
        session,
        sender,
        report.payments,
        base_url=base_url,
        secret=secret,
        quiet=quiet,
        import_id=report.bank_import.id,
        now=now,
    )
    split = notify_splits(session, sender, report, base_url=base_url, quiet=quiet, now=now)
    return [split, *sent] if split is not None else sent
