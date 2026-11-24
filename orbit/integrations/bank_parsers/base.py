"""The normalized row every bank parser produces, and shared parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from orbit.domain.enums import Bank, TransactionDirection, TransactionKind
from orbit.domain.money import exponent


class BankFormatError(ValueError):
    """The file isn't a statement Orbit can read (unknown header, broken row)."""


@dataclass(frozen=True)
class ParsedTransaction:
    booking_date: date
    amount_minor: int  # > 0
    currency: str
    direction: TransactionDirection
    kind: TransactionKind
    raw: dict[str, str]
    occurred_at: datetime | None = None  # UTC
    counterparty: str | None = None
    counterparty_iban: str | None = None  # compact
    message: str | None = None
    bank_reference: str | None = None


@dataclass(frozen=True)
class ParseResult:
    bank: Bank
    transactions: list[ParsedTransaction]
    skipped: int = 0  # rows left out on purpose (pending, zero amount)
    rows_total: int = field(default=0)


def to_minor(text: str, currency: str, *, line: int) -> int:
    """A signed decimal ("-2485.11", "1000,00") -> signed minor units."""
    cleaned = "".join(text.split()).replace(",", ".")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise BankFormatError(f"Line {line}: “{text}” isn't an amount.") from exc
    minor = amount.scaleb(exponent(currency))
    if not amount.is_finite() or minor != minor.to_integral_value():
        raise BankFormatError(f"Line {line}: “{text}” isn't a {currency} amount.")
    return int(minor)


def local_to_utc(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))


def clean(value: str | None) -> str | None:
    """Strip, and turn empty strings into None."""
    if value is None:
        return None
    value = value.strip()
    return value or None
