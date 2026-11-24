from __future__ import annotations

import pytest

from orbit.domain.enums import HandleType
from orbit.domain.normalize import (
    normalize_handle,
    normalize_phone,
    normalize_text,
    strip_diacritics,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tobiáš", "tobias"),
        ("Paťo", "pato"),
        ("PAŤO", "pato"),
        ("Ľubomír Šťastný", "lubomir stastny"),
        ("Ďurčo Ňaňo Ŕ Ĺ ô ä", "durco nano r l o a"),
        ("Řehoř Dvořák", "rehor dvorak"),
        ("Jiří Kůň Ěšč", "jiri kun esc"),
        ("  Terka \t\n Nováková  ", "terka novakova"),
        ("Pato" + chr(0xA0) + "Veselý", "pato vesely"),  # non-breaking space
        ("Straße", "strasse"),
        ("", ""),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_strip_diacritics_keeps_case() -> None:
    assert strip_diacritics("Tobiáš Ďurčo") == "Tobias Durco"


def test_search_equivalence() -> None:
    assert normalize_text("pato") == normalize_text("Paťo") == normalize_text("PAŤO")


@pytest.mark.parametrize(
    ("raw", "region", "expected"),
    [
        ("0905 123 456", "SK", "+421905123456"),
        ("0905123456", "sk", "+421905123456"),
        ("+421 905 123 456", None, "+421905123456"),
        ("00421 905 123 456", None, "+421905123456"),
        ("+420 601 123 456", None, "+420601123456"),
        ("601 123 456", "CZ", "+420601123456"),
        ("+420 601 123 456", "SK", "+420601123456"),  # explicit prefix wins over region
        ("(0905) 123-456", "SK", "+421905123456"),
    ],
)
def test_normalize_phone(raw: str, region: str | None, expected: str) -> None:
    assert normalize_phone(raw, region) == expected


@pytest.mark.parametrize(
    ("raw", "region"),
    [
        ("0905 123 456", None),  # no prefix, no region: can't resolve
        ("not a phone", "SK"),
        ("", "SK"),
        ("123", "SK"),
        ("+421 1", None),
    ],
)
def test_normalize_phone_unresolvable(raw: str, region: str | None) -> None:
    assert normalize_phone(raw, region) is None


@pytest.mark.parametrize(
    ("handle_type", "value", "region", "expected"),
    [
        (HandleType.PHONE, "0905 123 456", "SK", "+421905123456"),
        (HandleType.PHONE, "0905 123 456", None, "0905123456"),  # unresolved: digits only
        (HandleType.PHONE, "+421 1", None, "+4211"),
        (HandleType.WHATSAPP, "+420 601 123 456", None, "+420601123456"),
        (HandleType.EMAIL, "  Pato.Vesely@Example.COM ", None, "pato.vesely@example.com"),
        (HandleType.INSTAGRAM, "@Pato.Vesely", None, "pato.vesely"),
        (HandleType.INSTAGRAM, "https://www.instagram.com/pato.vesely/?hl=sk", None, "pato.vesely"),
        (HandleType.INSTAGRAM, "instagram.com/pato", None, "pato"),
        (HandleType.TELEGRAM, "https://t.me/Terka_N", None, "terka_n"),
        (HandleType.DISCORD, "@Pato#1234", None, "pato#1234"),
        (
            HandleType.LINKEDIN,
            "https://www.linkedin.com/in/Martin-Horvath-123/",
            None,
            "martin-horvath-123",
        ),
        (HandleType.LINKEDIN, "martin-horvath", None, "martin-horvath"),
        (HandleType.IBAN, "sk31 1200 0000 1987 4263 7541", None, "SK3112000000198742637541"),
        (HandleType.BANK_NAME, "VESELÝ  PATRIK", None, "vesely patrik"),
        (HandleType.OTHER, "  Něco ", None, "neco"),
        (HandleType.INSTAGRAM, "@", None, ""),
    ],
)
def test_normalize_handle(
    handle_type: HandleType, value: str, region: str | None, expected: str
) -> None:
    assert normalize_handle(handle_type, value, region) == expected
