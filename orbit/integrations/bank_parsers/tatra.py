"""Tatra banka CSV from internet banking ("download-DD.MM.YYYY.csv", Windows-1250, CRLF).

Layout, from a real export (tests/fixtures/bank/tatra.csv):
    "Dátum spracovania","Dátum zúčtovania",Suma,Mena,Typ,"Predčíslie","Číslo účtu","Kód banky",
    IBAN,"Variabilný symbol","Špecifický symbol","Konštantný symbol","Referencia platiteľa",
    "Informácia pre príjemcu",Popis
Dates are DD.MM.YYYY; `Suma` is unsigned with a decimal comma ("1,39"); `Typ` is Debet/Kredit.
Card payments carry the details in "Informácia pre príjemcu", in two shapes:
    400000******0000 Praha 2 20260912 17:44:12 Potraviny Sever dekuje za nakup
    400000******0000 BRNO NOVÁK ALEX 20260909 18:21:44 5.30EUR Potraviny Sever dakuje 158
(card, place [+ cardholder], local date and time, [original amount + currency,] merchant).
Transfers have no counterparty name, only the account (IBAN) and an optional message.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from orbit.domain.enums import Bank, TransactionDirection, TransactionKind
from orbit.domain.normalize import compact_iban, normalize_text, normalize_whitespace
from orbit.integrations.bank_parsers.base import (
    BankFormatError,
    ParsedTransaction,
    ParseResult,
    clean,
    local_to_utc,
    to_minor,
)

HEADER = (
    "Dátum spracovania",
    "Dátum zúčtovania",
    "Suma",
    "Mena",
    "Typ",
    "Predčíslie",
    "Číslo účtu",
    "Kód banky",
    "IBAN",
    "Variabilný symbol",
    "Špecifický symbol",
    "Konštantný symbol",
    "Referencia platiteľa",
    "Informácia pre príjemcu",
    "Popis",
)
_DIRECTIONS = {"debet": TransactionDirection.OUT, "kredit": TransactionDirection.IN}
_CARD_INFO = re.compile(
    r"^\d{4,6}\*+\d{4}\s+(?P<place>.*?)\s*(?P<when>\d{8} \d{2}:\d{2}:\d{2})\s+"
    r"(?:\d*\.\d+[A-Z]{3}\s+)?(?P<merchant>.+?)\s*$"
)
# "Platba OO26091200049529": a transfer with the bank's own payment id.
_PAYMENT_ID = re.compile(r"^Platba\s+(OO\d+)\s*$")


def _date(value: str, *, line: int) -> date:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError as exc:
        raise BankFormatError(f"Line {line}: “{value}” isn't a date.") from exc


def _kind(description: str, card_info: bool) -> TransactionKind:
    text = normalize_text(description)
    if "bankomat" in text:
        return TransactionKind.ATM
    if card_info or "nakup" in text or text.startswith("platba kartou"):
        return TransactionKind.CARD
    if text.startswith("platba"):
        return TransactionKind.TRANSFER
    return TransactionKind.OTHER


def parse(rows: list[dict[str, str]], tz: ZoneInfo) -> ParseResult:
    transactions: list[ParsedTransaction] = []
    skipped = 0
    for line, row in enumerate(rows, start=2):
        direction = _DIRECTIONS.get((row["Typ"] or "").strip().casefold())
        if direction is None:
            raise BankFormatError(f"Line {line}: unknown type “{row['Typ']}” (Debet/Kredit).")
        currency = (row["Mena"] or "").strip().upper()
        amount = to_minor(row["Suma"], currency, line=line)
        if amount == 0:
            skipped += 1
            continue
        info = clean(row["Informácia pre príjemcu"])
        description = clean(row["Popis"]) or ""
        card = _CARD_INFO.match(info) if info else None
        kind = _kind(description, card is not None)
        iban = clean(row["IBAN"])
        occurred_at = None
        if card:
            counterparty: str | None = normalize_whitespace(card["merchant"])
            when = datetime.strptime(card["when"], "%Y%m%d %H:%M:%S")
            occurred_at = local_to_utc(when, tz)
            message = None
            reference: str | None = when.isoformat()
        else:
            counterparty = None if kind is TransactionKind.TRANSFER else info
            message = info if kind is TransactionKind.TRANSFER else None
            payment_id = _PAYMENT_ID.match(description)
            reference = clean(row["Referencia platiteľa"]) or (
                payment_id.group(1) if payment_id else None
            )
        transactions.append(
            ParsedTransaction(
                booking_date=_date(row["Dátum zúčtovania"], line=line),
                occurred_at=occurred_at,
                amount_minor=abs(amount),
                currency=currency,
                direction=direction,
                kind=kind,
                counterparty=counterparty,
                counterparty_iban=compact_iban(iban) if iban else None,
                message=message,
                bank_reference=reference,
                raw=dict(row),
            )
        )
    return ParseResult(Bank.TATRA, transactions, skipped, len(rows))
