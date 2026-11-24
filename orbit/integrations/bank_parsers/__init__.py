"""Bank statement parsers: one module per bank, each built from a real (anonymized) export.

`parse_statement` decodes the file, picks the bank from its header (or checks the one asked
for) and returns normalized `ParsedTransaction`s.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from zoneinfo import ZoneInfo

from orbit.domain.enums import Bank
from orbit.integrations.bank_parsers import revolut, tatra
from orbit.integrations.bank_parsers.base import BankFormatError, ParsedTransaction, ParseResult

__all__ = ["BankFormatError", "ParseResult", "ParsedTransaction", "detect_bank", "parse_statement"]

Parser = Callable[[list[dict[str, str]], ZoneInfo], ParseResult]
PARSERS: dict[Bank, tuple[tuple[str, ...], Parser]] = {
    Bank.REVOLUT: (revolut.HEADER, revolut.parse),
    Bank.TATRA: (tatra.HEADER, tatra.parse),
}
# Revolut exports UTF-8; Tatra banka exports Windows-1250.
_ENCODINGS = ("utf-8-sig", "cp1250")


def decode(raw: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BankFormatError("The file isn't text (UTF-8 or Windows-1250).")


def detect_bank(header: list[str]) -> Bank | None:
    columns = {name.strip() for name in header}
    for bank, (expected, _) in PARSERS.items():
        if set(expected) <= columns:
            return bank
    return None


def parse_statement(raw: bytes, tz: ZoneInfo, bank: Bank | None = None) -> ParseResult:
    """Parse a statement file. `bank=None` detects it from the header."""
    reader = csv.DictReader(io.StringIO(decode(raw), newline=""))
    header = reader.fieldnames or []
    found = detect_bank(list(header))
    if found is None:
        names = ", ".join(b.value for b in PARSERS)
        raise BankFormatError(f"Not a statement Orbit knows ({names}): unrecognized header.")
    if bank is not None and bank is not found:
        raise BankFormatError(f"This looks like a {found.value} statement, not {bank.value}.")
    rows: list[dict[str, str]] = []
    for line, row in enumerate(reader, start=2):
        if None in row or any(value is None for value in row.values()):
            raise BankFormatError(f"Line {line} doesn't have the header's columns.")
        rows.append(row)
    return PARSERS[found][1](rows, tz)
