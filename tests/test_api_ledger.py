from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


def person(api: TestClient, name: str) -> int:
    response = api.post("/api/v1/people", json={"display_name": name})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def debt(api: TestClient, **body: object) -> dict[str, object]:
    response = api.post("/api/v1/debts", json=body)
    assert response.status_code == 201, response.text
    result: dict[str, object] = response.json()
    return result


def test_ledger_endpoints_require_token(client: TestClient) -> None:
    for method, path in (
        ("get", "/api/v1/debts"),
        ("post", "/api/v1/debts"),
        ("post", "/api/v1/debts/1/settle"),
        ("get", "/api/v1/balances"),
        ("get", "/api/v1/promises"),
        ("post", "/api/v1/promises"),
        ("patch", "/api/v1/promises/1"),
    ):
        assert getattr(client, method)(path).status_code == 401, path


def test_create_and_list_debts(api: TestClient) -> None:
    pato = person(api, "Pato")
    pizza = debt(
        api, person_id=pato, direction="they_owe_me", amount_minor=1200, description="pizza"
    )
    assert pizza["currency"] == "EUR"
    assert pizza["reference_code"] == "D-0001"
    assert pizza["person"] == {"id": pato, "display_name": "Pato"}
    assert pizza["settled_at"] is None
    charger = debt(api, person_id=pato, direction="they_owe_me", type="item", description="charger")

    listed = api.get("/api/v1/debts").json()
    assert [d["id"] for d in listed] == [pizza["id"], charger["id"]]
    assert [d["id"] for d in api.get("/api/v1/debts?type=item").json()] == [charger["id"]]
    assert api.get(f"/api/v1/debts?person_id={pato + 1}").json() == []
    assert api.get(f"/api/v1/debts/{pizza['id']}").json()["description"] == "pizza"


def test_create_debt_validation(api: TestClient) -> None:
    pato = person(api, "Pato")
    # No amount on a money debt: a service-level error.
    response = api.post("/api/v1/debts", json={"person_id": pato, "direction": "they_owe_me"})
    assert response.status_code == 422
    assert "amount" in response.json()["detail"]
    # Float amounts are not money.
    response = api.post(
        "/api/v1/debts", json={"person_id": pato, "direction": "they_owe_me", "amount_minor": 12.5}
    )
    assert response.status_code == 422
    response = api.post(
        "/api/v1/debts", json={"person_id": 999, "direction": "they_owe_me", "amount_minor": 5}
    )
    assert response.status_code == 404


def test_patch_and_delete_debt(api: TestClient) -> None:
    pato = person(api, "Pato")
    pizza = debt(api, person_id=pato, direction="they_owe_me", amount_minor=1200)
    response = api.patch(
        f"/api/v1/debts/{pizza['id']}", json={"amount_minor": 1500, "due": "2026-10-01"}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["amount_minor"], response.json()["due"]) == (1500, "2026-10-01")
    assert api.delete(f"/api/v1/debts/{pizza['id']}").status_code == 204
    assert api.get(f"/api/v1/debts/{pizza['id']}").status_code == 404


def test_settle_partial_then_full(api: TestClient) -> None:
    pato = person(api, "Pato")
    pizza = debt(api, person_id=pato, direction="they_owe_me", amount_minor=1200)

    response = api.post(f"/api/v1/debts/{pizza['id']}/settle", json={"amount_minor": 500})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["settled"]["settled_at"] is not None
    rest = body["remainder"]
    assert (rest["amount_minor"], rest["parent_id"], rest["reference_code"]) == (
        700,
        pizza["id"],
        "D-0002",
    )

    assert api.post(f"/api/v1/debts/{pizza['id']}/settle").status_code == 422  # already settled
    assert (
        api.post(f"/api/v1/debts/{rest['id']}/settle", json={"amount_minor": 800}).status_code
        == 422
    )
    response = api.post(f"/api/v1/debts/{rest['id']}/settle")  # no body: full
    assert response.status_code == 200
    assert response.json()["remainder"] is None
    assert api.get("/api/v1/debts").json() == []
    assert len(api.get("/api/v1/debts?status=settled").json()) == 2
    assert len(api.get("/api/v1/debts?status=all").json()) == 2


def test_unsettle(api: TestClient) -> None:
    pato = person(api, "Pato")
    pizza = debt(api, person_id=pato, direction="they_owe_me", amount_minor=1200)
    api.post(f"/api/v1/debts/{pizza['id']}/settle", json={"amount_minor": 500})
    response = api.post(f"/api/v1/debts/{pizza['id']}/unsettle")
    assert response.status_code == 200
    assert response.json()["settled_at"] is None
    assert [d["id"] for d in api.get("/api/v1/debts").json()] == [pizza["id"]]


def test_balances(api: TestClient) -> None:
    pato, terka = person(api, "Pato"), person(api, "Terka")
    debt(api, person_id=pato, direction="they_owe_me", amount_minor=2400)
    debt(api, person_id=pato, direction="they_owe_me", amount_minor=25000, currency="CZK")
    debt(api, person_id=terka, direction="i_owe_them", amount_minor=1000)
    debt(api, person_id=terka, direction="i_owe_them", type="favor", description="moving")

    body = api.get("/api/v1/balances").json()
    assert body["totals"] == [
        {"currency": "CZK", "owed_to_me": 25000, "i_owe": 0, "net": 25000},
        {"currency": "EUR", "owed_to_me": 2400, "i_owe": 1000, "net": 1400},
    ]
    assert [
        (b["person"]["display_name"], b["currency"], b["amount_minor"]) for b in body["people"]
    ] == [
        ("Pato", "CZK", 25000),
        ("Pato", "EUR", 2400),
        ("Terka", "EUR", -1000),
    ]
    only_terka = api.get(f"/api/v1/balances?person_id={terka}").json()
    assert [b["amount_minor"] for b in only_terka["people"]] == [-1000]


def test_promises_crud(api: TestClient) -> None:
    pato = person(api, "Pato")
    response = api.post(
        "/api/v1/promises",
        json={"person_id": pato, "direction": "i_promised", "text": "send the photos"},
    )
    assert response.status_code == 201, response.text
    promise = response.json()
    today = datetime.now(ZoneInfo("Europe/Prague")).date()
    assert promise["due"] == (today + timedelta(days=7)).isoformat()
    assert promise["status"] == "open"

    response = api.patch(f"/api/v1/promises/{promise['id']}", json={"status": "done"})
    assert response.status_code == 200
    assert response.json()["closed_at"] is not None
    assert api.get("/api/v1/promises").json() == []
    assert [p["id"] for p in api.get("/api/v1/promises?status=done").json()] == [promise["id"]]
    assert len(api.get("/api/v1/promises?status=all").json()) == 1
    assert api.get("/api/v1/promises?status=nope").status_code == 422

    response = api.patch(f"/api/v1/promises/{promise['id']}", json={"status": "open", "due": None})
    assert (response.json()["status"], response.json()["due"], response.json()["closed_at"]) == (
        "open",
        None,
        None,
    )
    assert api.get(f"/api/v1/promises/{promise['id']}").status_code == 200
    assert api.delete(f"/api/v1/promises/{promise['id']}").status_code == 204
    assert api.get(f"/api/v1/promises/{promise['id']}").status_code == 404


def test_promise_with_explicit_due(api: TestClient) -> None:
    pato = person(api, "Pato")
    response = api.post(
        "/api/v1/promises",
        json={
            "person_id": pato,
            "direction": "they_promised",
            "text": "return book",
            "due": "2026-12-24",
        },
    )
    assert response.json()["due"] == date(2026, 12, 24).isoformat()


def test_delete_person_with_open_debt_is_refused(api: TestClient) -> None:
    pato = person(api, "Pato")
    debt(api, person_id=pato, direction="they_owe_me", amount_minor=100)
    response = api.delete(f"/api/v1/people/{pato}")
    assert response.status_code == 422
    assert "open debts" in response.json()["detail"]
