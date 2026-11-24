"""Normalization for matching and uniqueness (`value_norm` columns, search, fuzzy matching)."""

from __future__ import annotations

import re
import unicodedata

import phonenumbers

from orbit.domain.enums import PHONE_HANDLE_TYPES, HandleType

_WHITESPACE = re.compile(r"\s+")
_URL_SCHEME = re.compile(r"^(?:https?://)?(?:www\.)?", re.IGNORECASE)
_LINKEDIN_SLUG = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)
_PROFILE_HOSTS: dict[HandleType, tuple[str, ...]] = {
    HandleType.INSTAGRAM: ("instagram.com/", "instagr.am/"),
    HandleType.TELEGRAM: ("t.me/", "telegram.me/"),
}


def strip_diacritics(value: str) -> str:
    """Remove combining marks: "Tobiáš Ďurčo" -> "Tobias Durco"."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_whitespace(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def normalize_text(value: str) -> str:
    """Casefold, strip diacritics, collapse whitespace: "  Paťo  Veselý " -> "pato vesely"."""
    return normalize_whitespace(strip_diacritics(value.casefold()))


def compact_iban(value: str) -> str:
    """ "SK31 1200 0000 …" -> "SK311200000…" (the `iban` handle's `value_norm`)."""
    return "".join(value.split()).upper()


def mask_iban(value: str) -> str:
    """Keep the country and the last 4 characters: "SK** **** 7541"."""
    compact = compact_iban(value)
    return f"{compact[:2]}** **** {compact[-4:]}"


def normalize_phone(raw: str, region: str | None) -> str | None:
    """Return the E.164 form of a phone number, or None if it can't be resolved.

    `region` (ISO 3166-1 alpha-2, e.g. "SK") is used only for numbers without an international
    prefix; numbers starting with "+" or "00" resolve without it.
    """
    candidate = raw.strip()
    if candidate.startswith("00"):
        candidate = "+" + candidate[2:]
    try:
        parsed = phonenumbers.parse(candidate, region.upper() if region else None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def phone_digits(raw: str) -> str:
    """Digits-only fallback for numbers that can't be resolved to E.164 (keeps a leading "+")."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return "+" + digits if raw.strip().startswith("+") and digits else digits


def _profile_username(handle_type: HandleType, value: str) -> str:
    candidate = _URL_SCHEME.sub("", value.strip())
    for host in _PROFILE_HOSTS.get(handle_type, ()):
        if candidate.casefold().startswith(host):
            candidate = candidate[len(host) :]
            break
    candidate = candidate.split("?", 1)[0].split("#", 1)[0].strip("/")
    return candidate.lstrip("@").strip().casefold()


def normalize_handle(handle_type: HandleType, value: str, region: str | None) -> str:
    """The `value_norm` used for uniqueness and matching; "" means the value is unusable.

    `region` is the person's country, used only for phone numbers without a prefix.
    """
    if handle_type in PHONE_HANDLE_TYPES:
        return normalize_phone(value, region) or phone_digits(value)
    if handle_type is HandleType.EMAIL:
        return value.strip().casefold()
    if handle_type in _PROFILE_HOSTS:
        return _profile_username(handle_type, value)
    if handle_type is HandleType.DISCORD:
        return value.strip().lstrip("@").casefold()
    if handle_type is HandleType.LINKEDIN:
        match = _LINKEDIN_SLUG.search(value)
        slug = match.group(1) if match else value
        return slug.strip().strip("/").casefold()
    if handle_type is HandleType.IBAN:
        return compact_iban(value)
    return normalize_text(value)
