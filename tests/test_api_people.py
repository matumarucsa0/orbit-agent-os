from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.deps import get_contact_source
from orbit.integrations.carddav import CardDavError, RawCard

FIXTURE = Path(__file__).parent / "fixtures" / "vcard" / "apple_contacts.vcf"


def create(api: TestClient, **body: object) -> dict:
    response = api.post("/api/v1/people", json=body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/people"),
        ("post", "/api/v1/people"),
        ("get", "/api/v1/people/1"),
        ("get", "/api/v1/people/duplicates"),
        ("post", "/api/v1/people/merge"),
        ("get", "/api/v1/interactions"),
        ("post", "/api/v1/imports/vcard"),
        ("post", "/api/v1/imports/icloud-contacts"),
        ("get", "/api/v1/tags"),
    ],
)
def test_requires_token(client: TestClient, method: str, path: str) -> None:
    assert getattr(client, method)(path).status_code == 401


def test_person_lifecycle(api: TestClient) -> None:
    person = create(api, display_name="Patrik Veselý", country="sk")
    assert person["nameday_country"] == "SK"
    assert person["country"] == "SK"
    pid = person["id"]

    patched = api.patch(f"/api/v1/people/{pid}", json={"city": "Košice", "first_name": "Patrik"})
    assert patched.status_code == 200
    assert patched.json()["city"] == "Košice"
    assert patched.json()["display_name"] == "Patrik Veselý"

    assert api.post(f"/api/v1/people/{pid}/aliases", json={"value": "Paťo"}).status_code == 201
    handle = api.post(
        f"/api/v1/people/{pid}/handles", json={"type": "phone", "value": "0905 123 456"}
    ).json()
    assert handle["value_norm"] == "+421905123456"
    tag = api.post(f"/api/v1/people/{pid}/tags", json={"name": "Uni"}).json()
    note = api.post(f"/api/v1/people/{pid}/notes", json={"text": "Has a dog"}).json()
    edited = api.patch(f"/api/v1/people/{pid}/notes/{note['id']}", json={"text": "Has two dogs"})
    assert edited.json()["text"] == "Has two dogs"
    interaction = api.post(
        "/api/v1/interactions",
        json={"occurred_on": "2026-09-20", "kind": "call", "person_ids": [pid]},
    )
    assert interaction.status_code == 201

    detail = api.get(f"/api/v1/people/{pid}").json()
    assert [a["value"] for a in detail["aliases"]] == ["Paťo"]
    assert [t["name"] for t in detail["tags"]] == ["Uni"]
    assert detail["last_interaction_on"] == "2026-09-20"
    assert [i["kind"] for i in detail["interactions"]] == ["call"]

    assert [p["id"] for p in api.get("/api/v1/people", params={"q": "paťo"}).json()] == [pid]
    assert api.get("/api/v1/people", params={"q": "0905"}).json()[0]["id"] == pid
    assert api.get("/api/v1/people", params={"tag": "uni"}).json()[0]["id"] == pid
    assert api.get("/api/v1/tags").json() == [
        {"id": tag["id"], "name": "Uni", "cadence_days": None, "people": 1}
    ]

    for sub, sub_id in (
        ("handles", handle["id"]),
        ("tags", tag["id"]),
        ("notes", note["id"]),
        ("aliases", detail["aliases"][0]["id"]),
    ):
        assert api.delete(f"/api/v1/people/{pid}/{sub}/{sub_id}").status_code == 204
    assert api.delete(f"/api/v1/people/{pid}").status_code == 204
    assert api.get(f"/api/v1/people/{pid}").status_code == 404


def test_instagram_only_person(api: TestClient) -> None:
    person = create(api, display_name="someone")
    response = api.post(
        f"/api/v1/people/{person['id']}/handles", json={"type": "instagram", "value": "@someone"}
    )
    assert response.json()["value_norm"] == "someone"
    assert api.get("/api/v1/people", params={"q": "@someone"}).json()[0]["id"] == person["id"]


def test_errors(api: TestClient) -> None:
    a = create(api, display_name="A")
    b = create(api, display_name="B")
    api.post(f"/api/v1/people/{a['id']}/handles", json={"type": "email", "value": "x@example.com"})

    conflict = api.post(
        f"/api/v1/people/{b['id']}/handles", json={"type": "email", "value": "X@example.com"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["owner_id"] == a["id"]
    assert conflict.json()["owner_name"] == "A"

    bad_birthday = api.patch(
        f"/api/v1/people/{a['id']}", json={"birthday_month": 2, "birthday_day": 31}
    )
    assert bad_birthday.status_code == 422
    assert api.post("/api/v1/people", json={"display_name": ""}).status_code == 422
    assert api.get("/api/v1/people/999").status_code == 404
    assert api.delete(f"/api/v1/people/{a['id']}/notes/999").status_code == 404


def test_duplicates_and_merge(api: TestClient) -> None:
    keep = create(api, display_name="Terézia Nováková")
    other = create(api, display_name="terezia novakova", city="Košice")
    found = api.get("/api/v1/people/duplicates").json()
    assert [(d["a"]["id"], d["b"]["id"], d["reason"]) for d in found] == [
        (keep["id"], other["id"], "Same name")
    ]

    merged = api.post("/api/v1/people/merge", json={"keep_id": keep["id"], "merge_id": other["id"]})
    assert merged.status_code == 200
    assert merged.json()["city"] == "Košice"
    assert api.get(f"/api/v1/people/{other['id']}").status_code == 404
    assert api.get("/api/v1/people/duplicates").json() == []


def test_dismiss_duplicate(api: TestClient) -> None:
    a = create(api, display_name="Martin Horváth")
    b = create(api, display_name="Martin Horváth")
    response = api.post(
        "/api/v1/people/duplicates/dismiss", json={"a_id": a["id"], "b_id": b["id"]}
    )
    assert response.status_code == 204
    assert api.get("/api/v1/people/duplicates").json() == []


def test_interactions_api(api: TestClient) -> None:
    a = create(api, display_name="A")
    b = create(api, display_name="B")
    created = api.post(
        "/api/v1/interactions",
        json={"occurred_on": "2026-09-01", "person_ids": [a["id"], b["id"]], "summary": "kafe"},
    ).json()
    assert created["kind"] == "meet"
    assert {p["display_name"] for p in created["people"]} == {"A", "B"}

    listed = api.get("/api/v1/interactions", params={"person_id": b["id"]}).json()
    assert [i["id"] for i in listed] == [created["id"]]
    patched = api.patch(
        f"/api/v1/interactions/{created['id']}", json={"person_ids": [a["id"]], "kind": "call"}
    ).json()
    assert [p["id"] for p in patched["people"]] == [a["id"]]
    assert api.delete(f"/api/v1/interactions/{created['id']}").status_code == 204
    assert api.patch(f"/api/v1/interactions/{created['id']}", json={}).status_code == 404
    bad = api.post("/api/v1/interactions", json={"occurred_on": "2026-09-01", "person_ids": []})
    assert bad.status_code == 422


def test_vcard_upload(api: TestClient) -> None:
    files = {"file": ("contacts.vcf", FIXTURE.read_bytes(), "text/vcard")}
    first = api.post("/api/v1/imports/vcard", files=files).json()
    assert (first["created"], first["summary"]) == (7, "7 new, 0 updated, 0 unchanged")
    again = api.post("/api/v1/imports/vcard", files=files).json()
    assert (again["created"], again["unchanged"]) == (0, 7)

    not_utf8 = {"file": ("x.vcf", "FN:Kováč".encode("cp1250"), "text/vcard")}
    assert api.post("/api/v1/imports/vcard", files=not_utf8).status_code == 422


class FakeSource:
    def __init__(self, error: bool = False) -> None:
        self.error = error

    def fetch_cards(self) -> list[RawCard]:
        if self.error:
            raise CardDavError(
                "The contacts server rejected the username or app-specific password."
            )
        return [RawCard(FIXTURE.read_text(encoding="utf-8"))]


def test_icloud_import(app: FastAPI, api: TestClient) -> None:
    assert api.post("/api/v1/imports/icloud-contacts").status_code == 400  # not configured

    app.dependency_overrides[get_contact_source] = lambda: FakeSource()
    assert api.post("/api/v1/imports/icloud-contacts").json()["created"] == 7

    app.dependency_overrides[get_contact_source] = lambda: FakeSource(error=True)
    failed = api.post("/api/v1/imports/icloud-contacts")
    assert failed.status_code == 502
    assert "app-specific password" in failed.json()["detail"]
