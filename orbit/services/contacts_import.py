"""One-way, idempotent contact import from vCards (a .vcf upload or iCloud CardDAV).

Matching: vCard UID -> phone/email/social handle -> unique exact name (only people not already
linked to another contact). Existing people only get *empty* fields filled; nothing is ever
overwritten or deleted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import HandleType
from orbit.domain.normalize import normalize_handle, normalize_text
from orbit.domain.people import Handle, Person
from orbit.integrations.carddav import ContactSource
from orbit.integrations.vcard import VCard, parse_vcards
from orbit.services import people
from orbit.services.errors import HandleConflictError, InvalidInputError

_SOCIAL_TYPES = {
    "instagram": HandleType.INSTAGRAM,
    "telegram": HandleType.TELEGRAM,
    "linkedin": HandleType.LINKEDIN,
    "discord": HandleType.DISCORD,
    "whatsapp": HandleType.WHATSAPP,
}
MAX_NAME = 200


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = f"{self.created} new, {self.updated} updated, {self.unchanged} unchanged"
        if self.skipped:
            text += f", {self.skipped} skipped"
        if self.conflicts:
            text += f", {len(self.conflicts)} conflicts"
        return text


@dataclass(frozen=True)
class _CardHandle:
    type: HandleType
    value: str
    label: str | None


class _ConflictError(Exception):
    pass


def _display_name(card: VCard) -> str | None:
    name = card.formatted_name or " ".join(
        part for part in (card.given_name, card.family_name) if part
    )
    name = name or card.organization or ""
    if not name.strip():
        name = next((h.value for h in [*card.emails, *card.phones]), "")
    return name.strip()[:MAX_NAME] or None


def _card_handles(card: VCard) -> list[_CardHandle]:
    handles = [_CardHandle(HandleType.PHONE, p.value, p.label) for p in card.phones]
    handles += [_CardHandle(HandleType.EMAIL, e.value, e.label) for e in card.emails]
    for social in card.socials:
        handle_type = _SOCIAL_TYPES.get(social.service, HandleType.OTHER)
        label = social.service if handle_type is HandleType.OTHER else None
        handles.append(_CardHandle(handle_type, social.value, label))
    return handles


def _birthday_fields(card: VCard) -> dict[str, Any]:
    if card.birthday is None:
        return {}
    return {
        "birthday_month": card.birthday.month,
        "birthday_day": card.birthday.day,
        "birthday_year": card.birthday.year,
    }


def _match(
    session: Session, card: VCard, name: str, handles: list[_CardHandle], region: str
) -> Person | None:
    if card.uid:
        person = session.scalar(select(Person).where(Person.vcard_uid == card.uid))
        if person is not None:
            return person

    owners: dict[int, Person] = {}
    for handle in handles:
        value_norm = normalize_handle(handle.type, handle.value, region)
        if not value_norm:
            continue
        owner = session.scalar(
            select(Person)
            .join(Handle, Handle.person_id == Person.id)
            .where(Handle.type == handle.type, Handle.value_norm == value_norm)
        )
        if owner is None:
            continue
        if owner.vcard_uid and card.uid and owner.vcard_uid != card.uid:
            # Already linked to a *different* contact (e.g. a shared family landline): not the
            # same person. The shared handle is reported as a conflict when adding handles.
            continue
        owners[owner.id] = owner
    if len(owners) > 1:
        names = ", ".join(sorted(p.display_name for p in owners.values()))
        raise _ConflictError(f"{name}: phone/email belong to different people ({names}); skipped.")
    if owners:
        return next(iter(owners.values()))

    name_norm = normalize_text(name)
    same_name = [
        p
        for p in session.scalars(select(Person).where(Person.vcard_uid.is_(None)))
        if normalize_text(p.display_name) == name_norm
    ]
    return same_name[0] if len(same_name) == 1 else None


def _add_extras(
    session: Session,
    person: Person,
    card: VCard,
    handles: list[_CardHandle],
    report: ImportReport,
) -> bool:
    """Add missing aliases, handles and the note. Returns True if anything was added."""
    changed = False
    alias_norms = {a.value_norm for a in person.aliases}
    for nickname in card.nicknames:
        if normalize_text(nickname) not in alias_norms:
            people.add_alias(session, person, nickname)
            alias_norms.add(normalize_text(nickname))
            changed = True

    existing = {(h.type, h.value_norm) for h in person.handles}
    for handle in handles:
        value_norm = normalize_handle(handle.type, handle.value, person.country)
        if not value_norm or (handle.type, value_norm) in existing:
            continue
        try:
            people.add_handle(session, person, handle.type, handle.value, handle.label)
        except HandleConflictError as exc:
            report.conflicts.append(
                f"{person.display_name}: {handle.type.value} {handle.value} already belongs to "
                f"{exc.owner_name}."
            )
            continue
        except InvalidInputError:
            continue
        existing.add((handle.type, value_norm))
        changed = True

    if card.note:
        note_norm = normalize_text(card.note)
        if all(n.text_norm != note_norm for n in person.notes):
            people.add_note(session, person, card.note)
            changed = True
    return changed


def _create(
    session: Session,
    card: VCard,
    name: str,
    handles: list[_CardHandle],
    region: str,
    report: ImportReport,
) -> Person:
    fields: dict[str, Any] = {
        "display_name": name,
        "first_name": card.given_name,
        "last_name": card.family_name,
        "country": region,
        "city": card.city,
    }
    # An impossible birthday (or an over-long field) must not block the import:
    # retry without the birthday, then with just the essentials.
    attempts = [
        {**fields, **_birthday_fields(card)},
        fields,
        {"display_name": name, "country": region},
    ]
    for attempt in attempts:
        try:
            person = people.create_person(session, people.PersonCreate(**attempt))
            break
        except (ValidationError, InvalidInputError):
            continue
    person.vcard_uid = card.uid
    _add_extras(session, person, card, handles, report)
    session.flush()
    return person


def _fill_empty(session: Session, person: Person, card: VCard, region: str) -> bool:
    fills: dict[str, Any] = {}
    if person.first_name is None and card.given_name:
        fills["first_name"] = card.given_name
    if person.last_name is None and card.family_name:
        fills["last_name"] = card.family_name
    if person.city is None and card.city:
        fills["city"] = card.city
    if person.country is None:
        fills["country"] = region
    if person.birthday_month is None and card.birthday is not None:
        fills.update(_birthday_fields(card))
    uid_linked = False
    if card.uid and person.vcard_uid is None:
        taken = session.scalar(select(Person.id).where(Person.vcard_uid == card.uid))
        if taken is None:
            person.vcard_uid = card.uid
            uid_linked = True
    try:
        if fills:
            people.update_person(session, person, people.PersonUpdate(**fills))
    except (ValidationError, InvalidInputError):
        # An impossible birthday must not block the other fields.
        for key in ("birthday_month", "birthday_day", "birthday_year"):
            fills.pop(key, None)
        if fills:
            people.update_person(session, person, people.PersonUpdate(**fills))
    return uid_linked or bool(fills)


def import_vcards(
    session: Session, cards: Iterable[VCard], default_country: str = "SK"
) -> ImportReport:
    report = ImportReport()
    for card in cards:
        name = _display_name(card)
        if name is None:
            report.skipped += 1
            continue
        region = card.country_code or default_country
        handles = _card_handles(card)
        try:
            person = _match(session, card, name, handles, region)
        except _ConflictError as exc:
            report.conflicts.append(str(exc))
            continue
        if person is None:
            _create(session, card, name, handles, region, report)
            report.created += 1
            continue
        filled = _fill_empty(session, person, card, region)
        added = _add_extras(session, person, card, handles, report)
        if filled or added:
            report.updated += 1
        else:
            report.unchanged += 1
    session.flush()
    return report


def import_vcard_text(session: Session, text: str, default_country: str = "SK") -> ImportReport:
    return import_vcards(session, parse_vcards(text), default_country)


def import_from_source(
    session: Session, source: ContactSource, default_country: str = "SK"
) -> ImportReport:
    cards = [card for raw in source.fetch_cards() for card in parse_vcards(raw.text)]
    return import_vcards(session, cards, default_country)


class MatchConflictError(Exception):
    """The card's phone/email belong to several different people."""


def match_card(session: Session, card: VCard, region: str) -> Person | None:
    """The person this card is (UID, handle, unique exact name), or None."""
    name = _display_name(card)
    if name is None:
        return None
    try:
        return _match(session, card, name, _card_handles(card), region)
    except _ConflictError as exc:
        raise MatchConflictError(str(exc)) from exc


def create_from_card(session: Session, card: VCard, region: str) -> Person:
    name = _display_name(card)
    if name is None:
        raise InvalidInputError("That contact has no name, phone or email.")
    return _create(session, card, name, _card_handles(card), region, ImportReport())
