from __future__ import annotations

import pytest

from orbit.domain.enums import HandleType
from orbit.services.contact_links import ContactLink, links_for


@pytest.mark.parametrize(
    ("handle_type", "value", "expected"),
    [
        (
            HandleType.PHONE,
            "+421 905 123 456",
            [ContactLink("Call", "tel:+421905123456"), ContactLink("Text", "sms:+421905123456")],
        ),
        (
            HandleType.PHONE,
            "00420 777 123 456",
            [ContactLink("Call", "tel:+420777123456"), ContactLink("Text", "sms:+420777123456")],
        ),
        (
            HandleType.PHONE,
            "0905 123 456",
            [ContactLink("Call", "tel:0905123456"), ContactLink("Text", "sms:0905123456")],
        ),
        (
            HandleType.WHATSAPP,
            "+421 905 123 456",
            [ContactLink("WhatsApp", "https://wa.me/421905123456")],
        ),
        (HandleType.EMAIL, "pato@example.com", [ContactLink("Email", "mailto:pato@example.com")]),
        (
            HandleType.INSTAGRAM,
            "@pato.vesely",
            [ContactLink("Instagram", "https://instagram.com/pato.vesely")],
        ),
        (
            HandleType.INSTAGRAM,
            "https://instagram.com/pato.vesely/",
            [ContactLink("Instagram", "https://instagram.com/pato.vesely")],
        ),
        (HandleType.TELEGRAM, "@pato", [ContactLink("Telegram", "https://t.me/pato")]),
        (
            HandleType.LINKEDIN,
            "pato-vesely",
            [ContactLink("LinkedIn", "https://www.linkedin.com/in/pato-vesely")],
        ),
        (
            HandleType.LINKEDIN,
            "https://www.linkedin.com/in/pato/",
            [ContactLink("LinkedIn", "https://www.linkedin.com/in/pato/")],
        ),
    ],
)
def test_links_for(handle_type: HandleType, value: str, expected: list[ContactLink]) -> None:
    assert links_for(handle_type, value) == expected


@pytest.mark.parametrize(
    ("handle_type", "value"),
    [
        (HandleType.PHONE, "12"),
        (HandleType.EMAIL, "not an email"),
        (HandleType.IBAN, "SK00 0000 0000 0000 0000 0000"),
        (HandleType.BANK_NAME, "VESELY PATRIK"),
        (HandleType.DISCORD, "pato#1234"),
        (HandleType.OTHER, "whatever"),
        (HandleType.PHONE, "   "),
    ],
)
def test_nothing_to_open(handle_type: HandleType, value: str) -> None:
    assert links_for(handle_type, value) == []
