"""Birthday / name-day message drafts: Claude writes one in the person's language, from their name,
the occasion, their circles and my latest notes about them. Without Claude (or when it fails) a
plain wish is offered instead. A draft is never stored or sent; I copy it and send it myself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from orbit.domain.people import Person
from orbit.integrations.claude import ClaudeError, DraftRequest, Language, MessageDrafter
from orbit.services import dates, interactions
from orbit.services.errors import InvalidInputError
from orbit.services.messages import address_name, language

Occasion = Literal["birthday", "nameday"]

LOOK_BACK_DAYS = 3  # a belated wish still counts as for the last occurrence
NOTES = 3
NOTE_CHARS = 300

_FALLBACK: dict[Language, dict[Occasion, str]] = {
    "sk": {
        "birthday": "Všetko najlepšie k narodeninám, {name}! 🎉",
        "nameday": "Všetko najlepšie k meninám, {name}! 🎉",
    },
    "cs": {
        "birthday": "Všechno nejlepší k narozeninám, {name}! 🎉",
        "nameday": "Všechno nejlepší k svátku, {name}! 🎉",
    },
    "en": {
        "birthday": "Happy birthday, {name}! 🎉",
        "nameday": "Happy name day, {name}! 🎉",
    },
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def occasion_date(person: Person, occasion: Occasion, today: date) -> dates.UpcomingDate:
    """The occasion's nearest date: a few days ago (belated) or the next one within a year."""
    start = today - timedelta(days=LOOK_BACK_DAYS)
    for upcoming in dates.person_upcoming(person, start, 366 + LOOK_BACK_DAYS):
        if upcoming.kind == occasion:
            return upcoming
    what = "birthday" if occasion == "birthday" else "name day"
    raise InvalidInputError(f"{person.display_name} has no {what} set.")


def build_request(
    session: Session, person: Person, occasion: Occasion, today: date, style: str | None = None
) -> DraftRequest:
    """The `DraftRequest` for Claude: the person, the occasion and its date, age (birthdays with a
    year) and recent notes."""
    when = occasion_date(person, occasion, today)
    age = None
    if occasion == "birthday" and person.birthday_year is not None:
        age = when.date.year - person.birthday_year
    last = interactions.last_interaction(session, person.id)
    last_text = None
    if last is not None:
        last_text = f"{last.occurred_on.isoformat()} ({last.kind.value})"
        if last.summary:
            last_text += f": {_clip(last.summary, NOTE_CHARS)}"
    return DraftRequest(
        occasion=occasion,
        name=address_name(person),
        language=language(person),
        today=today,
        date=when.date,
        age=age,
        celebrated_name=(when.detail or None) if occasion == "nameday" else None,
        tags=tuple(t.name for t in person.tags),
        notes=tuple(_clip(n.text, NOTE_CHARS) for n in person.notes[:NOTES]),
        last_interaction=last_text,
        style=style,
    )


def fallback(person: Person, occasion: Occasion) -> str:
    """The plain template message used when Claude is not available."""
    return _FALLBACK[language(person)][occasion].format(name=address_name(person))


@dataclass(frozen=True)
class Draft:
    text: str
    by_claude: bool
    problem: str | None = None  # why Claude's draft isn't there, if it isn't


def draft(
    session: Session,
    person: Person,
    occasion: Occasion,
    today: date,
    drafter: MessageDrafter | None,
    style: str | None = None,
) -> Draft:
    """A message draft for an occasion: Claude's when a drafter is given, otherwise the template
    fallback."""
    request = build_request(session, person, occasion, today, style)
    if drafter is None:
        return Draft(fallback(person, occasion), by_claude=False, problem="Claude isn't set up.")
    try:
        text = drafter.draft_message(request).text.strip()
    except ClaudeError as exc:
        return Draft(fallback(person, occasion), by_claude=False, problem=str(exc))
    if not text:
        return Draft(fallback(person, occasion), by_claude=False, problem="Claude wrote nothing.")
    return Draft(text, by_claude=True)
