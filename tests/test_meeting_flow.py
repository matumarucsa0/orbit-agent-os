"""Phase 8 end to end: calendar sync -> brief page and push -> dictation afterwards lands in the
Inbox linked to the meeting; the Meetings pages and API; split pre-selection."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from orbit.db import make_engine, make_sessionmaker
from orbit.domain import Base
from orbit.domain.bank import BankImport, Transaction
from orbit.domain.enums import Bank, Classification, TransactionDirection, TransactionKind
from orbit.domain.inbox import Notification
from orbit.domain.meetings import Meeting
from orbit.integrations.caldav_client import CalDavError, CalendarEvent
from orbit.jobs.scheduler import meeting_pushes, sync_calendar
from orbit.services import split
from orbit.services.classify import classify
from tests.conftest import AUTH
from tests.test_classify import CONTEXT, FakeClassifier, tx
from tests.test_meetings import FakeLinker, FakeSource, FakeSummarizer
from tests.test_notifications import FakeSender
from tests.test_web_ledger import post, text
from tests.test_web_people import add


def at(hours: float, key: str = "kafe", title: str = "kafe s Paťom") -> CalendarEvent:
    """An event starting `hours` from now (whole minutes), an hour long."""
    start = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=hours)
    return CalendarEvent(key, "Personal", title, None, start, start + timedelta(hours=1))


def pato(api: TestClient) -> int:
    created = api.post(
        "/api/v1/people", json={"display_name": "Patrik Veselý", "first_name": "Patrik"}
    )
    person_id = int(created.json()["id"])
    api.post(f"/api/v1/people/{person_id}/aliases", json={"value": "Paťo"})
    return person_id


def synced(api: TestClient, app: FastAPI, *events: CalendarEvent) -> dict[str, object]:
    app.state.calendar = FakeSource(*events)
    response = api.post("/api/v1/meetings/sync")
    assert response.status_code == 200, response.text
    return dict(response.json())


def meeting_id(app: FastAPI, key: str = "kafe") -> int:
    with app.state.sessionmaker() as session:
        return int(session.scalars(select(Meeting.id).where(Meeting.calendar_uid == key)).one())


# --- API ---


def test_meetings_api(api: TestClient, app: FastAPI) -> None:
    person_id = pato(api)
    body = synced(api, app, at(1), at(3, "gym", "Gym"))
    assert body == {
        "new": 2,
        "updated": 0,
        "cancelled": 0,
        "calendars": ["Personal"],
        "missing": [],
        "summary": "2 new, 0 updated, 0 cancelled (Personal)",
    }
    listed = api.get("/api/v1/meetings").json()
    assert [(m["title"], m["people"]) for m in listed] == [
        (
            "kafe s Paťom",
            [{"id": person_id, "display_name": "Patrik Veselý", "link_source": "title"}],
        ),
        ("Gym", []),
    ]
    gym = listed[1]["id"]
    linked = api.post(f"/api/v1/meetings/{gym}/people", json={"person_id": person_id})
    assert linked.json()["people"][0]["link_source"] == "manual"
    assert [m["title"] for m in api.get(f"/api/v1/meetings?person_id={person_id}").json()] == [
        "kafe s Paťom",
        "Gym",
    ]
    unlinked = api.delete(f"/api/v1/meetings/{gym}/people/{person_id}")
    assert unlinked.json()["people"] == []
    assert api.delete(f"/api/v1/meetings/{gym}/people/{person_id}").status_code == 422
    assert api.get("/api/v1/meetings/999").status_code == 404
    assert api.post("/api/v1/meetings/999/people", json={"person_id": person_id}).status_code == 404
    later = api.get(
        "/api/v1/meetings", params={"start": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    )
    assert [m["title"] for m in later.json()] == ["Gym"]


def test_sync_api_without_or_with_a_broken_calendar(api: TestClient, app: FastAPI) -> None:
    assert api.post("/api/v1/meetings/sync").status_code == 503
    source = FakeSource()
    source.error = CalDavError("The calendar server rejected the username.")
    app.state.calendar = source
    response = api.post("/api/v1/meetings/sync")
    assert (response.status_code, response.json()["detail"]) == (
        502,
        "The calendar server rejected the username.",
    )


def test_sync_api_uses_claude_for_ambiguous_names(api: TestClient, app: FastAPI) -> None:
    pato(api)
    other = api.post("/api/v1/people", json={"display_name": "Patrik Horváth"}).json()["id"]
    api.post(f"/api/v1/people/{other}/aliases", json={"value": "Paťo"})
    app.state.claude = FakeLinker([other])
    synced(api, app, at(1))
    (meeting,) = api.get("/api/v1/meetings").json()
    assert [(p["display_name"], p["link_source"]) for p in meeting["people"]] == [
        ("Patrik Horváth", "claude")
    ]


def test_meetings_api_requires_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/meetings").status_code == 401


# --- Phase 8 "done when" ---


def test_kafe_s_patom_brief_then_dictation_lands_in_the_inbox(
    api: TestClient, app: FastAPI
) -> None:
    person_id = pato(api)
    api.post(
        "/api/v1/debts",
        json={
            "person_id": person_id,
            "direction": "they_owe_me",
            "amount_minor": 1200,
            "description": "pizza",
        },
    )
    synced(api, app, at(0.4))  # starts in 24 minutes
    sender = FakeSender()
    settings = app.state.settings
    assert meeting_pushes(app.state.sessionmaker, sender, FakeSummarizer(), settings) == 1
    assert meeting_pushes(app.state.sessionmaker, sender, FakeSummarizer(), settings) == 0
    (brief,) = sender.sent
    mid = meeting_id(app)
    assert brief.title.endswith(" kafe s Paťom")
    assert "💶 owes you 12.00 €" in brief.body
    assert brief.click == f"http://testserver/meetings/{mid}"

    # The post-meeting push opens the Shortcut with the meeting id; it sends the dictation.
    response = api.post(
        "/api/v1/capture", json={"text": "Paťo má novú prácu", "meeting_id": str(mid)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == 2
    assert "Meet with Patrik Veselý: kafe s Paťom" in body["summary"]
    (proposal_id,) = body["proposal_ids"]
    confirmed = api.post(f"/api/v1/inbox/{proposal_id}/confirm")
    assert confirmed.json()["message"] == "Saved: 1 note, 1 interaction."
    (interaction,) = api.get(f"/api/v1/interactions?person_id={person_id}").json()
    assert interaction["kind"] == "meet" and interaction["summary"] == "kafe s Paťom"


def test_scheduler_jobs(make_settings) -> None:  # type: ignore[no-untyped-def]
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    source = FakeSource(at(0.2, title="Gym"))
    report = sync_calendar(sessions, source, None)
    assert report is not None and report.new == 1
    source.error = CalDavError("down")
    assert sync_calendar(sessions, source, None) is None
    with sessions() as session:
        assert len(session.scalars(select(Meeting)).all()) == 1  # committed
    sender = FakeSender()
    assert meeting_pushes(sessions, sender, None, make_settings()) == 0  # nobody linked
    engine.dispose()


# --- Web ---


def test_meetings_pages(web: TestClient, app: FastAPI) -> None:
    person_id = add(web, "Patrik Veselý")
    post(web, f"/people/{person_id}/aliases", value="Paťo")
    jana = add(web, "Jana Kováčová")
    app.state.calendar = FakeSource(at(1), at(2, "gym", "Gym"), at(-30, "old", "Obed s Paťom"))
    page = text(post(web, "/meetings/sync"))
    assert "Synced: 3 new, 0 updated, 0 cancelled (Personal)." in page
    assert "kafe s Paťom with Patrik Veselý" in page
    assert "Gym not linked to anyone" in page
    assert "Earlier" in page and "Obed s Paťom" in page

    mid = meeting_id(app)
    detail = text(web.get(f"/meetings/{mid}").text)
    assert "Patrik Veselý from the title" in detail
    assert "Owes you" not in detail and "Nothing logged yet." in detail
    assert "Note something" in detail

    found = web.get(f"/meetings/{mid}/people-search", params={"q": "jana"}).text
    assert "Jana Kováčová" in found and "Patrik" not in found
    detail = text(post(web, f"/meetings/{mid}/people", person_id=str(jana)))
    assert "Jana Kováčová linked by you" in detail
    detail = text(post(web, f"/meetings/{mid}/people/{person_id}/delete"))
    assert "Patrik Veselý" not in detail.split("With", 1)[1].split("Jana", 1)[0]

    home = text(web.get("/").text)
    if datetime.now(UTC).astimezone(app.state.settings.timezone).hour < 22:
        assert "Meetings today" in home


def test_meetings_page_without_a_calendar(web: TestClient) -> None:
    page = text(web.get("/meetings").text)
    assert "The calendar isn't connected." in page
    assert "Sync now" not in page
    assert "The calendar isn't configured." in text(post(web, "/meetings/sync"))


def test_sync_error_is_shown(web: TestClient, app: FastAPI) -> None:
    source = FakeSource()
    source.error = CalDavError("Could not read the calendar: ConnectError")
    app.state.calendar = source
    assert "Could not read the calendar: ConnectError" in text(post(web, "/meetings/sync"))


def test_dictating_from_the_meeting_page(web: TestClient, app: FastAPI) -> None:
    person_id = add(web, "Patrik Veselý")
    post(web, f"/people/{person_id}/aliases", value="Paťo")
    app.state.calendar = FakeSource(at(-2))
    post(web, "/meetings/sync")
    mid = meeting_id(app)
    page = text(post(web, "/inbox/capture", text="bolo fajn", meeting_id=str(mid)))
    assert "2 items waiting in Inbox" in page
    assert "How was it?" in page  # back on the meeting page
    inbox_page = text(web.get("/inbox").text)
    assert "Meet with Patrik Veselý: kafe s Paťom" in inbox_page
    ids = [
        int(i) for i in dict.fromkeys(re.findall(r'id="proposal-(\d+)"', web.get("/inbox").text))
    ]
    post(web, f"/inbox/{ids[0]}/confirm")
    detail = text(web.get(f"/meetings/{mid}").text)
    assert "From this meeting" in detail and "bolo fajn" in detail


def test_split_preselects_people_from_the_meeting(web: TestClient, app: FastAPI) -> None:
    person_id = add(web, "Patrik Veselý")
    post(web, f"/people/{person_id}/aliases", value="Paťo")
    app.state.calendar = FakeSource(at(-3, title="večera s Paťom"))
    post(web, "/meetings/sync")
    with app.state.sessionmaker() as session:
        paid = Transaction(
            bank_import=BankImport(bank=Bank.TATRA),
            bank=Bank.TATRA,
            booking_date=datetime.now(UTC).date(),
            occurred_at=datetime.now(UTC) - timedelta(hours=1, minutes=30),  # 30 min after it ended
            amount_minor=4800,
            currency="EUR",
            direction=TransactionDirection.OUT,
            kind=TransactionKind.CARD,
            counterparty="Lokál",
            counterparty_norm="lokal",
            classification=Classification.LIKELY_SHARED,
            hash="lokal",
            raw_json={},
        )
        session.add(paid)
        session.flush()
        proposal_id = split.propose_split(session, paid).id
        session.commit()
    page = web.get(f"/inbox/{proposal_id}").text
    assert f'name="person" value="{person_id}" checked' in page
    assert "From your calendar:" in text(page) and "večera s Paťom" in text(page)
    # Nothing is saved until I confirm: the stored split is still empty.
    assert api_shares(web, proposal_id) == []


def api_shares(web: TestClient, proposal_id: int) -> list[object]:
    response = web.get(f"/api/v1/inbox/{proposal_id}", headers=AUTH)
    return list(response.json()["payload"]["shares"])


# --- Bank classification sees the meeting ---


def test_claude_is_told_about_overlapping_meetings() -> None:
    classifier = FakeClassifier([(0, Classification.LIKELY_SHARED)])
    row = tx("U Fleků", 900)
    classify([row], classifier, None, CONTEXT, lambda t: "večera s Paťom (Patrik Veselý)")
    (rows,) = classifier.calls
    assert rows[0].meeting == "večera s Paťom (Patrik Veselý)"


def test_pushes_are_recorded(api: TestClient, app: FastAPI) -> None:
    pato(api)
    synced(api, app, at(-1.4))  # ended 24 minutes ago
    sender = FakeSender()
    assert meeting_pushes(app.state.sessionmaker, sender, None, app.state.settings) == 1
    with app.state.sessionmaker() as session:
        (record,) = session.scalars(select(Notification)).all()
    assert record.kind == "meeting_capture" and record.delivered
    (push,) = sender.sent
    assert push.actions[0].url.startswith(
        "shortcuts://run-shortcut?name=Orbit%20Capture&input=text&text="
    )
