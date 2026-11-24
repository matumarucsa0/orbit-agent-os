"""vCard 3.0 parser for Apple Contacts exports and iCloud CardDAV data.

Only what Orbit uses is extracted; PHOTO and other properties are skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from orbit.domain.normalize import normalize_text

_BDAY = re.compile(r"^(\d{4}|--)-?(\d{2})-?(\d{2})")
MIN_YEAR, MAX_YEAR = 1900, 2100
_APPLE_LABEL = re.compile(r"^_\$!<(.*)>!\$_$")
_IGNORED_TYPES = {"pref", "voice", "internet", "x400"}
_SCHEMES = ("x-apple:", "xmpp:", "tel:", "mailto:", "skype:", "aim:", "ymsgr:", "msnim:")

# Address country names as Apple writes them (localized), for when X-ABADR is missing.
_COUNTRY_NAMES = {
    "SK": ("slovakia", "slovensko", "slovenska republika", "slovak republic"),
    "CZ": ("czech republic", "czechia", "cesko", "ceska republika", "czech"),
    "AT": ("austria", "rakusko", "rakousko", "osterreich"),
    "DE": ("germany", "nemecko", "deutschland"),
    "HU": ("hungary", "madarsko", "magyarorszag"),
    "PL": ("poland", "polsko", "polska"),
    "GB": ("united kingdom", "uk", "great britain", "velka britania", "velka britanie"),
    "US": ("united states", "usa", "united states of america", "spojene staty"),
}
_COUNTRY_LOOKUP = {name: code for code, names in _COUNTRY_NAMES.items() for name in names}


@dataclass(frozen=True)
class LabeledValue:
    value: str
    label: str | None = None


@dataclass(frozen=True)
class SocialProfile:
    service: str  # lowercase, e.g. "instagram", "telegram", "linkedin", "twitter"
    value: str


@dataclass(frozen=True)
class Birthday:
    month: int
    day: int
    year: int | None = None


@dataclass
class VCard:
    uid: str | None = None
    formatted_name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    organization: str | None = None
    nicknames: list[str] = field(default_factory=list)
    phones: list[LabeledValue] = field(default_factory=list)
    emails: list[LabeledValue] = field(default_factory=list)
    socials: list[SocialProfile] = field(default_factory=list)
    birthday: Birthday | None = None
    city: str | None = None
    country_code: str | None = None
    note: str | None = None


@dataclass
class _Property:
    group: str | None
    name: str
    params: dict[str, list[str]]
    value: str


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_unescaped(value: str, separator: str) -> list[str]:
    parts, current, escaped = [], [], False
    for ch in value:
        if escaped:
            current.append("\\" + ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _unescape(value: str) -> str:
    out, escaped = [], False
    for ch in value:
        if escaped:
            out.append("\n" if ch in "nN" else ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    return "".join(out).strip()


def _parse_line(line: str) -> _Property | None:
    # The value starts at the first ":" outside double quotes.
    in_quotes = False
    for index, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            head, value = line[:index], line[index + 1 :]
            break
    else:
        return None
    name_part, *param_parts = head.split(";")
    group, _, name = name_part.rpartition(".")
    params: dict[str, list[str]] = {}
    for part in param_parts:
        key, sep, raw = part.partition("=")
        if not sep:  # vCard 2.1 bare type, e.g. TEL;CELL:
            key, raw = "type", key
        values = [v.strip().strip('"') for v in raw.split(",") if v.strip()]
        params.setdefault(key.strip().lower(), []).extend(values)
    return _Property(group or None, name.strip().upper(), params, value)


def _label(prop: _Property, group_labels: dict[str, str]) -> str | None:
    if prop.group and prop.group in group_labels:
        return group_labels[prop.group]
    types = [t.lower() for t in prop.params.get("type", []) if t.lower() not in _IGNORED_TYPES]
    return types[0] if types else None


def _strip_scheme(value: str) -> str:
    lowered = value.lower()
    for scheme in _SCHEMES:
        if lowered.startswith(scheme):
            return value[len(scheme) :]
    return value


def _parse_birthday(prop: _Property) -> Birthday | None:
    match = _BDAY.match(prop.value.strip())
    if not match:
        return None
    year_raw, month, day = match.groups()
    year = None if year_raw == "--" or "x-apple-omit-year" in prop.params else int(year_raw)
    if year is not None and not MIN_YEAR <= year <= MAX_YEAR:
        # Apple's "no year" placeholder (1604) also arrives via iCloud CardDAV without the
        # X-APPLE-OMIT-YEAR flag; keep the day and month.
        year = None
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None
    return Birthday(month=int(month), day=int(day), year=year)


def country_code_from_name(name: str) -> str | None:
    normalized = normalize_text(name)
    if len(normalized) == 2 and normalized.isalpha():
        return normalized.upper()
    return _COUNTRY_LOOKUP.get(normalized)


def _build(props: list[_Property]) -> VCard:
    card = VCard()
    group_labels: dict[str, str] = {}
    group_adr_country: dict[str, str] = {}
    for prop in props:
        if prop.name == "X-ABLABEL" and prop.group:
            raw = _unescape(prop.value)
            match = _APPLE_LABEL.match(raw)
            group_labels[prop.group] = (match.group(1) if match else raw).lower()
        elif prop.name == "X-ABADR" and prop.group and prop.value.strip():
            group_adr_country[prop.group] = prop.value.strip().upper()

    for prop in props:
        name = prop.name
        if name == "UID":
            card.uid = prop.value.strip() or None
        elif name == "FN":
            card.formatted_name = _unescape(prop.value) or None
        elif name == "N":
            parts = [_unescape(p) for p in _split_unescaped(prop.value, ";")]
            card.family_name = (parts[0] if parts else "") or None
            card.given_name = (parts[1] if len(parts) > 1 else "") or None
        elif name == "ORG":
            card.organization = _unescape(_split_unescaped(prop.value, ";")[0]) or None
        elif name == "NICKNAME":
            card.nicknames.extend(
                n for n in (_unescape(p) for p in _split_unescaped(prop.value, ",")) if n
            )
        elif name == "TEL":
            value = _strip_scheme(_unescape(prop.value))
            if value:
                card.phones.append(LabeledValue(value, _label(prop, group_labels)))
        elif name == "EMAIL":
            value = _strip_scheme(_unescape(prop.value))
            if value:
                card.emails.append(LabeledValue(value, _label(prop, group_labels)))
        elif name == "BDAY":
            card.birthday = _parse_birthday(prop)
        elif name == "X-SOCIALPROFILE":
            service = (prop.params.get("type") or ["other"])[0].lower()
            user = (prop.params.get("x-user") or [""])[0]
            value = user or _strip_scheme(_unescape(prop.value))
            if value:
                card.socials.append(SocialProfile(service, value))
        elif name == "IMPP":
            service = (prop.params.get("x-service-type") or prop.params.get("type") or ["other"])[0]
            value = _strip_scheme(_unescape(prop.value))
            if value:
                card.socials.append(SocialProfile(service.lower(), value))
        elif name == "ADR" and card.city is None and card.country_code is None:
            parts = [_unescape(p) for p in _split_unescaped(prop.value, ";")]
            parts += [""] * (7 - len(parts))
            card.city = parts[3] or None
            card.country_code = group_adr_country.get(prop.group or "") or (
                country_code_from_name(parts[6]) if parts[6] else None
            )
        elif name == "NOTE":
            card.note = _unescape(prop.value) or None
    return card


def parse_vcards(text: str) -> list[VCard]:
    cards: list[VCard] = []
    current: list[_Property] | None = None
    for line in _unfold(text):
        if not line.strip():
            continue
        prop = _parse_line(line)
        if prop is None:
            continue
        if prop.name == "BEGIN" and prop.value.strip().upper() == "VCARD":
            current = []
        elif prop.name == "END" and prop.value.strip().upper() == "VCARD":
            if current is not None:
                cards.append(_build(current))
            current = None
        elif current is not None:
            current.append(prop)
    return cards
