"""vCard patching keeps everything it was not asked to touch."""

from __future__ import annotations

from pathlib import Path

from orbit.domain.enums import HandleType
from orbit.integrations.vcard import parse_vcards
from orbit.integrations.vcard_patch import (
    AddHandle,
    RemoveHandle,
    SetBirthday,
    SetName,
    patch_vcard,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vcard" / "apple_contacts.vcf"
FIRST = FIXTURE.read_text(encoding="utf-8").split("END:VCARD\n")[0] + "END:VCARD\n"
PHOTO_1 = (
    "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoH"
)
PHOTO_2 = " BwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/"
CRLF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Test;Tom;;;\r\nFN:Tom Test\r\n"
    f"{PHOTO_1}\r\n{PHOTO_2}\r\n"
    "X-CUSTOM-APP;x-foo=bar:keep me\r\nUID:crlf-1\r\nEND:VCARD\r\n"
)


def card(text: str):
    return parse_vcards(text)[0]


def test_no_ops_returns_the_text_unchanged() -> None:
    assert patch_vcard(FIRST, [], "SK") == FIRST
    assert patch_vcard(CRLF, [], "SK") == CRLF


def test_birthday_replaced_added_and_removed() -> None:
    changed = patch_vcard(FIRST, [SetBirthday(5, 6, 1990)], "SK")
    assert "BDAY:1990-05-06" in changed and "BDAY:1994-03-17" not in changed
    assert card(changed).birthday.year == 1990
    # Untouched lines are byte-identical.
    assert [ln for ln in FIRST.splitlines() if not ln.startswith("BDAY")] == [
        ln for ln in changed.splitlines() if not ln.startswith("BDAY")
    ]

    no_year = patch_vcard(CRLF, [SetBirthday(2, 14)], "SK")
    parsed = card(no_year).birthday
    assert (parsed.month, parsed.day, parsed.year) == (2, 14, None)

    assert card(patch_vcard(FIRST, [SetBirthday(None)], "SK")).birthday is None


def test_unknown_properties_photo_and_line_endings_survive() -> None:
    changed = patch_vcard(
        CRLF, [SetBirthday(2, 14), AddHandle(HandleType.PHONE, "+421905123456")], "SK"
    )
    assert f"{PHOTO_1}\r\n{PHOTO_2}\r\n" in changed
    assert "X-CUSTOM-APP;x-foo=bar:keep me\r\n" in changed
    assert "\n" not in changed.replace("\r\n", "")  # still CRLF throughout
    assert changed.rstrip().endswith("END:VCARD")


def test_add_and_remove_phone_email_social() -> None:
    changed = patch_vcard(
        FIRST,
        [
            AddHandle(HandleType.PHONE, "0911 222 333"),
            AddHandle(HandleType.EMAIL, "new@example.com"),
            AddHandle(HandleType.INSTAGRAM, "@new.person"),
            RemoveHandle(HandleType.PHONE, "+421905123456"),
        ],
        "SK",
    )
    parsed = card(changed)
    assert [p.value for p in parsed.phones] == ["0911 222 333"]
    assert "new@example.com" in [e.value for e in parsed.emails]
    assert any(s.service == "instagram" and s.value == "new.person" for s in parsed.socials)
    # The removed number took its Apple label line along.
    assert "item1.X-ABLabel" not in changed
    assert "patrik.vesely@example.com" in changed  # the other email stays


def test_adding_what_is_already_there_is_a_no_op() -> None:
    assert patch_vcard(FIRST, [AddHandle(HandleType.PHONE, "0905 123 456")], "SK") == FIRST


def test_remove_social_matches_by_service_and_user() -> None:
    changed = patch_vcard(FIRST, [RemoveHandle(HandleType.INSTAGRAM, "pato.vesely")], "SK")
    assert card(changed).socials == []


def test_name_changes_fn_and_keeps_the_rest_of_n() -> None:
    changed = patch_vcard(CRLF, [SetName("Thomas Tester", first="Thomas", last="Tester")], "SK")
    parsed = card(changed)
    assert (parsed.formatted_name, parsed.given_name, parsed.family_name) == (
        "Thomas Tester",
        "Thomas",
        "Tester",
    )
    only_display = patch_vcard(CRLF, [SetName("Tommy")], "SK")
    parsed = card(only_display)
    assert (parsed.formatted_name, parsed.given_name) == ("Tommy", "Tom")


def test_long_lines_are_folded_and_parse_back() -> None:
    long_name = "Ábcdéfg " * 12
    changed = patch_vcard(CRLF, [SetName(long_name.strip())], "SK")
    start = changed.index("FN:")
    end = changed.index("\r\n", start)
    while changed[end + 2] == " ":
        end = changed.index("\r\n", end + 2)
    assert all(len(line) <= 75 for line in changed[start:end].split("\r\n"))
    assert card(changed).formatted_name == long_name.strip()
