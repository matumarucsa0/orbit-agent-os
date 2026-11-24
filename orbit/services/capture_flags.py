"""Flags Orbit computes for the items of a capture card (SPEC §2.32).

Claude's own `confidence` is poorly calibrated, so the card shows facts Orbit can check itself:
a name that fits two people, an amount that is missing, a date read from a relative word, a new
person. They are worked out when the card is drawn, so fixing the item makes its flag go away.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.normalize import normalize_text
from orbit.domain.people import Person
from orbit.services import matching
from orbit.services.capture import (
    AppointmentDraft,
    CapturePayload,
    DebtDraft,
    InteractionDraft,
    Item,
    PromiseDraft,
    _refs,
)

# Words (normalized, diacritics removed) that make a date relative: Slovak, Czech and English.
RELATIVE_STEMS = (
    "dnes",
    "zajtra",
    "zitra",
    "pozajtra",
    "pozitri",
    "vcera",
    "predvcerom",
    "tyzden",
    "tyden",
    "mesiac",
    "mesic",
    "pondelok",
    "pondeli",
    "utorok",
    "uteri",
    "stred",
    "stvrt",
    "ctvrt",
    "piatok",
    "patek",
    "pati",
    "sobot",
    "nedel",
    "vikend",
    "victor",
    "buduc",
    "pristi",
    "today",
    "tomorrow",
    "yesterday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "weekend",
    "next",
)
_RELATIVE = re.compile(r"\b(?:" + "|".join(RELATIVE_STEMS) + r")\w*")
MIN_TOKEN = 3


def relative_word(text: str) -> str | None:
    """The first relative-date word in `text` ("zajtra", "v piatok"), else None."""
    found = _RELATIVE.search(normalize_text(text))
    return found.group(0) if found else None


def _hits(word_list: list[str], person: Person) -> set[str]:
    """The words of the dictation that point at `person` (an exact or inflected name token)."""
    return {
        w
        for w in word_list
        for token in matching.name_tokens(person)
        if len(token) >= MIN_TOKEN and matching.word_score(w, token) >= 2
    }


def ambiguous_people(
    session: Session, payload: CapturePayload, word_list: list[str]
) -> dict[int, list[str]]:
    """For each person an item points at: the other people the same words fit (so "Jana" that
    could be two people shows both). Empty when the name is unambiguous."""
    wanted = {int(r) for item in payload.items() for r in _refs(item) if r.isdigit()}
    if not wanted:
        return {}
    everyone = list(
        session.scalars(
            select(Person).where(Person.archived.is_(False)).options(selectinload(Person.aliases))
        )
    )
    by_id = {p.id: p for p in everyone}
    found: dict[int, list[str]] = {}
    for pid in wanted & by_id.keys():
        mine = _hits(word_list, by_id[pid])
        if not mine:
            continue
        rivals = [q.display_name for q in everyone if q.id != pid and mine & _hits(word_list, q)]
        if rivals:
            found[pid] = rivals
    return found


def _date_word(item: Item, text: str) -> str | None:
    has_date = (
        (isinstance(item, PromiseDraft) and item.due is not None)
        or isinstance(item, AppointmentDraft)
        or (isinstance(item, InteractionDraft) and item.occurred_on is not None)
    )
    return relative_word(text) if has_date else None


def item_flags(
    item: Item,
    payload: CapturePayload,
    names: dict[int, str],
    ambiguity: dict[int, list[str]],
) -> list[str]:
    flags: list[str] = []
    for ref in _refs(item):
        if ref.isdigit() and int(ref) in ambiguity:
            who = names.get(int(ref), f"#{ref}")
            others = ", ".join(ambiguity[int(ref)][:3])
            flags.append(f"{who} could also be {others}")
        elif ref.startswith("new:") and int(ref[4:]) < len(payload.new_people):
            draft = payload.new_people[int(ref[4:])]
            if draft.action == "create":
                flags.append(f"New person: {draft.name}")
    if isinstance(item, DebtDraft) and item.amount_missing:
        flags.append("Amount missing")
    word = _date_word(item, payload.text)
    if word is not None:
        flags.append(f"Date read from “{word}”: check it")
    return flags


def flags_for(session: Session, payload: CapturePayload) -> list[list[str]]:
    """One list of flags per included item, in the order of `payload.included()`."""
    from orbit.services.capture import _names

    names = _names(session, payload.referenced_ids())
    ambiguity = ambiguous_people(session, payload, matching.words(payload.text))
    return [item_flags(item, payload, names, ambiguity) for item in payload.included()]
