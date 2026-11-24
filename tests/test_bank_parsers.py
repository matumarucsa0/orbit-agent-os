from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbit.domain.enums import Bank, TransactionDirection, TransactionKind
from orbit.integrations.bank_parsers import BankFormatError, ParsedTransaction, parse_statement

FIXTURES = Path(__file__).parent / "fixtures" / "bank"
PRAGUE = ZoneInfo("Europe/Prague")
OUT, IN = TransactionDirection.OUT, TransactionDirection.IN


def tatra_bytes() -> bytes:
    return (FIXTURES / "tatra.csv").read_bytes()


def revolut_bytes() -> bytes:
    return (FIXTURES / "revolut.csv").read_bytes()


def by_counterparty(rows: list[ParsedTransaction], name: str) -> ParsedTransaction:
    (row,) = [r for r in rows if r.counterparty == name]
    return row


# --- Detection & decoding ---


def test_detects_the_bank_from_the_header() -> None:
    assert parse_statement(tatra_bytes(), PRAGUE).bank is Bank.TATRA
    assert parse_statement(revolut_bytes(), PRAGUE).bank is Bank.REVOLUT


def test_the_fixtures_keep_the_banks_encodings() -> None:
    raw = tatra_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # Tatra banka exports Windows-1250 …
    assert b"\r\n" in raw  # … with CRLF line ends
    revolut_bytes().decode("utf-8")


def test_a_utf8_copy_of_a_tatra_file_reads_the_same() -> None:
    raw = tatra_bytes()
    utf8 = parse_statement(raw.decode("cp1250").encode("utf-8-sig"), PRAGUE)
    assert utf8.transactions == parse_statement(raw, PRAGUE).transactions


def test_rejects_an_unknown_file() -> None:
    with pytest.raises(BankFormatError, match="unrecognized header"):
        parse_statement(b"Date,Amount\n2026-09-01,12.00\n", PRAGUE)
    with pytest.raises(BankFormatError, match="unrecognized header"):
        parse_statement(b"", PRAGUE)


def test_rejects_the_wrong_bank_when_one_is_asked_for() -> None:
    with pytest.raises(BankFormatError, match="looks like a tatra statement, not revolut"):
        parse_statement(tatra_bytes(), PRAGUE, Bank.REVOLUT)
    assert parse_statement(tatra_bytes(), PRAGUE, Bank.TATRA).bank is Bank.TATRA


# --- Tatra banka ---


def test_tatra_reads_every_row() -> None:
    result = parse_statement(tatra_bytes(), PRAGUE)
    assert (result.rows_total, len(result.transactions), result.skipped) == (31, 31, 0)
    kinds = Counter((t.kind, t.direction) for t in result.transactions)
    assert kinds == {
        (TransactionKind.CARD, OUT): 21,
        (TransactionKind.TRANSFER, IN): 6,
        (TransactionKind.TRANSFER, OUT): 3,
        (TransactionKind.ATM, OUT): 1,
    }
    assert {t.currency for t in result.transactions} == {"EUR"}


def test_tatra_card_payment_short_form() -> None:
    """'Platba kartou': card, place, local time, merchant."""
    first = parse_statement(tatra_bytes(), PRAGUE).transactions[0]
    assert first.counterparty == "Potraviny Sever dekuje za nakup"
    assert first.amount_minor == 139
    assert first.direction is OUT
    assert first.booking_date == date(2026, 9, 12)
    assert first.occurred_at == datetime(2026, 9, 12, 15, 44, 12, tzinfo=UTC)  # 17:44:12 CEST
    assert first.bank_reference == "2026-09-12T17:44:12"
    assert first.message is None and first.counterparty_iban is None


def test_tatra_uses_the_booking_date_not_the_processing_date() -> None:
    rows = parse_statement(tatra_bytes(), PRAGUE).transactions
    relay = by_counterparty(rows, "KIOSK NAROZI 12")
    assert relay.raw["Dátum spracovania"] == "12.09.2026"
    assert relay.booking_date == date(2026, 9, 11)


def test_tatra_card_payment_long_form_with_foreign_currency() -> None:
    """'AP nákup POS': the original amount/currency sits between the time and the merchant."""
    rows = parse_statement(tatra_bytes(), PRAGUE).transactions
    hotel = by_counterparty(rows, "HOTEL U MOSTU 02")
    assert (hotel.amount_minor, hotel.currency) == (1581, "EUR")  # booked in EUR (373 CZK)
    assert hotel.occurred_at == datetime(2026, 8, 13, 14, 25, 40, tzinfo=UTC)
    kebap = by_counterparty(rows, "Grill Zahrada, BN, Hlavni")  # commas in the merchant
    assert kebap.amount_minor == 550
    # ".79EUR" (no leading zero) and a doubled space in the merchant
    tempo = by_counterparty(rows, "Potraviny Plzen Tempo")
    assert tempo.amount_minor == 146
    assert tempo.raw["Popis"] == "POS nákup"


def test_tatra_atm_withdrawal() -> None:
    rows = parse_statement(tatra_bytes(), PRAGUE).transactions
    (atm,) = [r for r in rows if r.kind is TransactionKind.ATM]
    assert (atm.amount_minor, atm.counterparty) == (1000, "TB Centrum")


def test_tatra_incoming_transfer() -> None:
    rows = parse_statement(tatra_bytes(), PRAGUE).transactions
    (rent,) = [r for r in rows if r.message == "Najom 12345"]
    assert rent.kind is TransactionKind.TRANSFER and rent.direction is IN
    assert rent.amount_minor == 100000  # "1000,00"
    assert rent.counterparty is None  # Tatra shows no name for transfers
    assert rent.counterparty_iban == "SK3211000000001000002222"
    assert rent.bank_reference == "/VS12345/SS/KS558"
    assert rent.occurred_at is None


def test_tatra_outgoing_transfer_with_the_banks_payment_id() -> None:
    rows = parse_statement(tatra_bytes(), PRAGUE).transactions
    (cent,) = [r for r in rows if r.amount_minor == 1]
    assert cent.kind is TransactionKind.TRANSFER and cent.direction is OUT
    assert cent.counterparty_iban == "BE71096123456769"
    assert cent.bank_reference == "AB12CD3"  # "Referencia platiteľa" wins over the OO id


def test_tatra_keeps_diacritics_in_raw_rows() -> None:
    rows = parse_statement(tatra_bytes(), PRAGUE).transactions
    assert "NOVÁK ALEX" in rows[5].raw["Informácia pre príjemcu"]


def test_tatra_rejects_a_broken_row() -> None:
    raw = tatra_bytes().decode("cp1250").replace('"1,39"', '"1,3x9"', 1).encode("cp1250")
    with pytest.raises(BankFormatError, match=r"Line 2: “1,3x9” isn't an amount"):
        parse_statement(raw, PRAGUE)
    header = tatra_bytes().decode("cp1250").split("\r\n")[0]
    with pytest.raises(BankFormatError, match="Line 2 doesn't have the header's columns"):
        parse_statement(f"{header}\r\n12.09.2026,12.09.2026\r\n".encode("cp1250"), PRAGUE)


# --- Revolut ---


def test_revolut_reads_topups_and_exchanges() -> None:
    result = parse_statement(revolut_bytes(), PRAGUE)
    assert (result.rows_total, len(result.transactions), result.skipped) == (5, 5, 0)
    topup, _, exchange, *_ = result.transactions
    assert (topup.kind, topup.direction, topup.amount_minor) == (TransactionKind.TOPUP, IN, 20000)
    assert topup.counterparty == "Apple Pay top-up by *0000"
    assert topup.booking_date == date(2026, 9, 14)
    assert topup.occurred_at == datetime(2026, 9, 14, 7, 14, 8, tzinfo=UTC)
    assert topup.bank_reference == "2026-09-14T09:14:08"
    assert (exchange.kind, exchange.direction, exchange.amount_minor) == (
        TransactionKind.EXCHANGE,
        OUT,
        20862,
    )


def revolut_with(*rows: str) -> bytes:
    header = revolut_bytes().decode().splitlines()[0]
    return "\n".join([header, *rows]).encode()


def revolut_row(
    kind: str,
    description: str,
    amount: str,
    *,
    currency: str = "EUR",
    state: str = "COMPLETED",
    started: str = "2026-09-19 20:30:00",
    completed: str = "2026-09-20 09:00:00",
) -> str:
    # Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
    return ",".join(
        [kind, "Current", started, completed, description, amount, "0.00", currency, state, "1"]
    )


def test_revolut_skips_rows_that_arent_completed_or_are_zero() -> None:
    result = parse_statement(
        revolut_with(
            revolut_row("Topup", "Top-up by *0000", "10.00", state="PENDING", completed=""),
            revolut_row("Topup", "Top-up", "0.00"),
            revolut_row("Topup", "Top-up", "5.00"),
        ),
        PRAGUE,
    )
    assert (result.rows_total, len(result.transactions), result.skipped) == (3, 1, 2)


def test_revolut_other_types_keep_the_description_and_the_sign() -> None:
    """Only Topup/Exchange are in the sample; other types parse with the same columns."""
    (row,) = parse_statement(
        revolut_with(revolut_row("Card Payment", "Lokál", "-48.00")), PRAGUE
    ).transactions
    assert (row.kind, row.direction, row.amount_minor) == (TransactionKind.CARD, OUT, 4800)
    assert row.counterparty == "Lokál"
    assert row.booking_date == date(2026, 9, 20)
    assert row.occurred_at == datetime(2026, 9, 19, 18, 30, tzinfo=UTC)
    (odd,) = parse_statement(revolut_with(revolut_row("Reward", "X", "1.00")), PRAGUE).transactions
    assert (odd.kind, odd.direction) == (TransactionKind.OTHER, IN)


def test_revolut_currency_exponents() -> None:
    (row,) = parse_statement(
        revolut_with(revolut_row("Card Payment", "Sushi", "-1500", currency="JPY")), PRAGUE
    ).transactions
    assert (row.amount_minor, row.currency) == (1500, "JPY")
    with pytest.raises(BankFormatError, match="isn't a JPY amount"):
        parse_statement(
            revolut_with(revolut_row("Card Payment", "Sushi", "-1.5", currency="JPY")), PRAGUE
        )
