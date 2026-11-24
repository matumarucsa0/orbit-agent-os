"""Contact sync API: start, inspect, apply, undo, forget."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.deps import get_contact_source
from tests.test_web_contacts import FIXTURE, FakeSource


def test_requires_token(client: TestClient) -> None:
    assert client.post("/api/v1/contacts/sync").status_code == 401


def test_not_configured_and_server_error(app: FastAPI, api: TestClient) -> None:
    assert api.post("/api/v1/contacts/sync").status_code == 400
    app.dependency_overrides[get_contact_source] = lambda: FakeSource(error=True)
    assert api.post("/api/v1/contacts/sync").status_code == 502


def test_sync_apply_undo_flow(app: FastAPI, api: TestClient) -> None:
    app.dependency_overrides[get_contact_source] = lambda: FakeSource()
    run = api.post("/api/v1/contacts/sync").json()
    assert run["status"] == "review" and len(run["changes"]) == 7
    assert api.get("/api/v1/people").json() == []  # a review writes no people
    first = run["changes"][0]
    assert first["kind"] == "new_contact" and first["choices"] == ["apply"]

    applied = api.post(
        f"/api/v1/contacts/sync/{run['id']}/apply", json={"choices": {first["id"]: "apply"}}
    ).json()
    assert (applied["applied"], applied["skipped"]) == (1, 6)
    assert applied["run"]["status"] == "applied"

    undone = api.post(
        f"/api/v1/contacts/sync/{run['id']}/undo", json={"change_id": first["id"]}
    ).json()
    assert next(c for c in undone["changes"] if c["id"] == first["id"])["state"] == "undone"
    assert (
        api.post(
            f"/api/v1/contacts/sync/{run['id']}/undo", json={"change_id": first["id"]}
        ).status_code
        == 422
    )

    assert api.post("/api/v1/contacts/sync/forget").json() == {"forgotten": 6}
    assert api.get("/api/v1/contacts/sync/999").status_code == 404


def test_vcf_upload_and_bad_encoding(api: TestClient) -> None:
    files = {"file": ("c.vcf", FIXTURE.read_bytes(), "text/vcard")}
    run = api.post("/api/v1/contacts/sync/vcf", files=files).json()
    assert run["source"] == "vcf" and len(run["changes"]) == 7
    bad = {"file": ("x.vcf", "FN:Kováč".encode("cp1250"), "text/vcard")}
    assert api.post("/api/v1/contacts/sync/vcf", files=bad).status_code == 422
