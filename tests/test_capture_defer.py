"""A capture answers at once and Claude reads it afterwards (SPEC §2.33)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from orbit.domain.enums import ProposalKind
from orbit.domain.inbox import Notification, Proposal
from orbit.integrations.claude import (
    CaptureExtraction,
    CaptureRequest,
    ClaudeError,
    ExtractedNote,
    UnknownPerson,
)
from orbit.services import capture, capture_defer, claude_jobs, notifications
from orbit.services.capture import CapturePayload
from tests.test_capture import TODAY, FakeExtractor, extraction
from tests.test_claude_jobs import Clock, alive, late_ctx, make_client, note_answer, serve, sessions
from tests.test_notifications import FakeSender
from tests.test_web_ledger import post, text

NO_QUIET = notifications.QuietHours(time(3, 0), time(3, 0), capture.DEFAULT_TZ)
__all__ = ["sessions"]  # the fixture comes from the job tests


def deferred(
    db: Session, answer: CaptureExtraction | Exception, said: str = "Janko je nový kolega"
) -> tuple[FakeExtractor, capture.CaptureResult]:
    fake = FakeExtractor(answer)
    result = capture.capture(
        db, fake, said, today=TODAY, default_currency="EUR", defer=True, now=datetime.now(UTC)
    )
    db.commit()
    return fake, result


def janko() -> CaptureExtraction:
    """A note about Janko, and Marta who is mentioned without any item: two cards."""
    return extraction(
        notes=[ExtractedNote(person_ref="new:0", text="nový kolega")],
        unknown_people=[
            UnknownPerson(name="Janko", context="work"),
            UnknownPerson(name="Marta", context="friend"),
        ],
    )


# --- The first step: save at once ---


def test_a_deferred_capture_asks_nobody_and_saves_a_processing_draft(db: Session) -> None:
    fake, result = deferred(db, janko())
    assert fake.requests == []
    assert result.summary.startswith("Saved; Claude is reading it and you get a push")
    assert isinstance(result.pending, capture.PendingCapture)
    assert result.pending.request.text == "Janko je nový kolega"
    (proposal,) = result.proposals
    payload = CapturePayload.model_validate(proposal.payload_json)
    assert payload.processing and payload.notes[0].text == "Janko je nový kolega"


def test_without_claude_there_is_nothing_pending(db: Session) -> None:
    result = capture.capture(
        db, None, "niečo", today=TODAY, default_currency="EUR", defer=True, now=datetime.now(UTC)
    )
    assert result.pending is None
    payload = CapturePayload.model_validate(result.proposals[0].payload_json)
    assert not payload.processing


def test_not_deferring_still_asks_at_once(db: Session) -> None:
    fake = FakeExtractor(janko())
    result = capture.capture(db, fake, "Janko", today=TODAY, default_currency="EUR")
    assert result.pending is None and len(fake.requests) == 1


# --- The second step: the answer replaces the draft ---


def test_the_background_answer_replaces_the_draft_and_pushes(
    sessions: sessionmaker[Session],
) -> None:
    sender = FakeSender()
    with sessions() as db:
        fake, result = deferred(db, janko())
        pending = result.pending
    assert pending is not None
    capture_defer.answer_in_background(
        sessions, fake, pending, sender=sender, base_url="https://orbit.test", quiet=NO_QUIET
    )
    with sessions() as db:
        kinds = [p.kind for p in db.query(Proposal).order_by(Proposal.id)]
        assert kinds == [ProposalKind.CAPTURE, ProposalKind.NEW_PERSON]
        first = db.query(Proposal).order_by(Proposal.id).first()
        assert first is not None
        payload = CapturePayload.model_validate(first.payload_json)
        assert not payload.processing and payload.notes[0].text == "nový kolega"
        new = db.query(Proposal).filter(Proposal.kind == ProposalKind.NEW_PERSON).one()
        assert new.payload_json["name"] == "Marta"
        assert new.payload_json["source_text"] == "Janko je nový kolega"
        assert new.payload_json["group"] == payload.group
        assert db.query(Notification).one().title == "📥 Capture ready"
    assert [m.title for m in sender.sent] == ["📥 Capture ready"]


def test_a_failed_answer_keeps_the_note_and_stops_saying_processing(
    sessions: sessionmaker[Session],
) -> None:
    sender = FakeSender()
    with sessions() as db:
        fake, result = deferred(db, ClaudeError("API down"))
        pending = result.pending
    assert pending is not None
    capture_defer.answer_in_background(
        sessions, fake, pending, sender=sender, base_url="x", quiet=NO_QUIET
    )
    with sessions() as db:
        proposal = db.query(Proposal).one()
        payload = CapturePayload.model_validate(proposal.payload_json)
        assert not payload.processing and payload.notes[0].text == "Janko je nový kolega"
        assert db.query(Notification).count() == 0
    assert sender.sent == []


def test_an_unexpected_error_in_the_background_is_contained(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as db:
        fake, result = deferred(db, RuntimeError("boom"))
        pending = result.pending
    assert pending is not None
    capture_defer.answer_in_background(
        sessions, fake, pending, sender=None, base_url="x", quiet=NO_QUIET
    )
    with sessions() as db:
        proposal = db.query(Proposal).one()
        assert not CapturePayload.model_validate(proposal.payload_json).processing


def test_an_edited_draft_is_left_alone_and_stops_processing(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as db:
        fake, result = deferred(db, janko())
        pending = result.pending
        proposal = db.query(Proposal).one()
        payload = CapturePayload.model_validate(proposal.payload_json)
        payload.notes[0].text = "my own wording"
        proposal.payload_json = payload.model_dump(mode="json")
        db.commit()
    assert pending is not None
    capture_defer.answer_in_background(
        sessions, fake, pending, sender=None, base_url="x", quiet=NO_QUIET
    )
    with sessions() as db:
        proposal = db.query(Proposal).one()
        payload = CapturePayload.model_validate(proposal.payload_json)
        assert payload.notes[0].text == "my own wording" and not payload.processing


# --- The worker path ---


def test_the_worker_queue_takes_a_deferred_capture_and_a_late_answer_applies(
    sessions: sessionmaker[Session],
) -> None:
    clock = Clock(datetime.now(UTC))
    alive(sessions)
    client = make_client(sessions, clock)
    with sessions() as db:
        result = capture.capture(
            db,
            client,
            "Kamil má rád kávu",
            today=TODAY,
            default_currency="EUR",
            defer=True,
            now=clock.now,
        )
        db.commit()
        pending = result.pending
    assert pending is not None
    capture_defer.dispatch(
        client,
        pending,
        sessions=sessions,
        sender=None,
        base_url="x",
        quiet=NO_QUIET,
        run_later=lambda *a, **k: (_ for _ in ()).throw(AssertionError("worker path")),
    )
    serve(sessions, note_answer("Likes coffee"), clock.now)
    with sessions() as db:
        assert claude_jobs.apply_late(db, late_ctx(), clock.now + timedelta(seconds=5)) == 1
        db.commit()
    with sessions() as db:
        payload = CapturePayload.model_validate(db.query(Proposal).one().payload_json)
        assert payload.notes[0].text == "Likes coffee" and not payload.processing


def test_a_job_nobody_took_expires_and_the_draft_stops_processing(
    sessions: sessionmaker[Session],
) -> None:
    clock = Clock(datetime.now(UTC))
    client = make_client(sessions, clock)
    with sessions() as db:
        result = capture.capture(
            db, client, "Kamil", today=TODAY, default_currency="EUR", defer=True, now=clock.now
        )
        db.commit()
        pending = result.pending
    assert pending is not None
    client.defer_capture(pending.request)
    with sessions() as db:
        later = clock.now + claude_jobs.LATE_TTL + timedelta(minutes=1)  # the real clock here
        claude_jobs.apply_late(db, late_ctx(), later)
        db.commit()
    with sessions() as db:
        payload = CapturePayload.model_validate(db.query(Proposal).one().payload_json)
        assert not payload.processing
        again = claude_jobs.apply_late(db, late_ctx(), later)
        assert again == 0


def test_a_full_queue_gives_up_at_once(sessions: sessionmaker[Session]) -> None:
    clock = Clock(datetime.now(UTC))
    client = make_client(sessions, clock)
    with sessions() as db:
        result = capture.capture(
            db, client, "Kamil", today=TODAY, default_currency="EUR", defer=True, now=clock.now
        )
        db.commit()
        pending = result.pending

    def full(_: CaptureRequest) -> None:
        raise ClaudeError("Claude's queue is full.")

    client.defer_capture = full  # type: ignore[method-assign]
    assert pending is not None
    capture_defer.dispatch(
        client,
        pending,
        sessions=sessions,
        sender=None,
        base_url="x",
        quiet=NO_QUIET,
        run_later=print,
    )
    with sessions() as db:
        payload = CapturePayload.model_validate(db.query(Proposal).one().payload_json)
        assert not payload.processing


# --- Through the endpoints ---


class Watching(FakeExtractor):
    """Records what the draft looked like while Claude was still thinking."""

    def __init__(self, app: FastAPI, answer: CaptureExtraction) -> None:
        super().__init__(answer)
        self.app = app
        self.seen: list[Any] = []

    def extract_capture(self, request: CaptureRequest) -> CaptureExtraction:
        with self.app.state.sessionmaker() as db:
            proposal = db.query(Proposal).one()
            self.seen.append(CapturePayload.model_validate(proposal.payload_json).processing)
        return super().extract_capture(request)


def test_the_api_answers_before_claude_reads_it(api: TestClient, app: FastAPI) -> None:
    watching = Watching(app, janko())
    app.state.claude = watching
    response = api.post("/api/v1/capture", json={"text": "Janko je nový kolega"})
    assert response.status_code == 200, response.text
    assert response.json()["summary"].startswith("Saved; Claude is reading it")
    assert watching.seen == [True]  # the draft was already saved while Claude was asked
    kinds = sorted(p["kind"] for p in api.get("/api/v1/inbox").json())
    assert kinds == ["capture", "new_person"]


def test_the_web_card_says_it_is_being_read_and_polls(web: TestClient, app: FastAPI) -> None:
    seen: list[str] = []

    class Peek(FakeExtractor):
        def extract_capture(self, request: CaptureRequest) -> CaptureExtraction:
            seen.append(web.get("/inbox").text)
            return super().extract_capture(request)

    app.state.claude = Peek(janko())
    post(web, "/inbox/capture", text="Janko je nový kolega")
    (during,) = seen
    assert "⏳ Claude is reading this…" in text(during)
    assert 'hx-trigger="every 3s"' in during and "/card" in during
    after = web.get("/inbox").text
    assert "Claude is reading this" not in after and "every 3s" not in after


def test_the_card_route_returns_just_the_card(web: TestClient, app: FastAPI) -> None:
    app.state.claude = FakeExtractor(janko())
    post(web, "/inbox/capture", text="Janko je nový kolega")
    with app.state.sessionmaker() as db:
        pid = db.query(Proposal).first().id  # type: ignore[union-attr]
    card = web.get(f"/inbox/{pid}/card")
    assert card.status_code == 200 and card.text.lstrip().startswith("<article")


def test_a_draft_older_than_ten_minutes_stops_saying_it_is_being_read(
    web: TestClient, app: FastAPI
) -> None:
    app.state.claude = FakeExtractor(ClaudeError("down"))
    post(web, "/inbox/capture", text="niečo")
    with app.state.sessionmaker() as db:
        proposal = db.query(Proposal).one()
        payload = CapturePayload.model_validate(proposal.payload_json)
        payload.processing = True  # as if the answer never came
        proposal.payload_json = payload.model_dump(mode="json")
        db.commit()
    assert "Claude is reading this" in text(web.get("/inbox").text)
    with app.state.sessionmaker() as db:
        proposal = db.query(Proposal).one()
        proposal.created_at = datetime.now(UTC) - timedelta(minutes=11)
        db.commit()
    assert "Claude is reading this" not in text(web.get("/inbox").text)
