from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orbit.domain.people import Person
from orbit.integrations.claude import (
    ClaudeError,
    DraftRequest,
    MessageDraft,
    build_draft_prompt,
)
from orbit.services import drafts, interactions, people, reminders
from orbit.services.dates import UpcomingDate
from orbit.services.errors import InvalidInputError
from tests.test_claude import answer, fake_client

TODAY = date(2026, 9, 24)


class FakeDrafter:
    def __init__(self, outcome: str | Exception = "Všetko najlepšie, Paťo! 🎂") -> None:
        self.outcome = outcome
        self.requests: list[DraftRequest] = []

    def draft_message(self, request: DraftRequest) -> MessageDraft:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return MessageDraft(text=self.outcome)


def pato(db: Session, **fields: Any) -> Person:
    values: dict[str, Any] = {
        "display_name": "Patrik Veselý",
        "first_name": "Paťo",
        "birthday_month": 9,
        "birthday_day": 26,
        "birthday_year": 1996,
    }
    values.update(fields)
    return people.create_person(db, people.PersonCreate(**values))


def test_request_carries_occasion_language_age_notes_and_style(db: Session) -> None:
    who = pato(db)
    people.add_tag(db, who, "Uni")
    for text in ("old note", "got into the PhD programme", "new flat in Petržalka", "running"):
        people.add_note(db, who, text)
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 1), summary="coffee", person_ids=[who.id]
        ),
    )
    request = drafts.build_request(db, who, "birthday", TODAY, style="short, no emojis")
    assert (request.occasion, request.name, request.language) == ("birthday", "Paťo", "sk")
    assert (request.date, request.age, request.celebrated_name) == (date(2026, 9, 26), 30, None)
    assert request.tags == ("Uni",)
    assert len(request.notes) == drafts.NOTES
    assert request.last_interaction == "2026-09-01 (meet): coffee"
    assert request.style == "short, no emojis"


def test_name_day_and_belated_wishes(db: Session) -> None:
    who = pato(db, first_name="Patrik", nameday_country="SK")  # Patrik: 6 Jul in SK
    request = drafts.build_request(db, who, "nameday", date(2026, 7, 8))
    assert request.date == date(2026, 7, 6)  # two days late still counts as this year's
    assert request.celebrated_name == "Patrik"
    later = drafts.build_request(db, who, "nameday", date(2026, 7, 10))
    assert later.date == date(2027, 7, 6)


def test_no_date_no_draft(db: Session) -> None:
    who = pato(db, birthday_month=None, birthday_day=None, birthday_year=None)
    with pytest.raises(InvalidInputError, match="no birthday"):
        drafts.draft(db, who, "birthday", TODAY, FakeDrafter())


def test_claude_draft_and_fallbacks(db: Session) -> None:
    who = pato(db)
    good = drafts.draft(db, who, "birthday", TODAY, FakeDrafter("  Všetko najlepšie!  "))
    assert (good.text, good.by_claude, good.problem) == ("Všetko najlepšie!", True, None)

    failed = drafts.draft(db, who, "birthday", TODAY, FakeDrafter(ClaudeError("API down")))
    assert failed.text == "Všetko najlepšie k narodeninám, Paťo! 🎉"
    assert (failed.by_claude, failed.problem) == (False, "API down")

    empty = drafts.draft(db, who, "birthday", TODAY, FakeDrafter(""))
    assert not empty.by_claude

    none = drafts.draft(db, who, "birthday", TODAY, None)
    assert none.problem == "Claude isn't set up."
    czech = pato(db, display_name="Jana", first_name="Jana", nameday_country="CZ")
    assert drafts.fallback(czech, "nameday") == "Všechno nejlepší k svátku, Jana! 🎉"


def test_draft_prompt_is_masked_and_complete() -> None:
    prompt = build_draft_prompt(
        DraftRequest(
            occasion="birthday",
            name="Paťo",
            language="sk",
            today=TODAY,
            date=date(2026, 9, 26),
            age=30,
            tags=("Uni",),
            notes=("new number +421 905 123 456",),
            style="short",
        )
    )
    assert "Occasion: birthday, on 2026-09-26." in prompt
    assert "Language: Slovak." in prompt
    assert "They turn 30." in prompt
    assert "905 123" not in prompt and "••• 456" in prompt
    assert "Style wishes from the owner: short" in prompt


def test_client_draft_message_uses_structured_output() -> None:
    client, messages = fake_client(answer(MessageDraft(text="Ahoj!")))  # type: ignore[arg-type]
    request = DraftRequest("nameday", "Jana", "cs", TODAY, TODAY, celebrated_name="Jana")
    assert client.draft_message(request).text == "Ahoj!"
    (call,) = messages.calls
    assert call["output_format"] is MessageDraft
    assert "Language: Czech." in call["messages"][0]["content"]


def test_date_push_has_a_draft_button(db: Session) -> None:
    who = pato(db)
    push = reminders.date_push(
        UpcomingDate(date(2026, 9, 26), "birthday", who, "turns 30"), TODAY, "https://o.x", None
    )
    assert [(a.label, a.url) for a in push.actions] == [
        ("Open", f"https://o.x/people/{who.id}"),
        ("✍️ Draft", f"https://o.x/people/{who.id}/draft?occasion=birthday"),
    ]


# --- Web ---


def test_draft_page(web: TestClient, app: FastAPI) -> None:
    with app.state.sessionmaker() as session:
        who = pato(session, birthday_month=None, birthday_day=None, birthday_year=None)
        session.commit()
        who_id = who.id
    app.state.claude = FakeDrafter("Všetko najlepšie, Paťo!")
    missing = web.get(f"/people/{who_id}/draft?occasion=birthday")
    assert "has no birthday set" in missing.text  # back on the person page

    # The page uses the real date, so the birthday has to be today's, not the fixed TODAY.
    today = datetime.now(UTC).astimezone(app.state.settings.timezone).date()
    with app.state.sessionmaker() as session:
        people.update_person(
            session,
            people.get_person(session, who_id),
            people.PersonUpdate(birthday_month=today.month, birthday_day=today.day),
        )
        session.commit()
    detail = web.get(f"/people/{who_id}").text
    assert f"/people/{who_id}/draft?occasion=birthday" in detail
    page = web.get(f"/people/{who_id}/draft?occasion=birthday").text
    assert "Všetko najlepšie, Paťo!" in page
    assert "Drafted by Claude" in page and "Write another" in page

    app.state.claude = None
    plain = web.get(f"/people/{who_id}/draft?occasion=birthday").text
    assert "Všetko najlepšie k narodeninám, Paťo!" in plain
    assert "Claude isn&#39;t set up" in plain
    wedding = web.get(f"/people/{who_id}/draft?occasion=wedding", follow_redirects=False)
    assert wedding.status_code == 303
