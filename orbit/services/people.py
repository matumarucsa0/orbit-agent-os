"""People CRUD plus aliases, handles, tags and notes. Callers own the transaction (commit)."""

from __future__ import annotations

import calendar
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    PHONE_HANDLE_TYPES,
    HandleType,
    NamedayCountry,
    PromiseStatus,
    ProposalKind,
)
from orbit.domain.normalize import normalize_handle, normalize_text
from orbit.domain.people import (
    Alias,
    Handle,
    Interaction,
    Note,
    Person,
    Tag,
    interaction_person,
    person_tag,
)
from orbit.integrations import namedays
from orbit.services.errors import HandleConflictError, InvalidInputError, NotFoundError
from orbit.services.proposal_kinds import Applied, ApplyContext, ProposalHandler

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CountryCode = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{2}$")
]

_PERSON_FIELDS = (
    "display_name",
    "first_name",
    "last_name",
    "birthday_month",
    "birthday_day",
    "birthday_year",
    "nameday_country",
    "nameday_name",
    "country",
    "city",
    "how_met",
    "met_through_id",
    "photo_url",
    "archived",
    "cadence_days",
)


class PersonUpdate(BaseModel):
    """PATCH semantics: only fields that were set are applied."""

    model_config = ConfigDict(str_strip_whitespace=True)

    display_name: Name | None = None
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    birthday_month: int | None = Field(default=None, ge=1, le=12)
    birthday_day: int | None = Field(default=None, ge=1, le=31)
    birthday_year: int | None = Field(default=None, ge=1900, le=2100)
    nameday_country: NamedayCountry | None = None
    # A name from the name-day calendar that overrides first_name (e.g. "Marián" for "Majo").
    nameday_name: str | None = Field(default=None, max_length=100)
    country: CountryCode | None = None
    city: str | None = Field(default=None, max_length=100)
    how_met: str | None = None
    met_through_id: int | None = None
    photo_url: str | None = Field(default=None, max_length=500)
    archived: bool | None = None
    # Keep in touch every N days; None = from the person's tags.
    cadence_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PersonCreate(PersonUpdate):
    display_name: Name
    nameday_country: NamedayCountry | None = NamedayCountry.SK
    archived: bool | None = False


def refresh_search_name(person: Person) -> None:
    """Recompute `search_name_norm` from the display, first and last name (call after any name
    change)."""
    parts = [person.display_name, person.first_name or "", person.last_name or ""]
    person.search_name_norm = normalize_text(" ".join(parts))


def _check_birthday(month: int | None, day: int | None, year: int | None) -> None:
    if (month is None) != (day is None):
        raise InvalidInputError("Birthday needs both a month and a day.")
    if year is not None and month is None:
        raise InvalidInputError("Birthday year needs a month and a day.")
    if month is not None and day is not None:
        # Without a year, allow Feb 29 (checked against a leap year).
        max_day = calendar.monthrange(year if year is not None else 2000, month)[1]
        if day > max_day:
            raise InvalidInputError("That birthday date doesn't exist.")


def _calendar_spelling(name: str, country: str | None) -> str:
    """Only names from the person's name-day calendar are accepted, in the calendar's spelling."""
    if not country:
        raise InvalidInputError("Pick a name-day country before assigning a name-day name.")
    spelled = namedays.canonical(name, country)
    if spelled is None:
        raise InvalidInputError(
            f"{name} isn't in the {country} name-day calendar; clear or change the name-day name."
        )
    return spelled


def get_person(session: Session, person_id: int) -> Person:
    """The person with this id; raises NotFoundError."""
    person = session.get(Person, person_id)
    if person is None:
        raise NotFoundError(f"Person {person_id} not found.")
    return person


def _changes(
    session: Session, person: Person, data: PersonUpdate, fields: set[str]
) -> dict[str, Any]:
    """Validate the requested changes against the person's current state, without mutating it."""
    changes: dict[str, Any] = {}
    for name in _PERSON_FIELDS:
        if name not in fields:
            continue
        value = getattr(data, name)
        if name == "display_name" and value is None:
            raise InvalidInputError("A person needs a name.")
        if name == "archived" and value is None:
            continue
        changes[name] = value.value if isinstance(value, NamedayCountry) else value

    def merged(name: str) -> Any:
        return changes[name] if name in changes else getattr(person, name)

    _check_birthday(merged("birthday_month"), merged("birthday_day"), merged("birthday_year"))
    if ("nameday_name" in changes or "nameday_country" in changes) and merged("nameday_name"):
        # Re-checked when the country changes too: a Slovak-only name can't stay on a CZ person.
        changes["nameday_name"] = _calendar_spelling(
            merged("nameday_name"), merged("nameday_country")
        )
    met_through_id = changes.get("met_through_id")
    if met_through_id is not None:
        if person.id is not None and met_through_id == person.id:
            raise InvalidInputError("A person can't be introduced through themselves.")
        with session.no_autoflush:
            get_person(session, met_through_id)
    return changes


def create_person(session: Session, data: PersonCreate) -> Person:
    """Create a person from validated input (a name-day country defaults to SK); flushes, the caller
    commits."""
    person = Person(archived=False)
    changes = _changes(session, person, data, data.model_fields_set | {"nameday_country"})
    for name, value in changes.items():
        setattr(person, name, value)
    refresh_search_name(person)
    session.add(person)
    session.flush()
    return person


def update_person(session: Session, person: Person, data: PersonUpdate) -> Person:
    """Apply only the fields that were set (PATCH semantics); a changed country re-normalises phone
    handles."""
    changes = _changes(session, person, data, data.model_fields_set)
    old_country = person.country
    for name, value in changes.items():
        setattr(person, name, value)
    refresh_search_name(person)
    if person.country != old_country:
        _renormalize_phones(session, person)
    session.flush()
    return person


def _renormalize_phones(session: Session, person: Person) -> None:
    for handle in person.handles:
        if handle.type not in PHONE_HANDLE_TYPES:
            continue
        new_norm = normalize_handle(handle.type, handle.value, person.country)
        if new_norm == handle.value_norm:
            continue
        owner = _handle_owner(session, handle.type, new_norm)
        if owner is None or owner.id == person.id:
            handle.value_norm = new_norm


def delete_person(session: Session, person: Person) -> None:
    """Delete a person with everything attached. Refused while money, items, favors or
    promises are still open: those shouldn't vanish silently (archive the person instead)."""
    if any(d.settled_at is None for d in person.debts) or any(
        p.status is PromiseStatus.OPEN for p in person.promises
    ):
        raise InvalidInputError(
            f"{person.display_name} still has open debts or promises. Settle or close them "
            "first, or archive instead."
        )
    linked = list(person.interactions)
    interaction_ids = [i.id for i in linked]
    session.delete(person)
    session.flush()
    for interaction in linked:
        session.expire(interaction, ["people"])
    if interaction_ids:
        # Interactions nobody is linked to any more go too.
        orphaned = select(Interaction.id).where(
            Interaction.id.in_(interaction_ids),
            ~select(interaction_person.c.interaction_id)
            .where(interaction_person.c.interaction_id == Interaction.id)
            .exists(),
        )
        session.execute(delete(Interaction).where(Interaction.id.in_(orphaned)))
        session.flush()


# --- Aliases ---


def add_alias(session: Session, person: Person, value: str) -> Alias:
    """Add an alias (stored with its normalised form); an alias the person already has is returned
    as it is."""
    value = value.strip()
    value_norm = normalize_text(value)
    if not value_norm:
        raise InvalidInputError("Alias can't be empty.")
    for alias in person.aliases:
        if alias.value_norm == value_norm:
            return alias
    alias = Alias(value=value, value_norm=value_norm)
    person.aliases.append(alias)
    session.flush()
    return alias


def remove_alias(session: Session, person: Person, alias_id: int) -> None:
    """Remove one of the person's aliases by id; raises NotFoundError."""
    alias = next((a for a in person.aliases if a.id == alias_id), None)
    if alias is None:
        raise NotFoundError(f"Alias {alias_id} not found.")
    person.aliases.remove(alias)
    session.flush()


# --- Handles ---


def _handle_owner(session: Session, handle_type: HandleType, value_norm: str) -> Person | None:
    return session.scalar(
        select(Person)
        .join(Handle, Handle.person_id == Person.id)
        .where(Handle.type == handle_type, Handle.value_norm == value_norm)
    )


def find_person_by_handle(
    session: Session, handle_type: HandleType, value: str, region: str | None
) -> Person | None:
    """The person who owns this handle (matched on its normalised value), or None."""
    value_norm = normalize_handle(handle_type, value, region)
    return _handle_owner(session, handle_type, value_norm) if value_norm else None


def add_handle(
    session: Session,
    person: Person,
    handle_type: HandleType,
    value: str,
    label: str | None = None,
) -> Handle:
    """Add a handle (phone, email, IBAN, social) normalised for matching. A handle the person
    already has is returned as it is; one owned by another person raises HandleConflictError."""
    value = value.strip()
    value_norm = normalize_handle(handle_type, value, person.country)
    if not value_norm:
        raise InvalidInputError(f"That doesn't look like a valid {handle_type.value} handle.")
    owner = _handle_owner(session, handle_type, value_norm)
    if owner is not None and owner.id != person.id:
        raise HandleConflictError(owner.id, owner.display_name)
    for handle in person.handles:
        if handle.type == handle_type and handle.value_norm == value_norm:
            return handle
    handle = Handle(
        type=handle_type, value=value, value_norm=value_norm, label=(label or "").strip() or None
    )
    person.handles.append(handle)
    session.flush()
    return handle


def remove_handle(session: Session, person: Person, handle_id: int) -> None:
    """Remove one of the person's handles by id; raises NotFoundError."""
    handle = next((h for h in person.handles if h.id == handle_id), None)
    if handle is None:
        raise NotFoundError(f"Handle {handle_id} not found.")
    person.handles.remove(handle)
    session.flush()


# --- Tags ---


def get_or_create_tag(session: Session, name: str) -> Tag:
    """The tag with this name (matched case- and diacritics-insensitively), created when missing."""
    name = " ".join(name.split())
    name_norm = normalize_text(name)
    if not name_norm:
        raise InvalidInputError("Tag can't be empty.")
    tag = session.scalar(select(Tag).where(Tag.name_norm == name_norm))
    if tag is None:
        tag = Tag(name=name, name_norm=name_norm)
        session.add(tag)
        session.flush()
    return tag


def add_tag(session: Session, person: Person, name: str) -> Tag:
    """Tag the person; adding a tag they already have does nothing."""
    tag = get_or_create_tag(session, name)
    if tag not in person.tags:
        person.tags.append(tag)
        session.flush()
    return tag


def remove_tag(session: Session, person: Person, tag_id: int) -> None:
    """Remove a tag from the person (the tag itself stays); raises NotFoundError."""
    tag = next((t for t in person.tags if t.id == tag_id), None)
    if tag is None:
        raise NotFoundError(f"Tag {tag_id} not found on this person.")
    person.tags.remove(tag)
    session.flush()


def list_tags(session: Session) -> list[tuple[Tag, int]]:
    """All tags with the number of (non-archived) people carrying them, by name."""
    count = func.count(Person.id)
    rows = session.execute(
        select(Tag, count)
        .outerjoin(person_tag, person_tag.c.tag_id == Tag.id)
        .outerjoin(Person, (Person.id == person_tag.c.person_id) & Person.archived.is_(False))
        .group_by(Tag.id)
        .order_by(Tag.name_norm)
    ).all()
    return [(tag, n) for tag, n in rows]


# --- Notes ---


def add_note(session: Session, person: Person, text: str) -> Note:
    """Add a note to the person; an empty note raises InvalidInputError."""
    text = text.strip()
    if not text:
        raise InvalidInputError("Note can't be empty.")
    note = Note(text=text, text_norm=normalize_text(text))
    person.notes.append(note)
    session.flush()
    return note


def get_note(session: Session, person: Person, note_id: int) -> Note:
    """The person's note with this id; raises NotFoundError."""
    note = session.get(Note, note_id)
    if note is None or note.person_id != person.id:
        raise NotFoundError(f"Note {note_id} not found.")
    return note


def update_note(session: Session, note: Note, text: str) -> Note:
    """Replace the text of a note; an empty text raises InvalidInputError."""
    text = text.strip()
    if not text:
        raise InvalidInputError("Note can't be empty.")
    note.text = text
    note.text_norm = normalize_text(text)
    session.flush()
    return note


def delete_note(session: Session, note: Note) -> None:
    """Delete a note."""
    note.person.notes.remove(note)  # delete-orphan cascade deletes the row
    session.flush()


# --- The "add alias" proposal (SPEC §2.28) ---


class AddAliasPayload(BaseModel):
    """Made when a capture card linked a new name to an existing person."""

    person_id: int
    alias: str = Field(min_length=1, max_length=200)


def _confirm_add_alias(session: Session, payload: AddAliasPayload, ctx: ApplyContext) -> Applied:
    person = get_person(session, payload.person_id)
    add_alias(session, person, payload.alias)
    return Applied(f"Added “{payload.alias}” as a name of {person.display_name}.", person.id)


ADD_ALIAS_HANDLER = ProposalHandler(ProposalKind.ADD_ALIAS, AddAliasPayload, _confirm_add_alias)
