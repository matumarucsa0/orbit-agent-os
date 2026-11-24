from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from orbit.app import create_app
from tests.conftest import AUTH, reset_schema
from tests.test_api_ledger import debt, person


def test_api_requires_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/context").status_code == 401


def test_api_returns_the_snapshot(api: TestClient) -> None:
    pato = person(api, "Patrik Veselý")
    debt(api, person_id=pato, direction="they_owe_me", type="money", amount_minor=1200)
    response = api.get("/api/v1/context")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["focus"] is None
    assert data["window"]["tz"] == "Europe/Prague"
    assert data["ledger"]["totals"] == [
        {"currency": "EUR", "owed_to_me_minor": 1200, "i_owe_minor": 0}
    ]
    assert data["budget"]["limit_chars"] == 400_000
    assert (
        date.fromisoformat(data["window"]["end"]) - date.fromisoformat(data["window"]["start"])
    ).days == 6


def test_api_focus_preset_and_custom_dates(api: TestClient) -> None:
    pato = person(api, "Patrik Veselý")
    other = person(api, "Terka")
    debt(api, person_id=pato, direction="they_owe_me", type="money", amount_minor=1200)
    debt(api, person_id=other, direction="they_owe_me", type="money", amount_minor=300)
    focused = api.get("/api/v1/context", params={"person_id": pato, "preset": "next_7_days"}).json()
    assert focused["focus"] == {"person_id": pato, "person_name": "Patrik Veselý"}
    assert [d["person"] for d in focused["ledger"]["debts"]] == ["Patrik Veselý"]
    today = date.today()
    custom = api.get(
        "/api/v1/context",
        params={"start": str(today), "end": str(today + timedelta(days=2))},
    ).json()
    assert custom["window"]["start"] == str(today)
    assert custom["window"]["end"] == str(today + timedelta(days=2))


def test_api_bad_window_is_422(api: TestClient) -> None:
    assert api.get("/api/v1/context", params={"preset": "fortnight"}).status_code == 422
    assert api.get("/api/v1/context", params={"start": "2026-09-01"}).status_code == 422


def test_budget_comes_from_settings(make_settings) -> None:
    app = create_app(make_settings(context_budget_chars=1000))
    reset_schema(app.state.engine)
    with TestClient(app, headers=AUTH) as small:
        pato = person(small, "Patrik Veselý")
        for i in range(30):
            debt(
                small, person_id=pato, direction="they_owe_me", type="item", description=f"book {i}"
            )
        data = small.get("/api/v1/context").json()
    assert data["budget"]["limit_chars"] == 1000
    assert data["budget"]["trimmed"]["ledger"] > 0
    assert data["ledger"]["truncated"] is True


def test_web_requires_login(client: TestClient) -> None:
    response = client.get("/context", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_web_shows_what_an_agent_would_receive(web: TestClient) -> None:
    response = web.get("/context")
    assert response.status_code == 200
    assert "Nothing was cut." in response.text
    assert "400,000" in response.text
    for module in ("People", "Ledger", "Meetings", "Promises", "As sent"):
        assert module in response.text
    assert '<option value="custom"' in response.text


def test_web_focus_and_custom_window(web: TestClient, api: TestClient) -> None:
    pato = person(api, "Patrik Veselý")
    response = web.get(
        "/context",
        params={"window": "custom", "start": "2026-09-01", "end": "2026-09-07", "person": pato},
    )
    assert response.status_code == 200
    assert "2026-09-01 to 2026-09-07" in response.text
    assert "about Patrik Veselý" in response.text
    assert f'<option value="{pato}" selected>' in response.text


def test_web_bad_dates_are_flashed_not_a_crash(web: TestClient) -> None:
    response = web.get("/context", params={"window": "custom", "start": "nope", "end": ""})
    assert response.status_code == 200
    assert "Not a date: nope" in response.text
    assert "Nothing was cut." not in response.text
