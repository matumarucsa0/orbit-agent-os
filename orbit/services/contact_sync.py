"""Contact sync review (SPEC §2.13): fetch cards, diff against Orbit, apply what I tick.

`start_run` never changes a person or an Apple card; it records a review (`SyncRun` with one
`SyncChange` per difference) and keeps the per-person base up to date. `apply_run` writes only
the changes I chose; the ones I left unticked are remembered as rejected and not proposed again
until their value changes.
"""

from __future__ import annotations

import difflib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.contacts import ContactBackup, ContactLink, SyncChange, SyncRun
from orbit.domain.enums import (
    HandleType,
    SyncDirection,
    SyncKind,
    SyncRunStatus,
    SyncSource,
    SyncState,
)
from orbit.domain.normalize import normalize_text
from orbit.domain.people import Person
from orbit.integrations import vcard_patch as vp
from orbit.integrations.carddav import CardDavError, ContactSource, RawCard
from orbit.integrations.vcard import VCard, parse_vcards
from orbit.services import contact_diff as cd
from orbit.services import people
from orbit.services.contacts_import import MatchConflictError, create_from_card, match_card
from orbit.services.errors import HandleConflictError, InvalidInputError, NotFoundError

FUZZY_NAME_RATIO = 0.86
DEFAULT_REGION = "SK"
# Choices a change accepts when applied; anything else (or nothing) means "skip".
CHOICES: dict[SyncKind, tuple[str, ...]] = {
    SyncKind.AUTO: ("apply",),
    SyncKind.PROPOSAL: ("apply",),
    SyncKind.CONFLICT: ("apple", "orbit"),
    SyncKind.NEW_CONTACT: ("apply",),
    SyncKind.POSSIBLE_MATCH: ("link", "new"),
    SyncKind.REMOVED_UPSTREAM: ("apply",),
}


@dataclass(frozen=True)
class CardInput:
    card: VCard
    href: str | None = None
    etag: str | None = None


def cards_from_raw(raws: Iterable[RawCard]) -> list[CardInput]:
    return [CardInput(card, raw.href, raw.etag) for raw in raws for card in parse_vcards(raw.text)]


@dataclass
class ApplyReport:
    applied: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = f"{self.applied} applied, {self.skipped} skipped"
        return text + (f", {len(self.failed)} failed" if self.failed else "")


# --- Building a run ---


def _add_change(
    session: Session,
    run: SyncRun,
    *,
    person: Person | None,
    name: str,
    key: str,
    direction: SyncDirection,
    kind: SyncKind,
    old: Any,
    new: Any,
    subject: str,
    remembered: set[str],
) -> None:
    digest = cd.reject_hash(subject, key, direction, new)
    if digest in remembered:
        return
    session.add(
        SyncChange(
            run=run,
            person_id=person.id if person else None,
            person_name=name,
            field=key,
            direction=direction,
            kind=kind,
            state=SyncState.PENDING,
            old_json=old,
            new_json=new,
            reject_hash=digest,
        )
    )


def _fuzzy_candidates(session: Session) -> list[tuple[str, Person]]:
    linked = set(session.scalars(select(ContactLink.person_id)))
    return [
        (normalize_text(p.display_name), p)
        for p in session.scalars(select(Person).where(Person.archived.is_(False)))
        if p.id not in linked and p.vcard_uid is None
    ]


def _fuzzy_match(name: str, pool: list[tuple[str, Person]]) -> Person | None:
    target = normalize_text(name)
    best: tuple[float, Person] | None = None
    for norm, person in pool:
        matcher = difflib.SequenceMatcher(None, target, norm)
        if (
            matcher.real_quick_ratio() < FUZZY_NAME_RATIO
            or matcher.quick_ratio() < FUZZY_NAME_RATIO
        ):
            continue
        ratio = matcher.ratio()
        if ratio >= FUZZY_NAME_RATIO and (best is None or ratio > best[0]):
            best = (ratio, person)
    return best[1] if best else None


def start_run(
    session: Session,
    cards: list[CardInput],
    source: SyncSource,
    *,
    default_country: str = DEFAULT_REGION,
    include_push: bool = False,
    now: datetime | None = None,
) -> SyncRun:
    """Diff `cards` against Orbit and record the review. `include_push` adds the Orbit -> Apple
    side (only for a server source, where there is a card to write to)."""
    now = now or utcnow()
    # A review I left half-done is closed, not deleted: what I already applied, rejected or can
    # still undo stays; only the changes I never looked at go.
    for stale in session.scalars(select(SyncRun).where(SyncRun.status == SyncRunStatus.REVIEW)):
        for undecided in [c for c in stale.changes if c.state is SyncState.PENDING]:
            session.delete(undecided)
        session.flush()
        session.refresh(stale)
        if stale.changes:
            stale.status, stale.applied_at = SyncRunStatus.APPLIED, now
        else:
            session.delete(stale)
    session.flush()
    remembered = set(
        session.scalars(
            select(SyncChange.reject_hash).where(SyncChange.state == SyncState.REJECTED)
        )
    )
    run = SyncRun(source=source, started_at=now, status=SyncRunStatus.REVIEW)
    session.add(run)
    session.flush()

    summary: dict[str, Any] = {"cards": 0, "skipped": 0, "conflicts": []}
    pool = _fuzzy_candidates(session)
    seen_uids: set[str] = set()
    links_by_person = {link.person_id: link for link in session.scalars(select(ContactLink))}

    for item in cards:
        card = item.card
        name = cd.card_name(card)
        subject = card.uid or item.href or name or ""
        if name is None or not (card.uid or item.href):
            summary["skipped"] += 1
            continue
        summary["cards"] += 1
        uid = card.uid or item.href or ""
        seen_uids.add(uid)
        region = card.country_code or default_country
        try:
            person = match_card(session, card, region)
        except MatchConflictError as exc:
            summary["conflicts"].append(str(exc))
            continue

        if person is None:
            match = _fuzzy_match(name, pool)
            if match is not None:
                _add_change(
                    session,
                    run,
                    person=match,
                    name=name,
                    key="possible_match",
                    direction=SyncDirection.PULL,
                    kind=SyncKind.POSSIBLE_MATCH,
                    old=match.display_name,
                    new={
                        "card": cd.card_to_json(card),
                        "uid": uid,
                        "href": item.href,
                        "etag": item.etag,
                    },
                    subject=subject,
                    remembered=remembered,
                )
            else:
                _add_change(
                    session,
                    run,
                    person=None,
                    name=name,
                    key="new_contact",
                    direction=SyncDirection.PULL,
                    kind=SyncKind.NEW_CONTACT,
                    old=None,
                    new={
                        "card": cd.card_to_json(card),
                        "uid": uid,
                        "href": item.href,
                        "etag": item.etag,
                    },
                    subject=subject,
                    remembered=remembered,
                )
            continue

        link = links_by_person.get(person.id)
        if link is None:
            if person.vcard_uid is None:
                person.vcard_uid = uid
            link = ContactLink(
                person_id=person.id, carddav_uid=person.vcard_uid or uid, base_json={}
            )
            session.add(link)
            links_by_person[person.id] = link
        if item.href:
            link.href, link.etag = item.href, item.etag
        link.last_synced_at = now

        orbit = cd.snapshot_person(person)
        apple = cd.snapshot_card(card, person.country or region)
        for change in cd.diff(link.base_json or {}, orbit, apple):
            if change.direction is SyncDirection.PUSH and not include_push:
                continue
            _add_change(
                session,
                run,
                person=person,
                name=person.display_name,
                key=change.key,
                direction=change.direction,
                kind=change.kind,
                old=change.old,
                new=change.new,
                subject=f"person:{person.id}",
                remembered=remembered,
            )
        link.base_json = cd.settle_equal(link.base_json or {}, orbit, apple)

    if source is SyncSource.CARDDAV:
        for link in links_by_person.values():
            person = session.get(Person, link.person_id)
            if person is None or person.archived or link.carddav_uid in seen_uids:
                continue
            _add_change(
                session,
                run,
                person=person,
                name=person.display_name,
                key="archive",
                direction=SyncDirection.PULL,
                kind=SyncKind.REMOVED_UPSTREAM,
                old=None,
                new=None,
                subject=f"person:{person.id}",
                remembered=remembered,
            )
    run.summary_json = summary
    session.flush()
    return run


# --- Applying ---


def get_run(session: Session, run_id: int) -> SyncRun:
    run = session.get(SyncRun, run_id)
    if run is None:
        raise NotFoundError(f"Sync run {run_id} not found.")
    return run


def latest_run(session: Session) -> SyncRun | None:
    return session.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))


def default_choice(change: SyncChange) -> str | None:
    """What the review pre-selects: only fills of an empty field are ticked."""
    return "apply" if change.kind is SyncKind.AUTO else None


def _handle_parts(key: str) -> tuple[HandleType, str]:
    fam = cd.family(key)
    value_norm = key.split(":", 1)[1] if fam in ("phone", "email") else key.split(":", 2)[2]
    if fam == "phone":
        return HandleType.PHONE, value_norm
    if fam == "email":
        return HandleType.EMAIL, value_norm
    return HandleType(fam.partition(":")[2]), value_norm


def _write_orbit(session: Session, person: Person, key: str, value: Any) -> None:
    """Make Orbit's `key` equal `value` (None = not set)."""
    fam = cd.family(key)
    if key == "name":
        people.update_person(session, person, people.PersonUpdate(display_name=value))
    elif key in ("first_name", "last_name", "city"):
        people.update_person(session, person, people.PersonUpdate(**{key: value}))
    elif key == "birthday":
        month, day, year = value or (None, None, None)
        people.update_person(
            session,
            person,
            people.PersonUpdate(birthday_month=month, birthday_day=day, birthday_year=year),
        )
    elif fam == "alias":
        if value is not None:
            people.add_alias(session, person, value["value"])
        else:
            norm = key.partition(":")[2]
            for alias in list(person.aliases):
                if alias.value_norm == norm:
                    people.remove_alias(session, person, alias.id)
    elif fam == "note":
        if value is not None:
            people.add_note(session, person, value["value"])
        else:
            for note in list(person.notes):
                if cd.note_key(note.text) == key:
                    people.delete_note(session, note)
    else:
        handle_type, value_norm = _handle_parts(key)
        if value is not None:
            people.add_handle(session, person, handle_type, value["value"], value.get("label"))
        else:
            for handle in list(person.handles):
                if handle.type is handle_type and handle.value_norm == value_norm:
                    people.remove_handle(session, person, handle.id)


def _current(session: Session, person: Person, key: str) -> Any:
    return cd.snapshot_person(person).get(key)


def _set_base(link: ContactLink | None, key: str, value: Any) -> None:
    if link is None:
        return
    base = dict(link.base_json or {})
    if value is None:
        base.pop(key, None)
    else:
        base[key] = value
    link.base_json = base


def _base_before(link: ContactLink | None, key: str) -> dict[str, Any]:
    base = (link.base_json or {}) if link else {}
    return {"present": key in base, "value": base.get(key)}


def _restore_base(link: ContactLink | None, key: str, before: dict[str, Any] | None) -> None:
    before = before or {}
    _set_base(link, key, before.get("value") if before.get("present") else None)


def _link_of(session: Session, person_id: int) -> ContactLink | None:
    return session.scalar(select(ContactLink).where(ContactLink.person_id == person_id))


def _apply_one(session: Session, change: SyncChange, choice: str) -> None:
    kind = change.kind
    if kind is SyncKind.NEW_CONTACT:
        data = change.new_json
        card = cd.card_from_json(data["card"])
        person = create_from_card(session, card, card.country_code or DEFAULT_REGION)
        _new_link(session, person, data, card)
        change.person_id = person.id
        return
    if kind is SyncKind.POSSIBLE_MATCH:
        data = change.new_json
        card = cd.card_from_json(data["card"])
        if choice == "new":
            person = create_from_card(session, card, card.country_code or DEFAULT_REGION)
            _new_link(session, person, data, card)
            change.person_id = person.id
        else:
            person = people.get_person(session, change.person_id or 0)
            _new_link(session, person, data, None)
        return
    person = people.get_person(session, change.person_id or 0)
    link = _link_of(session, person.id)
    if kind is SyncKind.REMOVED_UPSTREAM:
        person.archived = True
        if link is not None:
            session.delete(link)
        session.flush()
        return
    key = change.field
    change.base_old_json = _base_before(link, key)
    _write_orbit(session, person, key, change.new_json)
    _set_base(link, key, change.new_json)


def _new_link(
    session: Session, person: Person, data: Mapping[str, Any], card: VCard | None
) -> None:
    uid = data["uid"]
    if person.vcard_uid is None:
        person.vcard_uid = uid
    link = _link_of(session, person.id)
    if link is None:
        link = ContactLink(person_id=person.id, carddav_uid=person.vcard_uid or uid)
        session.add(link)
    link.href, link.etag = data.get("href"), data.get("etag")
    link.last_synced_at = utcnow()
    link.base_json = cd.snapshot_card(card, person.country or DEFAULT_REGION) if card else {}
    session.flush()


# --- Writing to Apple Contacts ---

_NO_WRITER = "Apple Contacts is not connected."


def is_push(change: SyncChange, choice: str | None) -> bool:
    return change.direction is SyncDirection.PUSH or (
        change.kind is SyncKind.CONFLICT and choice == "orbit"
    )


def _push_values(change: SyncChange) -> tuple[Any, Any]:
    """(the value to write to Apple, the value Apple had)."""
    if change.kind is SyncKind.CONFLICT:  # old = Orbit, new = Apple
        return change.old_json, change.new_json
    return change.new_json, change.old_json  # push: old = Apple, new = Orbit


def _ops(key: str, value: Any, person: Person) -> list[vp.Op]:
    fam = cd.family(key)
    if key == "name":
        return [vp.SetName(value, person.first_name, person.last_name)]
    if key == "birthday":
        return [vp.SetBirthday(*value)] if value else [vp.SetBirthday(None)]
    handle_type, value_norm = _handle_parts(key)
    if fam in ("phone", "email") or fam.startswith("social:"):
        if value is not None:
            return [vp.AddHandle(handle_type, value["value"])]
        return [vp.RemoveHandle(handle_type, value_norm)]
    raise InvalidInputError(f"{cd.key_label(key)} is not written to Apple Contacts.")


def _fail(change: SyncChange, report: ApplyReport | None, message: str) -> None:
    change.state = SyncState.FAILED
    change.error = message
    if report is not None:
        report.failed.append(f"{change.person_name}: {message}")


def _push_person(
    session: Session,
    writer: ContactSource | None,
    run: SyncRun,
    person: Person,
    items: list[tuple[SyncChange, str]],
    report: ApplyReport,
    now: datetime,
) -> None:
    """One PUT for everything chosen for this person; a failure marks all of them failed."""
    link = _link_of(session, person.id)
    try:
        if writer is None:
            raise InvalidInputError(_NO_WRITER)
        if link is None or not link.href:
            raise InvalidInputError("No Apple contact is linked to this person.")
        current = writer.get_card(link.href)
        if link.etag and current.etag and current.etag != link.etag:
            raise InvalidInputError(
                "The contact changed in Apple Contacts since the preview; sync again."
            )
        ops: list[vp.Op] = []
        for change, _choice in items:
            ops += _ops(change.field, _push_values(change)[0], person)
        patched = vp.patch_vcard(current.text, ops, person.country or DEFAULT_REGION)
        new_etag = link.etag
        if patched != current.text:
            new_etag = writer.put_card(link.href, patched, current.etag)
            session.add(
                ContactBackup(
                    person_id=person.id,
                    run_id=run.id,
                    href=link.href,
                    vcard_text=current.text,
                    etag_after=new_etag,
                    created_at=now,
                )
            )
    except (InvalidInputError, CardDavError) as exc:
        for change, _ in items:
            _fail(change, report, str(exc))
        return
    link.etag = new_etag
    for change, choice in items:
        change.base_old_json = _base_before(link, change.field)
        _set_base(link, change.field, _push_values(change)[0])
        change.state = SyncState.APPLIED
        change.choice = choice
        change.applied_at = now
        report.applied += 1


def apply_run(
    session: Session,
    run_id: int,
    choices: Mapping[int, str],
    *,
    seen: Collection[int] | None = None,
    writer: ContactSource | None = None,
    now: datetime | None = None,
) -> ApplyReport:
    """Apply the chosen changes; every other change I was shown (`seen`, default: all) is
    rejected and remembered. Changes I was not shown stay pending and the run stays open."""
    now = now or utcnow()
    run = get_run(session, run_id)
    if run.status is not SyncRunStatus.REVIEW:
        raise InvalidInputError("This sync was already applied.")
    report = ApplyReport()
    pushes: dict[int, list[tuple[SyncChange, str]]] = {}
    for change in run.changes:
        if change.state is not SyncState.PENDING:
            continue
        if seen is not None and change.id not in seen and change.id not in choices:
            continue
        choice = choices.get(change.id)
        if choice not in CHOICES[change.kind]:
            change.state = SyncState.REJECTED
            report.skipped += 1
            continue
        if is_push(change, choice) and change.person_id is not None:
            pushes.setdefault(change.person_id, []).append((change, choice))
            continue
        try:
            with session.begin_nested():
                _apply_one(session, change, choice)
        except (InvalidInputError, HandleConflictError, NotFoundError) as exc:
            change.state = SyncState.FAILED
            change.error = str(exc)
            report.failed.append(f"{change.person_name}: {exc}")
            continue
        change.state = SyncState.APPLIED
        change.choice = choice
        change.applied_at = now
        report.applied += 1
    for person_id, items in pushes.items():
        person = people.get_person(session, person_id)
        _push_person(session, writer, run, person, items, report, now)
    if all(c.state is not SyncState.PENDING for c in run.changes):
        run.status = SyncRunStatus.APPLIED
        run.applied_at = now
    session.flush()
    return report


# --- Presentation (shared by the web review and the API) ---


@dataclass(frozen=True)
class ChangeView:
    change: SyncChange
    label: str  # "Birthday", "Phone", "New contact" ...
    old: str
    new: str
    default: str | None  # the pre-selected choice
    choices: tuple[str, ...]


def _card_summary(data: Mapping[str, Any]) -> str:
    card = cd.card_from_json(data["card"])
    snap = cd.snapshot_card(card, card.country_code or DEFAULT_REGION)
    parts = [cd.describe(k, v) for k, v in snap.items() if cd.family(k) in ("phone", "email")]
    if "birthday" in snap:
        parts.append("🎂 " + cd.describe("birthday", snap["birthday"]))
    return ", ".join(parts) or "no phone, email or birthday"


def change_view(change: SyncChange) -> ChangeView:
    kind = change.kind
    if kind is SyncKind.NEW_CONTACT:
        label, old, new = "New contact", "", _card_summary(change.new_json)
    elif kind is SyncKind.POSSIBLE_MATCH:
        label = "Possible match"
        old, new = f"Orbit: {change.old_json}", _card_summary(change.new_json)
    elif kind is SyncKind.REMOVED_UPSTREAM:
        label, old, new = "Gone from Apple Contacts", "", "Archive in Orbit"
    else:
        label = cd.key_label(change.field)
        old = cd.describe(change.field, change.old_json)
        new = cd.describe(change.field, change.new_json)
    return ChangeView(change, label, old, new, default_choice(change), CHOICES[kind])


# --- Undo and rejection memory ---


def undo_change(
    session: Session, change_id: int, writer: ContactSource | None = None
) -> SyncChange:
    change = session.get(SyncChange, change_id)
    if change is None:
        raise NotFoundError(f"Sync change {change_id} not found.")
    if change.state is not SyncState.APPLIED:
        raise InvalidInputError("Only an applied change can be undone.")
    kind = change.kind
    if kind in (SyncKind.NEW_CONTACT, SyncKind.POSSIBLE_MATCH) and change.choice in (
        "apply",
        "new",
    ):
        person = people.get_person(session, change.person_id or 0)
        link = _link_of(session, person.id)
        if link is not None:
            session.delete(link)
        people.delete_person(session, person)
    elif kind is SyncKind.POSSIBLE_MATCH:
        person = people.get_person(session, change.person_id or 0)
        link = _link_of(session, person.id)
        if link is not None:
            session.delete(link)
        person.vcard_uid = None
    elif kind is SyncKind.REMOVED_UPSTREAM:
        people.get_person(session, change.person_id or 0).archived = False
    elif is_push(change, change.choice):
        _undo_push(session, change, writer)
    else:
        person = people.get_person(session, change.person_id or 0)
        if not cd.same(change.field, _current(session, person, change.field), change.new_json):
            raise InvalidInputError(
                f"{change.person_name}'s {cd.key_label(change.field).lower()} changed since; "
                "not undone."
            )
        _write_orbit(session, person, change.field, change.old_json)
        link = _link_of(session, person.id)
        before = change.base_old_json or {}
        _set_base(link, change.field, before.get("value") if before.get("present") else None)
    change.state = SyncState.UNDONE
    session.flush()
    return change


def _undo_push(session: Session, change: SyncChange, writer: ContactSource | None) -> None:
    """Put Apple's old value back, if the card still holds what we wrote."""
    person = people.get_person(session, change.person_id or 0)
    link = _link_of(session, person.id)
    if writer is None:
        raise InvalidInputError(_NO_WRITER)
    if link is None or not link.href:
        raise InvalidInputError("No Apple contact is linked to this person.")
    written, previous = _push_values(change)
    try:
        current = writer.get_card(link.href)
        region = person.country or DEFAULT_REGION
        now_value = cd.snapshot_card(cd.parse_card(current.text), region).get(change.field)
        if not cd.same(change.field, now_value, written):
            raise InvalidInputError(
                f"{change.person_name}'s {cd.key_label(change.field).lower()} changed in "
                "Apple Contacts since; not undone."
            )
        patched = vp.patch_vcard(current.text, _ops(change.field, previous, person), region)
        if patched != current.text:
            link.etag = writer.put_card(link.href, patched, current.etag)
    except CardDavError as exc:
        raise InvalidInputError(str(exc)) from exc
    _restore_base(link, change.field, change.base_old_json)


def restore_backup(
    session: Session,
    backup_id: int,
    writer: ContactSource | None,
    now: datetime | None = None,
) -> ContactBackup:
    """Put a whole card back as it was before a write, while nobody changed it since."""
    backup = session.get(ContactBackup, backup_id)
    if backup is None:
        raise NotFoundError(f"Backup {backup_id} not found.")
    if backup.restored_at is not None:
        raise InvalidInputError("That card was already restored.")
    if writer is None:
        raise InvalidInputError(_NO_WRITER)
    try:
        current = writer.get_card(backup.href)
        if backup.etag_after and current.etag != backup.etag_after:
            raise InvalidInputError(
                "The contact changed in Apple Contacts since Orbit wrote it; not restored."
            )
        new_etag = writer.put_card(backup.href, backup.vcard_text, current.etag)
    except CardDavError as exc:
        raise InvalidInputError(str(exc)) from exc
    link = _link_of(session, backup.person_id)
    if link is not None:
        link.etag = new_etag
    pushed = session.scalars(
        select(SyncChange).where(
            SyncChange.run_id == backup.run_id,
            SyncChange.person_id == backup.person_id,
            SyncChange.state == SyncState.APPLIED,
        )
    )
    for change in pushed:
        if is_push(change, change.choice):
            _restore_base(link, change.field, change.base_old_json)
            change.state = SyncState.UNDONE
    backup.restored_at = now or utcnow()
    session.flush()
    return backup


def list_backups(session: Session, run_id: int) -> list[ContactBackup]:
    return list(
        session.scalars(
            select(ContactBackup).where(ContactBackup.run_id == run_id).order_by(ContactBackup.id)
        )
    )


BACKUP_DAYS = 90


def prune_backups(
    session: Session, now: datetime | None = None, keep_days: int = BACKUP_DAYS
) -> int:
    cutoff = (now or utcnow()) - timedelta(days=keep_days)
    old = list(session.scalars(select(ContactBackup).where(ContactBackup.created_at < cutoff)))
    for backup in old:
        session.delete(backup)
    session.flush()
    return len(old)


def forget_rejections(session: Session) -> int:
    """Propose the changes I once left unticked again, on the next sync."""
    rejected = list(
        session.scalars(select(SyncChange).where(SyncChange.state == SyncState.REJECTED))
    )
    for change in rejected:
        session.delete(change)
    session.flush()
    return len(rejected)
