"""Money as integer minor units + ISO 4217 currency: parsing user input and display formatting."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# ISO 4217 minor-unit exponents that differ from the usual 2.
_EXPONENTS = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0,
    "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}  # fmt: skip
# (prefix, suffix) for currencies shown with a symbol; anything else shows its code.
_SYMBOLS = {
    "EUR": ("", " €"),
    "CZK": ("", " Kč"),
    "USD": ("$", ""),
    "GBP": ("£", ""),
}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_AMOUNT_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
# Spaces (incl. the no-break ones iOS and Slovak formatting use) and currency symbols.
_NOISE_RE = re.compile(
    r"[\s\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}€$£]|kč", flags=re.IGNORECASE
)


def is_currency(code: str) -> bool:
    return bool(_CURRENCY_RE.match(code))


def exponent(currency: str) -> int:
    return _EXPONENTS.get(currency, 2)


def parse_amount(text: str, currency: str) -> int:
    """Major units ("12", "12.5", "12,50", "1 250.00") -> minor units. Raises ValueError."""
    cleaned = _NOISE_RE.sub("", text)
    if not _AMOUNT_RE.match(cleaned):
        raise ValueError(f"“{text}” isn't an amount like 12.50.")
    try:
        amount = Decimal(cleaned.replace(",", "."))
    except InvalidOperation as exc:  # pragma: no cover - the regex already rules this out
        raise ValueError(f"“{text}” isn't an amount like 12.50.") from exc
    minor = amount.scaleb(exponent(currency))
    if minor != minor.to_integral_value():
        raise ValueError(f"{currency} amounts have at most {exponent(currency)} decimal places.")
    return int(minor)


def format_amount(amount_minor: int, currency: str) -> str:
    """1250 EUR -> "12.50 €"; 2500000 CZK -> "25,000.00 Kč"; 1500 JPY -> "1,500 JPY"."""
    digits = exponent(currency)
    sign = "-" if amount_minor < 0 else ""
    major = Decimal(abs(amount_minor)).scaleb(-digits)
    number = f"{major:,.{digits}f}"
    prefix, suffix = _SYMBOLS.get(currency, ("", f" {currency}"))
    return f"{sign}{prefix}{number}{suffix}"


def format_plain(amount_minor: int, currency: str) -> str:
    """1250 EUR -> "12.50": no symbol or grouping, so `parse_amount` reads it back."""
    digits = exponent(currency)
    return f"{Decimal(amount_minor).scaleb(-digits):.{digits}f}"
