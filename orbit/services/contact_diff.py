"""Three-way diff between Orbit and an Apple contact (SPEC §2.13). Pure: no database, no I/O.

A *snapshot* is a flat dict, field key -> value. Scalars have a fixed key (`name`, `first_name`,
`last_name`, `city`, `birthday`); every element of a multi-value field has its own key
(`phone:+447700900123`, `email:a@b.c`, `social:instagram:pato`, `alias:pato`, `note:<hash>`).
A missing key means "not set". `base` is the snapshot both sides had at the last sync.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from orbit.domain.enums import HandleType, SyncDirection, SyncKind
from orbit.domain.normalize import normalize_handle, normalize_text, normalize_whitespace
from orbit.domain.people import Person
from orbit.integrations.vcard import (
    Birthday,
    LabeledValue,
    SocialProfile,
    VCard,
    parse_vcards,
)

Snapshot = dict[str, Any]

SOCIAL_TYPES = {
    "instagram": HandleType.INSTAGRAM,
    "telegram": HandleType.TELEGRAM,
    "linkedin": HandleType.LINKEDIN,
    "discord": HandleType.DISCORD,
    "whatsapp": HandleType.WHATSAPP,
}
# Handle types written to an Apple card; the rest only ever flow Apple -> Orbit.
PUSH_SOCIAL = frozenset({"instagram", "telegram", "linkedin"})
_SCALARS = ("name", "first_name", "last_name", "city", "birthday")
# An absent value on the Apple side never clears or deletes these in Orbit.
_NEVER_CLEAR = frozenset({"first_name", "last_name", "city", "alias", "note"})


def family(key: str) -> str:
    """The field a key belongs to: "phone:+4219.." -> "phone"."""
    if key in _SCALARS:
        return key
    head, _, rest = key.partition(":")
    if head == "social":
        return "social:" + rest.partition(":")[0]
    return head


def is_scalar(key: str) -> bool:
    return key in _SCALARS


def is_pushable(key: str) -> bool:
    fam = family(key)
    if fam in ("name", "birthday", "phone", "email"):
        return True
    return fam.startswith("social:") and fam.partition(":")[2] in PUSH_SOCIAL


def key_label(key: str) -> str:
    fam = family(key)
    if fam.startswith("social:"):
        return fam.partition(":")[2].capitalize()
    return {
        "name": "Name",
        "first_name": "First name",
        "last_name": "Last name",
        "city": "City",
        "birthday": "Birthday",
        "phone": "Phone",
        "email": "Email",
        "alias": "Nickname",
        "note": "Note",
    }.get(fam, fam)


def note_key(text: str) -> str:
    return "note:" + hashlib.sha1(normalize_text(text).encode()).hexdigest()[:12]


def card_name(card: VCard) -> str | None:
    name = card.formatted_name or " ".join(
        part for part in (card.given_name, card.family_name) if part
    )
    name = name or card.organization or ""
    if not name.strip():
        name = next((h.value for h in [*card.emails, *card.phones]), "")
    return name.strip()[:200] or None


def _handle_key(handle_type: HandleType, value_norm: str) -> str:
    if handle_type is HandleType.PHONE:
        return f"phone:{value_norm}"
    if handle_type is HandleType.EMAIL:
        return f"email:{value_norm}"
    return f"social:{handle_type.value}:{value_norm}"


def snapshot_card(card: VCard, region: str) -> Snapshot:
    snap: Snapshot = {}
    name = card_name(card)
    if name:
        snap["name"] = name
    if card.given_name:
        snap["first_name"] = card.given_name
    if card.family_name:
        snap["last_name"] = card.family_name
    if card.city:
        snap["city"] = card.city
    if card.birthday is not None:
        snap["birthday"] = [card.birthday.month, card.birthday.day, card.birthday.year]

    def add(handle_type: HandleType, value: str, label: str | None) -> None:
        norm = normalize_handle(handle_type, value, region)
        if norm:
            snap[_handle_key(handle_type, norm)] = {"value": value, "label": label}

    for phone in card.phones:
        add(HandleType.PHONE, phone.value, phone.label)
    for email in card.emails:
        add(HandleType.EMAIL, email.value, email.label)
    for social in card.socials:
        handle_type = SOCIAL_TYPES.get(social.service, HandleType.OTHER)
        add(handle_type, social.value, social.service if handle_type is HandleType.OTHER else None)
    for nickname in card.nicknames:
        snap[f"alias:{normalize_text(nickname)}"] = {"value": nickname}
    if card.note:
        snap[note_key(card.note)] = {"value": card.note}
    return snap


_ORBIT_HANDLE_TYPES = {
    HandleType.PHONE,
    HandleType.EMAIL,
    HandleType.INSTAGRAM,
    HandleType.TELEGRAM,
    HandleType.LINKEDIN,
    HandleType.DISCORD,
    HandleType.WHATSAPP,
    HandleType.OTHER,
}


def snapshot_person(person: Person) -> Snapshot:
    snap: Snapshot = {"name": person.display_name}
    if person.first_name:
        snap["first_name"] = person.first_name
    if person.last_name:
        snap["last_name"] = person.last_name
    if person.city:
        snap["city"] = person.city
    if person.birthday_month is not None and person.birthday_day is not None:
        snap["birthday"] = [person.birthday_month, person.birthday_day, person.birthday_year]
    for handle in person.handles:
        if handle.type in _ORBIT_HANDLE_TYPES and handle.value_norm:
            snap[_handle_key(handle.type, handle.value_norm)] = {
                "value": handle.value,
                "label": handle.label,
            }
    for alias in person.aliases:
        snap[f"alias:{alias.value_norm}"] = {"value": alias.value}
    for note in person.notes:
        snap[note_key(note.text)] = {"value": note.text}
    return snap


def same(key: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if key == "birthday":
        return list(a) == list(b)
    if is_scalar(key):
        return normalize_whitespace(str(a)).casefold() == normalize_whitespace(str(b)).casefold()
    return True  # set elements: the key is the identity


@dataclass(frozen=True)
class FieldChange:
    key: str
    direction: SyncDirection
    kind: SyncKind
    old: Any  # target side's current value (a conflict: the Orbit value)
    new: Any  # value to write (a conflict: the Apple value)


def diff(base: Snapshot, orbit: Snapshot, apple: Snapshot) -> list[FieldChange]:
    """What differs between Orbit and Apple, given the base both had at the last sync."""
    changes: list[FieldChange] = []
    families_orbit = {family(k) for k in orbit}
    families_apple = {family(k) for k in apple}
    for key in sorted({*base, *orbit, *apple}):
        b, o, a = base.get(key), orbit.get(key), apple.get(key)
        if same(key, o, a):
            continue
        orbit_changed = not same(key, o, b)
        apple_changed = not same(key, a, b)
        if apple_changed and not orbit_changed:
            if a is None and family(key) in _NEVER_CLEAR:
                continue
            empty_target = (
                o is None if is_scalar(key) else family(key) not in families_orbit
            ) and a is not None
            kind = SyncKind.AUTO if empty_target and key != "name" else SyncKind.PROPOSAL
            changes.append(FieldChange(key, SyncDirection.PULL, kind, o, a))
        elif orbit_changed and not apple_changed:
            if not is_pushable(key):
                continue
            empty_target = (
                a is None if is_scalar(key) else family(key) not in families_apple
            ) and o is not None
            kind = SyncKind.AUTO if empty_target and key != "name" else SyncKind.PROPOSAL
            changes.append(FieldChange(key, SyncDirection.PUSH, kind, a, o))
        else:  # both changed, and they differ
            if key in _NEVER_CLEAR and a is None:
                continue
            changes.append(FieldChange(key, SyncDirection.PULL, SyncKind.CONFLICT, o, a))
    return changes


def settle_equal(base: Snapshot, orbit: Snapshot, apple: Snapshot) -> Snapshot:
    """The new base: wherever Orbit and Apple already agree, that value is the base from now on."""
    new_base = dict(base)
    for key in {*base, *orbit, *apple}:
        if same(key, orbit.get(key), apple.get(key)):
            if orbit.get(key) is None:
                new_base.pop(key, None)
            else:
                new_base[key] = orbit[key]
    return new_base


def reject_hash(subject: str, key: str, direction: SyncDirection, new: Any) -> str:
    """Stable id of "this value for this field of this person", for the rejection memory."""
    raw = json.dumps([subject, key, direction.value, new], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def describe(key: str, value: Any) -> str:
    """A human-readable value for the review screen."""
    if value is None:
        return "not set"
    if key == "birthday":
        month, day, year = value
        text = f"{day}.{month}."
        return f"{text}{year}" if year else text
    if isinstance(value, dict):
        return str(value.get("value", ""))
    return str(value)


def card_to_json(card: VCard) -> dict[str, Any]:
    return asdict(card)


def card_from_json(data: dict[str, Any]) -> VCard:
    values = dict(data)
    values["phones"] = [LabeledValue(**p) for p in values.get("phones", [])]
    values["emails"] = [LabeledValue(**e) for e in values.get("emails", [])]
    values["socials"] = [SocialProfile(**s) for s in values.get("socials", [])]
    birthday = values.get("birthday")
    values["birthday"] = Birthday(**birthday) if birthday else None
    return VCard(**values)


def parse_card(text: str) -> VCard:
    cards = parse_vcards(text)
    return cards[0] if cards else VCard()
