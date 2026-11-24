"""Revolut "Account statement" CSV (English, UTF-8).

Layout, from a real export (tests/fixtures/bank/revolut.csv):
    Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
    Topup,Current,2026-09-14 10:50:26,2026-09-14 10:50:28,Apple Pay top-up by *0000,…,COMPLETED,…
Amounts are signed with a "." decimal point; times are local ("YYYY-MM-DD HH:MM:SS").
Only COMPLETED rows are imported: pending ones may still change or be reverted.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from orbit.domain.enums import Bank, TransactionDirection, TransactionKind
from orbit.integrations.bank_parsers.base import (
    BankFormatError,
    ParsedTransaction,
    ParseResult,
    clean,
    local_to_utc,
    to_minor,
)

HEADER = (
    "Type",
    "Product",
    "Started Date",
    "Completed Date",
    "Description",
    "Amount",
    "Fee",
    "Currency",
    "State",
    "Balance",
)
COMPLETED = "COMPLETED"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# Revolut's `Type` column. Topup and Exchange are in the sample; the others are Revolut's names
# for card payments, transfers, ATM withdrawals and fees. Anything else is kept as "other".
_KINDS = {
    "topup": TransactionKind.TOPUP,
    "exchange": TransactionKind.EXCHANGE,
    "card payment": TransactionKind.CARD,
    "transfer": TransactionKind.TRANSFER,
    "atm": TransactionKind.ATM,
    "fee": TransactionKind.FEE,
}


def _time(value: str, *, line: int) -> datetime:
    try:
        return datetime.strptime(value.strip(), _TIME_FORMAT)
    except ValueError as exc:
        raise BankFormatError(f"Line {line}: “{value}” isn't a date and time.") from exc


def parse(rows: list[dict[str, str]], tz: ZoneInfo) -> ParseResult:
    transactions: list[ParsedTransaction] = []
    skipped = 0
    for line, row in enumerate(rows, start=2):
        currency = (row["Currency"] or "").strip().upper()
        if (row["State"] or "").strip().upper() != COMPLETED or not clean(row["Completed Date"]):
            skipped += 1
            continue
        amount = to_minor(row["Amount"], currency, line=line)
        if amount == 0:
            skipped += 1
            continue
        started = _time(row["Started Date"], line=line)
        completed = _time(row["Completed Date"], line=line)
        transactions.append(
            ParsedTransaction(
                booking_date=completed.date(),
                occurred_at=local_to_utc(started, tz),
                amount_minor=abs(amount),
                currency=currency,
                direction=TransactionDirection.IN if amount > 0 else TransactionDirection.OUT,
                kind=_KINDS.get((row["Type"] or "").strip().casefold(), TransactionKind.OTHER),
                counterparty=clean(row["Description"]),
                # No transaction id in the export; the start time tells same-day rows apart.
                bank_reference=started.isoformat(),
                raw=dict(row),
            )
        )
    return ParseResult(Bank.REVOLUT, transactions, skipped, len(rows))
