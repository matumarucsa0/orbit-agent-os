from __future__ import annotations

from pathlib import Path

import pytest

from orbit.integrations.vcard import (
    Birthday,
    LabeledValue,
    SocialProfile,
    VCard,
    country_code_from_name,
    parse_vcards,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vcard" / "apple_contacts.vcf"


@pytest.fixture(scope="module")
def cards() -> dict[str, VCard]:
    parsed = parse_vcards(FIXTURE.read_text(encoding="utf-8"))
    assert len(parsed) == 7
    return {card.uid[-1]: card for card in parsed if card.uid}


def test_full_apple_card(cards: dict[str, VCard]) -> None:
    pato = cards["1"]
    assert pato.formatted_name == "Patrik Veselý"
    assert (pato.given_name, pato.family_name) == ("Patrik", "Veselý")
    assert pato.nicknames == ["Paťo", "Pato"]
    assert pato.phones == [LabeledValue("+421 905 123 456", "mobile")]
    assert pato.emails == [LabeledValue("patrik.vesely@example.com", "home")]
    assert pato.birthday == Birthday(3, 17, 1994)
    assert pato.socials == [SocialProfile("instagram", "pato.vesely")]
    assert (pato.city, pato.country_code) == ("Košice", "SK")
    assert pato.note == "Met at Uni, loves climbing.\nHas a dog."


def test_folded_note_omitted_year_and_impp(cards: dict[str, VCard]) -> None:
    terka = cards["2"]
    assert terka.birthday == Birthday(11, 2, None)
    assert terka.socials == [SocialProfile("telegram", "terka_n")]
    assert terka.phones == [LabeledValue("0905 111 222", "cell")]
    assert terka.note is not None
    assert "several physical lines when exporting, so" in terka.note
    assert terka.note.endswith("that start with a space.")


def test_country_from_address_name(cards: dict[str, VCard]) -> None:
    jiri = cards["3"]
    assert jiri.country_code == "CZ"
    assert jiri.city == "Praha"
    assert jiri.phones == [LabeledValue("601 234 567", "mobile")]
    assert jiri.emails == [LabeledValue("jiri.dvorak@example.cz", "work")]


def test_company_card(cards: dict[str, VCard]) -> None:
    company = cards["4"]
    assert company.formatted_name is None
    assert company.given_name is None
    assert company.organization == "Autoservis Kováč"


def test_vcard4_birthday_and_linkedin(cards: dict[str, VCard]) -> None:
    martin = cards["5"]
    assert martin.birthday == Birthday(7, 8, None)
    assert martin.socials == [
        SocialProfile("linkedin", "https://www.linkedin.com/in/martin-horvath-123/")
    ]


def test_photo_skipped_and_custom_label(cards: dict[str, VCard]) -> None:
    ema = cards["6"]
    assert ema.emails == [LabeledValue("ema@example.com", "school")]
    assert ema.phones == []


def test_email_only_card(cards: dict[str, VCard]) -> None:
    card = cards["7"]
    assert card.formatted_name is None
    assert card.emails == [LabeledValue("someone@example.org", None)]


def test_crlf_line_endings() -> None:
    text = FIXTURE.read_text(encoding="utf-8").replace("\n", "\r\n")
    assert len(parse_vcards(text)) == 7


def test_vcard21_bare_types_and_garbage_lines() -> None:
    text = (
        "BEGIN:VCARD\nVERSION:2.1\nnot a property\nFN:Old Phone\nTEL;CELL:0905 000 111\nEND:VCARD\n"
    )
    (card,) = parse_vcards(text)
    assert card.formatted_name == "Old Phone"
    assert card.phones == [LabeledValue("0905 000 111", "cell")]


def test_placeholder_year_without_omit_flag() -> None:
    # iCloud CardDAV sends Apple's "no year" placeholder without X-APPLE-OMIT-YEAR.
    (card,) = parse_vcards("BEGIN:VCARD\nFN:X\nBDAY;value=date:1604-05-12\nEND:VCARD\n")
    assert card.birthday == Birthday(5, 12, None)


def test_invalid_birthday_ignored() -> None:
    (card,) = parse_vcards("BEGIN:VCARD\nFN:X\nBDAY:1990-13-45\nEND:VCARD\n")
    assert card.birthday is None


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("Slovensko", "SK"),
        ("Slovakia", "SK"),
        ("Česká republika", "CZ"),
        ("Czechia", "CZ"),
        ("Rakúsko", "AT"),
        ("de", "DE"),
        ("Atlantis", None),
    ],
)
def test_country_names(name: str, code: str | None) -> None:
    assert country_code_from_name(name) == code
