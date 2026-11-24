"""Name-based duplicate candidates, "not a duplicate" dismissals, and merging two people.

Handles are unique per (type, value), so two people can never share a phone/email/IG handle;
adding a taken handle surfaces as a HandleConflictError with a merge offer instead.

Every merge stores a `MergeRecord` snapshot of the merged person and of what moved, so it can be
undone: the person comes back (under the old id when it's still free) and takes back whatever of
theirs is still with the kept person. Things changed since are left as they are: a note deleted
after the merge stays deleted, a field edited on the kept person keeps the edit.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.orm import Session, selectinload

from orbit.domain.base import UTCDateTime, utcnow
from orbit.domain.enums import MeetingLinkSource
from orbit.domain.goals import Goal, goal_person
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.normalize import normalize_text
from orbit.domain.people import (
    Alias,
    Handle,
    Interaction,
    MergeRecord,
    Note,
    Person,
    Tag,
    interaction_person,
    not_duplicate,
)
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.people import add_alias, get_person, refresh_search_name

SIMILARITY_THRESHOLD = 0.85
MIN_PREFIX = 3
MAX_BUCKET = 200  # skip comparing inside huge buckets (very common first names)

_FILLABLE = (
    "first_name",
    "last_name",
    "country",
    "city",
    "how_met",
    "photo_url",
)
_BIRTHDAY = ("birthday_month", "birthday_day", "birthday_year")
# Fields of the kept person a merge may fill in from the other one.
_KEEP_FIELDS = (*_FILLABLE, *_BIRTHDAY, "nameday_name", "met_through_id", "vcard_uid")
UNDO_DAYS = 30  # merges listed for undo (older ones can still be undone through the API)


@dataclass(frozen=True)
class DuplicateCandidate:
    a: Person
    b: Person
    reason: str


@dataclass(frozen=True)
class _Names:
    full: str
    first: str
    last: str | None
    aliases: frozenset[str]
    single_token: bool


def _names(person: Person) -> _Names:
    full = normalize_text(person.display_name)
    tokens = full.split() or [""]
    first = normalize_text(person.first_name) if person.first_name else tokens[0]
    last = normalize_text(person.last_name) if person.last_name else None
    if last is None and len(tokens) > 1:
        last = tokens[-1]
    aliases = frozenset(a.value_norm for a in person.aliases)
    single_token = len(full.split()) == 1 and person.last_name is None
    return _Names(full, first, last, aliases, single_token)


def _reason(a: _Names, b: _Names, first_name_counts: dict[str, int]) -> str | None:
    if a.full and a.full == b.full:
        return "Same name"
    if a.last and a.last == b.last:
        if a.first == b.first:
            return "Same name"
        if a.first in b.aliases or b.first in a.aliases:
            return "First name matches an alias"
        shorter, longer = sorted((a.first, b.first), key=len)
        if len(shorter) >= MIN_PREFIX and longer.startswith(shorter):
            return "Similar first name"
        if SequenceMatcher(None, a.full, b.full).ratio() >= SIMILARITY_THRESHOLD:
            return "Similar name"
    for one, other in ((a, b), (b, a)):
        if not one.single_token:
            continue
        if one.full in other.aliases:
            return "Name matches an alias"
        # "pato" (IG-only) vs the only "Pato Novák": plausible; vs one of five Martins: not.
        if one.full == other.first and first_name_counts[one.full] == 2:
            return "Name matches the first name"
    return None


def _pair_key(a_id: int, b_id: int) -> tuple[int, int]:
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


def find_candidates(session: Session) -> list[DuplicateCandidate]:
    people = list(
        session.scalars(
            select(Person)
            .where(Person.archived.is_(False))
            .options(selectinload(Person.aliases))
            .order_by(Person.search_name_norm, Person.id)
        )
    )
    dismissed = {
        (row.person_a_id, row.person_b_id) for row in session.execute(select(not_duplicate))
    }
    names = {p.id: _names(p) for p in people}
    by_id = {p.id: p for p in people}

    first_name_counts: dict[str, int] = defaultdict(int)
    buckets: dict[str, list[int]] = defaultdict(list)
    for person_id, n in names.items():
        first_name_counts[n.first] += 1
        keys = {f"full:{n.full}", f"first:{n.first}"}
        if n.last:
            keys.add(f"last:{n.last}")
        keys.update(f"first:{alias}" for alias in n.aliases)
        for bucket in keys:
            buckets[bucket].append(person_id)

    seen: set[tuple[int, int]] = set()
    candidates: list[DuplicateCandidate] = []
    for ids in buckets.values():
        if len(ids) < 2 or len(ids) > MAX_BUCKET:
            continue
        for a_id, b_id in combinations(ids, 2):
            low, high = _pair_key(a_id, b_id)
            if (low, high) in seen or (low, high) in dismissed:
                continue
            seen.add((low, high))
            reason = _reason(names[low], names[high], first_name_counts)
            if reason:
                candidates.append(DuplicateCandidate(by_id[low], by_id[high], reason))
    candidates.sort(key=lambda c: (c.a.search_name_norm, c.b.search_name_norm))
    return candidates


def dismiss(session: Session, a_id: int, b_id: int) -> None:
    if a_id == b_id:
        raise InvalidInputError("Pick two different people.")
    get_person(session, a_id)
    get_person(session, b_id)
    low, high = _pair_key(a_id, b_id)
    exists = session.execute(
        select(not_duplicate).where(
            not_duplicate.c.person_a_id == low, not_duplicate.c.person_b_id == high
        )
    ).first()
    if exists is None:
        session.execute(not_duplicate.insert().values(person_a_id=low, person_b_id=high))
    session.flush()


def merge(session: Session, keep_id: int, merge_id: int) -> Person:
    """Fold `merge_id` into `keep_id` (with its notes, debts, promises, …) and delete it.
    The caller commits (one transaction)."""
    if keep_id == merge_id:
        raise InvalidInputError("Pick two different people.")
    keep = get_person(session, keep_id)
    other = get_person(session, merge_id)
    snapshot = _snapshot(session, keep, other)

    for name in _FILLABLE:
        if getattr(keep, name) is None and getattr(other, name) is not None:
            setattr(keep, name, getattr(other, name))
    if keep.birthday_month is None and other.birthday_month is not None:
        keep.birthday_month = other.birthday_month
        keep.birthday_day = other.birthday_day
        keep.birthday_year = other.birthday_year
    if (
        keep.nameday_name is None
        and other.nameday_name is not None
        and keep.nameday_country == other.nameday_country  # the name must be in keep's calendar
    ):
        keep.nameday_name = other.nameday_name
    refresh_search_name(keep)
    if keep.met_through_id is None and other.met_through_id not in (None, keep.id):
        keep.met_through_id = other.met_through_id
    if keep.vcard_uid is None and other.vcard_uid is not None:
        uid, other.vcard_uid = other.vcard_uid, None
        session.flush()  # release the unique value before reassigning it
        keep.vcard_uid = uid

    # Re-parent children by setting `.person` (not remove + append): removing first would make
    # them delete-orphans, and any autoflush in between would delete them.
    keep_alias_norms = {a.value_norm for a in keep.aliases}
    for alias in list(other.aliases):
        if alias.value_norm in keep_alias_norms:
            snapshot["dropped_aliases"].append(alias.value)
            session.delete(alias)
        else:
            snapshot["moved_aliases"].append(alias.id)
            alias.person = keep
            keep_alias_norms.add(alias.value_norm)
    for handle in list(other.handles):  # handles are globally unique: no collisions possible
        handle.person = keep
    for note in list(other.notes):
        note.person = keep
    for debt in list(other.debts):
        debt.person = keep
    for promise in list(other.promises):
        promise.person = keep
    session.flush()
    if normalize_text(other.display_name) not in keep_alias_norms | {
        normalize_text(keep.display_name)
    }:
        snapshot["added_alias"] = add_alias(session, keep, other.display_name).id
    for tag in list(other.tags):
        if tag not in keep.tags:
            snapshot["added_tags"].append(tag.id)
            keep.tags.append(tag)
    session.flush()

    shared = select(interaction_person.c.interaction_id).where(
        interaction_person.c.person_id == keep.id
    )
    session.execute(
        delete(interaction_person).where(
            interaction_person.c.person_id == other.id,
            interaction_person.c.interaction_id.in_(shared),
        )
    )
    session.execute(
        update(interaction_person)
        .where(interaction_person.c.person_id == other.id)
        .values(person_id=keep.id)
    )
    # Meetings: keep's own link wins where both were linked.
    kept_meetings = select(MeetingPerson.meeting_id).where(MeetingPerson.person_id == keep.id)
    session.execute(
        delete(MeetingPerson).where(
            MeetingPerson.person_id == other.id, MeetingPerson.meeting_id.in_(kept_meetings)
        )
    )
    session.execute(
        update(MeetingPerson).where(MeetingPerson.person_id == other.id).values(person_id=keep.id)
    )
    # Goals: keep's own link wins where both were linked.
    kept_goals = select(goal_person.c.goal_id).where(goal_person.c.person_id == keep.id)
    session.execute(
        delete(goal_person).where(
            goal_person.c.person_id == other.id, goal_person.c.goal_id.in_(kept_goals)
        )
    )
    session.execute(
        update(goal_person).where(goal_person.c.person_id == other.id).values(person_id=keep.id)
    )
    session.execute(
        update(Person)
        .where(Person.met_through_id == other.id, Person.id != keep.id)
        .values(met_through_id=keep.id)
    )
    if keep.met_through_id == other.id:
        keep.met_through_id = None
    session.execute(
        delete(not_duplicate).where(
            or_(not_duplicate.c.person_a_id == other.id, not_duplicate.c.person_b_id == other.id)
        )
    )
    # Reload the (now empty) collections so deleting `other` cascades over current state.
    session.expire(
        other,
        ["aliases", "handles", "notes", "debts", "promises", "interactions", "tags"],
    )
    session.expire(keep, ["interactions", "debts", "promises"])
    session.delete(other)
    session.add(
        MergeRecord(
            keep_id=keep.id,
            merged_id=merge_id,
            merged_name=other.display_name,
            snapshot_json=snapshot,
        )
    )
    session.flush()
    return keep


# --- Undo ---


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _snapshot(session: Session, keep: Person, other: Person) -> dict[str, Any]:
    """What `merge` is about to change, taken before it changes anything."""
    other_interactions = set(
        session.scalars(
            select(interaction_person.c.interaction_id).where(
                interaction_person.c.person_id == other.id
            )
        )
    )
    keep_interactions = set(
        session.scalars(
            select(interaction_person.c.interaction_id).where(
                interaction_person.c.person_id == keep.id
            )
        )
    )
    keep_meetings = set(
        session.scalars(select(MeetingPerson.meeting_id).where(MeetingPerson.person_id == keep.id))
    )
    other_meetings = session.scalars(
        select(MeetingPerson).where(MeetingPerson.person_id == other.id)
    ).all()
    keep_goals = set(
        session.scalars(select(goal_person.c.goal_id).where(goal_person.c.person_id == keep.id))
    )
    other_goals = set(
        session.scalars(select(goal_person.c.goal_id).where(goal_person.c.person_id == other.id))
    )
    pairs = session.execute(
        select(not_duplicate.c.person_a_id, not_duplicate.c.person_b_id).where(
            or_(not_duplicate.c.person_a_id == other.id, not_duplicate.c.person_b_id == other.id)
        )
    ).all()
    return {
        "person": {c.key: _jsonable(getattr(other, c.key)) for c in Person.__table__.columns},
        "keep_before": {name: getattr(keep, name) for name in _KEEP_FIELDS},
        "moved_aliases": [],
        "dropped_aliases": [],
        "added_alias": None,
        "handles": [h.id for h in other.handles],
        "notes": [n.id for n in other.notes],
        "debts": [d.id for d in other.debts],
        "promises": [p.id for p in other.promises],
        "tags": [t.id for t in other.tags],
        "added_tags": [],
        "moved_interactions": sorted(other_interactions - keep_interactions),
        "shared_interactions": sorted(other_interactions & keep_interactions),
        "meetings": [
            [m.meeting_id, m.link_source.value, m.meeting_id in keep_meetings]
            for m in other_meetings
        ],
        "goals": [[g, g in keep_goals] for g in sorted(other_goals)],
        "introduced": list(
            session.scalars(
                select(Person.id).where(Person.met_through_id == other.id, Person.id != keep.id)
            )
        ),
        "keep_introduced_by_other": keep.met_through_id == other.id,
        "not_duplicate": [[a, b] for a, b in pairs],
    }


def list_merges(session: Session, now: datetime | None = None) -> list[MergeRecord]:
    """Merges of the last UNDO_DAYS that haven't been undone, newest first."""
    since = (now or utcnow()) - timedelta(days=UNDO_DAYS)
    return list(
        session.scalars(
            select(MergeRecord)
            .where(MergeRecord.undone_at.is_(None), MergeRecord.created_at >= since)
            .options(selectinload(MergeRecord.keep))
            .order_by(MergeRecord.created_at.desc(), MergeRecord.id.desc())
        )
    )


def _restore_person(session: Session, record: MergeRecord, keep: Person) -> Person:
    data = dict(record.snapshot_json["person"])
    uid = data.get("vcard_uid")
    if uid is not None and keep.vcard_uid == uid:
        keep.vcard_uid = record.snapshot_json["keep_before"]["vcard_uid"]
        session.flush()  # release the unique value before giving it back
    elif uid is not None and session.scalar(select(Person.id).where(Person.vcard_uid == uid)):
        data["vcard_uid"] = None  # someone else has it now
    if data.get("met_through_id") is not None and not session.get(Person, data["met_through_id"]):
        data["met_through_id"] = None
    if session.get(Person, record.merged_id) is not None:
        data.pop("id")  # the old id was taken since: a new one
    fields: dict[str, Any] = {}
    for column in Person.__table__.columns:
        if column.key not in data:
            continue
        value = data[column.key]
        if isinstance(column.type, UTCDateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        fields[column.key] = value
    person = Person(**fields)
    session.add(person)
    session.flush()
    return person


def _revert_keep_fields(keep: Person, before: dict[str, Any], merged: dict[str, Any]) -> None:
    """Undo what the merge filled in, unless it was edited since."""

    def filled(names: tuple[str, ...]) -> bool:
        now = [getattr(keep, n) for n in names]
        return now == [merged.get(n) for n in names] and now != [before[n] for n in names]

    for name in (*_FILLABLE, "nameday_name"):
        if filled((name,)):
            setattr(keep, name, before[name])
    if filled(_BIRTHDAY):
        for name in _BIRTHDAY:
            setattr(keep, name, before[name])
    if filled(("met_through_id",)):
        keep.met_through_id = before["met_through_id"]


def _take_back[T: (Alias, Handle, Note, Promise)](
    session: Session, model: type[T], ids: list[int], record: MergeRecord, restored: Person
) -> None:
    query = select(model).where(model.id.in_(ids), model.person_id == record.keep_id)
    created_at = getattr(model, "created_at", None)  # notes and promises have one
    if created_at is not None:
        # SQLite can reuse the id of a row deleted since for a new one: that one stays.
        query = query.where(created_at <= record.created_at)
    for row in session.scalars(query):
        row.person = restored


def undo_merge(session: Session, record_id: int, now: datetime | None = None) -> Person:
    """Bring the merged person back with what's still theirs. The caller commits."""
    record = session.get(MergeRecord, record_id)
    if record is None:
        raise NotFoundError(f"Merge {record_id} not found.")
    if record.undone_at is not None:
        raise InvalidInputError("This merge was already undone.")
    snap = record.snapshot_json
    keep = record.keep
    restored = _restore_person(session, record, keep)
    _revert_keep_fields(keep, snap["keep_before"], snap["person"])

    _take_back(session, Alias, snap["moved_aliases"], record, restored)
    for value in snap["dropped_aliases"]:
        session.add(Alias(person=restored, value=value, value_norm=normalize_text(value)))
    if snap["added_alias"] is not None:
        added = session.get(Alias, snap["added_alias"])
        if added is not None and added.person_id == keep.id:
            session.delete(added)
    _take_back(session, Handle, snap["handles"], record, restored)
    _take_back(session, Note, snap["notes"], record, restored)
    _take_back(session, Promise, snap["promises"], record, restored)
    # Debts, plus remainders split off them since (a partial settlement after the merge).
    debt_ids = set(
        session.scalars(
            select(Debt.id).where(Debt.id.in_(snap["debts"]), Debt.created_at <= record.created_at)
        )
    )
    while True:
        more = set(
            session.scalars(
                select(Debt.id).where(
                    Debt.person_id == keep.id,
                    Debt.parent_id.in_(debt_ids),
                    Debt.id.not_in(debt_ids),
                )
            )
        )
        if not more:
            break
        debt_ids |= more
    for debt in session.scalars(
        select(Debt).where(Debt.id.in_(debt_ids), Debt.person_id == keep.id)
    ):
        debt.person = restored

    restored.tags = list(session.scalars(select(Tag).where(Tag.id.in_(snap["tags"]))))
    keep.tags = [t for t in keep.tags if t.id not in set(snap["added_tags"])]
    session.flush()

    session.execute(
        update(interaction_person)
        .where(
            interaction_person.c.person_id == keep.id,
            interaction_person.c.interaction_id.in_(snap["moved_interactions"]),
        )
        .values(person_id=restored.id)
    )
    shared = session.scalars(
        select(Interaction.id).where(Interaction.id.in_(snap["shared_interactions"]))
    ).all()
    if shared:
        session.execute(
            insert(interaction_person),
            [{"interaction_id": i, "person_id": restored.id} for i in shared],
        )
    existing_meetings = set(
        session.scalars(select(Meeting.id).where(Meeting.id.in_([m[0] for m in snap["meetings"]])))
    )
    for meeting_id, source, was_shared in snap["meetings"]:
        if meeting_id not in existing_meetings:
            continue
        if not was_shared:
            session.execute(
                delete(MeetingPerson).where(
                    MeetingPerson.meeting_id == meeting_id, MeetingPerson.person_id == keep.id
                )
            )
        session.add(
            MeetingPerson(
                meeting_id=meeting_id,
                person_id=restored.id,
                link_source=MeetingLinkSource(source),
            )
        )
    existing_goals = set(
        session.scalars(select(Goal.id).where(Goal.id.in_([g[0] for g in snap.get("goals", [])])))
    )
    for goal_id, was_shared in snap.get("goals", []):
        if goal_id not in existing_goals:
            continue
        if not was_shared:
            session.execute(
                delete(goal_person).where(
                    goal_person.c.goal_id == goal_id, goal_person.c.person_id == keep.id
                )
            )
        session.execute(insert(goal_person).values(goal_id=goal_id, person_id=restored.id))
    if snap["introduced"]:
        session.execute(
            update(Person)
            .where(Person.id.in_(snap["introduced"]), Person.met_through_id == keep.id)
            .values(met_through_id=restored.id)
        )
    if snap["keep_introduced_by_other"] and keep.met_through_id is None:
        keep.met_through_id = restored.id
    for a, b in snap["not_duplicate"]:
        a, b = (restored.id if x == record.merged_id else x for x in (a, b))
        if session.get(Person, a) is not None and session.get(Person, b) is not None:
            session.execute(
                insert(not_duplicate).values(person_a_id=min(a, b), person_b_id=max(a, b))
            )

    refresh_search_name(keep)
    refresh_search_name(restored)
    record.undone_at = now or utcnow()
    session.flush()
    session.expire(keep, ["aliases", "handles", "notes", "debts", "promises", "interactions"])
    session.expire(restored)
    return restored
