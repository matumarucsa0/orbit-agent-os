from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from orbit.integrations import namedays
from orbit.web.pages import day_label
from orbit.web.templating import static_url


def prague_today() -> date:
    return datetime.now(UTC).astimezone(ZoneInfo("Europe/Prague")).date()


def add_person(api: TestClient, **body: object) -> int:
    response = api.post("/api/v1/people", json=body)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def test_day_labels() -> None:
    today = date(2026, 9, 28)  # a Monday
    assert day_label(today, today) == "Today"
    assert day_label(today + timedelta(days=1), today) == "Tomorrow"
    assert day_label(today + timedelta(days=2), today) == "Wed 30 Sep"


def test_home_shows_today_and_upcoming(web: TestClient, settings) -> None:
    today = prague_today()
    tomorrow = today + timedelta(days=1)
    # Someone celebrating a name day tomorrow: take the first SK name on that date.
    tomorrow_names = namedays.names_on(tomorrow.month, tomorrow.day, "SK")
    token = {"Authorization": f"Bearer {settings.orbit_api_token.get_secret_value()}"}
    web.headers.update(token)
    add_person(
        web,
        display_name="Birthday Person",
        birthday_month=today.month,
        birthday_day=today.day,
        birthday_year=today.year - 32,  # a multiple of 4: still a leap year on 29 Feb
    )
    if tomorrow_names:
        add_person(web, display_name=f"{tomorrow_names[0]} Test")
    add_person(
        web, display_name="Far Away", birthday_month=((today.month + 5) % 12) + 1, birthday_day=1
    )

    page = web.get("/").text
    assert "Today" in page
    assert "🎂 Birthday Person" in page and "turns 32" in page
    if tomorrow_names:
        assert "Tomorrow" in page
        assert f"Name day ({tomorrow_names[0]})" in page
    assert "Far Away" not in page
    # The feed URL (secret) is on the page for the logged-in user.
    assert f"/calendar/{settings.ics_token.get_secret_value()}.ics" in page


def test_home_empty_state(web: TestClient) -> None:
    assert "No birthdays or name days in the next 7 days." in web.get("/").text


def test_person_page_shows_name_day(web: TestClient, settings) -> None:
    web.headers.update({"Authorization": f"Bearer {settings.orbit_api_token.get_secret_value()}"})
    pid = add_person(web, display_name="Paťo", first_name="Patrik")
    assert '🎉 6 Jul <span class="muted">· Patrik (SK)' in web.get(f"/people/{pid}").text
    no_country = add_person(web, display_name="Tobiáš", nameday_country=None)
    assert "No name-day country set." in web.get(f"/people/{no_country}").text


def test_home_needs_attention_and_money(web: TestClient, settings) -> None:
    web.headers.update({"Authorization": f"Bearer {settings.orbit_api_token.get_secret_value()}"})
    today = prague_today()
    pato = add_person(web, display_name="Paťo")
    terka = add_person(web, display_name="Terka")

    def post(path: str, **body: object) -> None:
        response = web.post(path, json=body)
        assert response.status_code == 201, response.text

    def promise(person_id: int, direction: str, text: str, days: int) -> None:
        due = (today + timedelta(days=days)).isoformat()
        post("/api/v1/promises", person_id=person_id, direction=direction, text=text, due=due)

    promise(pato, "i_promised", "send photos", 0)
    promise(terka, "they_promised", "return the drill", -3)
    promise(terka, "i_promised", "not yet", 2)
    post(
        "/api/v1/debts",
        person_id=pato,
        direction="they_owe_me",
        type="item",
        description="charger",
        reminder_days=1,
    )
    post("/api/v1/debts", person_id=pato, direction="they_owe_me", amount_minor=2400)

    page = web.get("/").text
    assert "Needs attention" in page
    assert "✅ send photos" in page and "due today" in page and "you promised Paťo" in page
    assert "✅ return the drill" in page and "Terka promised you" in page
    assert "overdue since" in page
    assert "not yet" not in page
    # Lent today with a 1-day reminder: not due yet.
    assert "has your charger" not in page
    assert "💶 Money" in page and "24.00 €" in page
    assert 'action="/people"' in page  # the search box


def test_pwa_manifest_and_icons(client: TestClient) -> None:
    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone" and data["start_url"] == "/"
    for icon in data["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200, icon["src"]
    touch = client.get("/static/icons/apple-touch-icon.png")
    assert touch.status_code == 200 and touch.content.startswith(b"\x89PNG")
    # Linked from every page, the login page included (no session needed).
    page = client.get("/login").text
    assert f'<link rel="manifest" href="{static_url("manifest.webmanifest")}">' in page
    touch_url = static_url("icons/apple-touch-icon.png")
    assert f'<link rel="apple-touch-icon" href="{touch_url}">' in page
