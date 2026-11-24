from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from orbit.domain.inbox import Notification
from tests.test_bank_parsers import revolut_bytes, tatra_bytes
from tests.test_notifications import FakeSender


def upload(api: TestClient, raw: bytes, bank: str | None = "auto", name: str = "s.csv"):  # type: ignore[no-untyped-def]
    data = {"bank": bank} if bank else {}
    return api.post("/api/v1/imports/bank", files={"file": (name, raw, "text/csv")}, data=data)


def test_requires_the_bearer_token(client: TestClient) -> None:
    response = client.post("/api/v1/imports/bank", files={"file": ("s.csv", b"x", "text/csv")})
    assert response.status_code == 401


def test_orbit_import_shortcut(api: TestClient, app: FastAPI) -> None:
    """SHORTCUTS.md `Orbit Import`: file + bank=auto -> a summary to show."""
    sender = FakeSender()
    app.state.push_sender = sender
    response = upload(api, tatra_bytes(), name="download-12.09.2026.csv")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bank"] == "tatra"
    assert (body["rows_total"], body["new"], body["duplicates"], body["skipped"]) == (31, 31, 0, 0)
    assert body["flagged"] == 6 and len(body["proposal_ids"]) == 6
    # The 6 incoming transfers come from accounts nobody is linked to yet.
    assert body["payments"] == 6 and len(body["payment_proposal_ids"]) == 6
    assert body["summary"].startswith("31 new, 6 flagged for splitting, 6 payments received")

    inbox = api.get("/api/v1/inbox", params={"kind": "split_transaction"}).json()
    assert sorted(p["id"] for p in inbox) == sorted(body["proposal_ids"])
    matches = api.get("/api/v1/inbox", params={"kind": "payment_match"}).json()
    assert sorted(p["id"] for p in matches) == sorted(body["payment_proposal_ids"])
    # One push for the splits, one for the unknown senders (queued during quiet hours).
    assert len(sender.sent) in (0, 2)

    again = upload(api, tatra_bytes()).json()
    assert (again["new"], again["duplicates"], again["flagged"]) == (0, 31, 0)
    assert again["summary"] == "0 new, 31 already imported, 0 flagged for splitting"


def test_the_pushes_are_recorded_even_without_ntfy(api: TestClient, app: FastAPI) -> None:
    upload(api, tatra_bytes())
    with app.state.sessionmaker() as session:
        found = {n.kind: n.title for n in session.scalars(select(Notification))}
    assert found == {
        "split_transaction": "🧾 6 payments to split?",
        "payment_match": "💶 6 payments from unknown senders",
    }


def test_bank_can_be_named_or_left_out(api: TestClient) -> None:
    assert upload(api, revolut_bytes(), bank="revolut").json()["bank"] == "revolut"
    assert upload(api, tatra_bytes(), bank=None).json()["bank"] == "tatra"
    response = upload(api, tatra_bytes(), bank="revolut")
    assert response.status_code == 422
    assert "not revolut" in response.json()["detail"]
    assert upload(api, tatra_bytes(), bank="n26").status_code == 422


def test_rejects_files_it_cant_read(api: TestClient) -> None:
    response = upload(api, b"\x89PNG\r\n\x1a\n\x00\x00")
    assert response.status_code == 422
    assert "unrecognized header" in response.json()["detail"]
