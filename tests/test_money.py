from __future__ import annotations

import pytest

from orbit.domain.money import exponent, format_amount, format_plain, is_currency, parse_amount

NBSP = chr(0xA0)
NARROW_NBSP = chr(0x202F)


@pytest.mark.parametrize(
    ("text", "currency", "expected"),
    [
        ("12", "EUR", 1200),
        ("12.5", "EUR", 1250),
        ("12,50", "EUR", 1250),
        (" 0.01 ", "EUR", 1),
        ("1 250.00", "EUR", 125000),
        (f"1{NBSP}250,00", "EUR", 125000),
        (f"1{NARROW_NBSP}250", "EUR", 125000),
        ("12 €", "EUR", 1200),
        ("250 Kč", "CZK", 25000),
        ("250 kč", "CZK", 25000),
        ("$9.99", "USD", 999),
        ("1500", "JPY", 1500),
        ("1.234", "KWD", 1234),
    ],
)
def test_parse_amount(text: str, currency: str, expected: int) -> None:
    assert parse_amount(text, currency) == expected


@pytest.mark.parametrize(
    ("text", "currency"),
    [("", "EUR"), ("abc", "EUR"), ("-5", "EUR"), ("1.2.3", "EUR"), ("12.345", "EUR"),
     ("1.5", "JPY"), ("1,250.00", "EUR"), ("1e3", "EUR")],
)  # fmt: skip
def test_parse_amount_rejects(text: str, currency: str) -> None:
    with pytest.raises(ValueError):
        parse_amount(text, currency)


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (1250, "EUR", "12.50 €"),
        (5, "EUR", "0.05 €"),
        (2500000, "CZK", "25,000.00 Kč"),
        (999, "USD", "$9.99"),
        (-999, "USD", "-$9.99"),
        (1000, "GBP", "£10.00"),
        (1500, "JPY", "1,500 JPY"),
        (12345, "KWD", "12.345 KWD"),
        (10000, "PLN", "100.00 PLN"),
    ],
)
def test_format_amount(amount: int, currency: str, expected: str) -> None:
    assert format_amount(amount, currency) == expected


def test_exponent_and_currency_check() -> None:
    assert exponent("EUR") == 2
    assert exponent("JPY") == 0
    assert exponent("KWD") == 3
    assert is_currency("EUR")
    assert not is_currency("eur")
    assert not is_currency("EURO")


@pytest.mark.parametrize(
    ("minor", "currency", "text"),
    [(1250, "EUR", "12.50"), (125000, "CZK", "1250.00"), (1500, "JPY", "1500"), (5, "EUR", "0.05")],
)
def test_format_plain_reads_back(minor: int, currency: str, text: str) -> None:
    assert format_plain(minor, currency) == text
    assert parse_amount(text, currency) == minor
