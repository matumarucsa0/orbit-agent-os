from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.integrations.claude import ExtractedNote, UnknownPerson
from orbit.services.action_tokens import make_token
from orbit.settings import Settings
from tests.test_api_ledger import person
from tests.test_capture import FakeExtractor, extraction, slovak_answer

SLOVAK = "zaplatil som Paťovi pizzu 12€, sľúbil som mu poslať rozvrh"


def use_claude(app: FastAPI, extractor: FakeExtractor) -> FakeExtractor:
    app.state.claude = extractor
    return extractor


def slovak_capture(api: TestClient, app: FastAPI) -> tuple[int, dict[str, object]]:
    """ROADMAP Phase 4: the Slovak example, dictated from the `Orbit Capture` Shortcut."""
    pato = person(api, "Patrik Veselý")
    api.post(f"/api/v1/people/{pato}/aliases", json={"value": "Paťo"})
    use_claude(app, FakeExtractor(slovak_answer(pato)))
    response = api.post("/api/v1/capture", json={"text": SLOVAK, "meeting_id": ""})
    assert response.status_code == 200, response.text
    return pato, response.json()


def test_inbox_endpoints_require_token(client: TestClient) -> None:
    for method, path in (
        ("post", "/api/v1/capture"),
        ("get", "/api/v1/inbox"),
        ("get", "/api/v1/inbox/1"),
        ("post", "/api/v1/inbox/1/confirm"),
        ("post", "/api/v1/inbox/1/reject"),
    ):
        assert getattr(client, method)(path).status_code == 401, path


def test_capture_returns_a_summary_for_the_shortcut(api: TestClient, app: FastAPI) -> None:
    pato, body = slovak_capture(api, app)
    # The request answers at once with the text saved as a draft (SPEC §2.33) ...
    assert body["items"] == 1
    assert body["summary"].startswith("Saved; Claude is reading it and you get a push")
    (proposal_id,) = body["proposal_ids"]

    # Nothing is applied until confirmed.
    assert api.get("/api/v1/debts").json() == []
    assert api.get("/api/v1/promises").json() == []

    (item,) = api.get("/api/v1/inbox").json()
    assert item["id"] == proposal_id
    assert item["kind"] == "capture" and item["status"] == "pending"
    assert item["payload"]["debts"][0]["amount_minor"] == 1200
    assert item["payload"]["debts"][0]["person"] == str(pato)


def test_confirm_creates_the_debt_and_promise(api: TestClient, app: FastAPI) -> None:
    pato, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    response = api.post(f"/api/v1/inbox/{proposal_id}/confirm")
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Saved: 1 debt, 1 promise."
    assert response.json()["proposal"]["status"] == "confirmed"

    (debt,) = api.get("/api/v1/debts").json()
    assert (debt["person"]["id"], debt["amount_minor"], debt["description"]) == (
        pato,
        1200,
        "pizza",
    )
    (promise,) = api.get("/api/v1/promises").json()
    assert promise["text"] == "poslať rozvrh" and promise["direction"] == "i_promised"
    assert api.get("/api/v1/inbox").json() == []

    again = api.post(f"/api/v1/inbox/{proposal_id}/confirm")
    assert again.status_code == 422
    assert len(api.get("/api/v1/debts").json()) == 1


def test_edit_then_confirm_via_api(api: TestClient, app: FastAPI) -> None:
    _, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    payload = api.get(f"/api/v1/inbox/{proposal_id}").json()["payload"]
    payload["debts"][0]["amount_minor"] = None
    missing = api.post(f"/api/v1/inbox/{proposal_id}/confirm", json={"payload": payload})
    assert missing.status_code == 422
    assert "Amount missing" in missing.json()["detail"]
    assert api.get("/api/v1/debts").json() == []

    payload["debts"][0]["amount_minor"] = 1350
    response = api.post(f"/api/v1/inbox/{proposal_id}/confirm", json={"payload": payload})
    assert response.status_code == 200, response.text
    assert api.get("/api/v1/debts").json()[0]["amount_minor"] == 1350


def test_reject(api: TestClient, app: FastAPI) -> None:
    _, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    response = api.post(f"/api/v1/inbox/{proposal_id}/reject")
    assert response.status_code == 200 and response.json()["status"] == "rejected"
    assert api.get("/api/v1/inbox").json() == []
    assert [p["id"] for p in api.get("/api/v1/inbox?status=rejected").json()] == [proposal_id]
    assert api.get("/api/v1/debts").json() == []


def test_capture_with_unknown_people(api: TestClient, app: FastAPI) -> None:
    use_claude(
        app,
        FakeExtractor(
            extraction(
                notes=[ExtractedNote(person_ref="new:0", text="nová kolegyňa")],
                unknown_people=[
                    UnknownPerson(name="Evka", context="colleague"),
                    UnknownPerson(name="Janko", context="from the gym"),
                ],
            )
        ),
    )
    body = api.post("/api/v1/capture", json={"text": "Evka je nová kolegyňa, Janko z gymu"}).json()
    assert body["items"] == 1  # the draft; Claude's answer replaces it a moment later
    (capture_id,) = body["proposal_ids"]
    listed = api.get("/api/v1/inbox").json()
    kinds = {p["id"]: p["kind"] for p in listed}
    (new_person_id,) = (i for i, kind in kinds.items() if kind == "new_person")
    assert kinds == {capture_id: "capture", new_person_id: "new_person"}

    created = api.post(f"/api/v1/inbox/{new_person_id}/confirm").json()
    janko = api.get(f"/api/v1/people/{created['person_id']}").json()
    assert janko["display_name"] == "Janko"
    api.post(f"/api/v1/inbox/{capture_id}/confirm")
    assert [p["display_name"] for p in api.get("/api/v1/people").json()] == ["Evka", "Janko"]


def test_capture_without_claude_keeps_the_text(api: TestClient) -> None:
    body = api.post("/api/v1/capture", json={"text": "niečo dôležité"}).json()
    assert body["summary"].startswith("Claude isn't configured; saved your text as a note draft.")
    (item,) = api.get("/api/v1/inbox").json()
    assert item["payload"]["notes"] == [{"include": True, "person": None, "text": "niečo dôležité"}]


def test_capture_validation(api: TestClient) -> None:
    assert api.post("/api/v1/capture", json={"text": ""}).status_code == 422
    assert api.post("/api/v1/capture", json={"text": "   "}).status_code == 422
    assert api.post("/api/v1/capture", json={}).status_code == 422


# --- /a/{token}: push action buttons ---


def token(settings: Settings, proposal_id: int, action: str, now: datetime | None = None) -> str:
    return make_token(settings.orbit_secret_key.get_secret_value(), proposal_id, action, now)


def test_action_confirm_needs_no_bearer_token(
    api: TestClient, app: FastAPI, settings: Settings
) -> None:
    _, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    anonymous = TestClient(app)
    response = anonymous.post(f"/a/{token(settings, proposal_id, 'confirm')}")
    assert response.status_code == 200, response.text
    assert response.text == "Saved: 1 debt, 1 promise."
    assert len(api.get("/api/v1/debts").json()) == 1

    # Single use: the proposal is resolved, so its tokens are spent.
    for action in ("confirm", "reject"):
        again = anonymous.post(f"/a/{token(settings, proposal_id, action)}")
        assert again.status_code == 409
        assert again.text == "Already confirmed."
    assert len(api.get("/api/v1/debts").json()) == 1


def test_action_reject(api: TestClient, app: FastAPI, settings: Settings) -> None:
    _, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    response = TestClient(app).post(f"/a/{token(settings, proposal_id, 'reject')}")
    assert (response.status_code, response.text) == (200, "Rejected.")
    assert api.get("/api/v1/debts").json() == []


def test_action_that_cant_be_applied_points_to_the_inbox(
    api: TestClient, app: FastAPI, settings: Settings
) -> None:
    pato = person(api, "Patrik Veselý")
    answer = slovak_answer(pato)
    answer.debts[0].amount = None  # "zaplatil som mu kávu": no amount said
    use_claude(app, FakeExtractor(answer))
    proposal_id = api.post("/api/v1/capture", json={"text": "kávu"}).json()["proposal_ids"][0]
    response = TestClient(app).post(f"/a/{token(settings, proposal_id, 'confirm')}")
    assert response.status_code == 422
    assert "Open the Inbox" in response.text
    assert api.get(f"/api/v1/inbox/{proposal_id}").json()["status"] == "pending"


def test_bad_or_expired_action_tokens(api: TestClient, app: FastAPI, settings: Settings) -> None:
    _, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    anonymous = TestClient(app)
    assert anonymous.post("/a/not-a-token").status_code == 403
    old = token(settings, proposal_id, "confirm", datetime.now(UTC) - timedelta(days=8))
    expired = anonymous.post(f"/a/{old}")
    assert expired.status_code == 403 and "expired" in expired.text
    assert anonymous.post(f"/a/{token(settings, 999, 'confirm')}").status_code == 403
    # The API bearer token is no substitute.
    assert anonymous.post(f"/a/{settings.orbit_api_token.get_secret_value()}").status_code == 403
    assert api.get(f"/api/v1/inbox/{proposal_id}").json()["status"] == "pending"


def test_reopen_via_api(api: TestClient, app: FastAPI) -> None:
    _, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    assert api.post(f"/api/v1/inbox/{proposal_id}/reopen").status_code == 422  # still pending
    api.post(f"/api/v1/inbox/{proposal_id}/reject")
    response = api.post(f"/api/v1/inbox/{proposal_id}/reopen")
    assert response.status_code == 200, response.text
    reopened = response.json()
    assert reopened["status"] == "pending" and reopened["resolved_at"] is None
    assert reopened["reopened_at"] is not None


def test_push_buttons_made_before_a_reopen_stay_spent(
    api: TestClient, app: FastAPI, settings: Settings
) -> None:
    """A mis-tapped "Not a debt" reopened in the Inbox mustn't bring the old Settle button back."""
    _, body = slovak_capture(api, app)
    proposal_id = body["proposal_ids"][0]
    earlier = datetime.now(UTC) - timedelta(hours=1)
    old_confirm = token(settings, proposal_id, "confirm", earlier)
    anonymous = TestClient(app)
    assert (
        anonymous.post(f"/a/{token(settings, proposal_id, 'reject', earlier)}").text == "Rejected."
    )
    api.post(f"/api/v1/inbox/{proposal_id}/reopen")

    response = anonymous.post(f"/a/{old_confirm}")
    assert response.status_code == 409 and "Reopened" in response.text
    assert api.get(f"/api/v1/inbox/{proposal_id}").json()["status"] == "pending"
    assert api.get("/api/v1/debts").json() == []
