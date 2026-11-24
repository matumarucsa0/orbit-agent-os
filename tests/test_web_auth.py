from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orbit.app import create_app
from tests.conftest import TEST_PASSWORD


def login(client: TestClient, password: str = TEST_PASSWORD, next_: str | None = None):
    data = {"password": password}
    if next_ is not None:
        data["next"] = next_
    return client.post("/login", data=data, follow_redirects=False)


def test_login_page_renders(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert "/static/vendor/htmx.min.js" in response.text
    assert 'name="viewport"' in response.text


def test_static_assets_served(client: TestClient) -> None:
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_home_requires_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_wrong_password(client: TestClient) -> None:
    response = login(client, password="nope")
    assert response.status_code == 401
    assert "Wrong password." in response.text
    assert "orbit_session" not in response.cookies
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_success(client: TestClient) -> None:
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    set_cookie = response.headers["set-cookie"].lower()
    assert "orbit_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie

    home = client.get("/")
    assert home.status_code == 200
    assert "Today" in home.text
    assert "Log out" in home.text


def test_login_page_redirects_when_logged_in(client: TestClient) -> None:
    login(client)
    response = client.get("/login?next=/people", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/people"


def test_logout(client: TestClient) -> None:
    login(client)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    ("next_", "expected"),
    [
        ("/people?tag=gym", "/people?tag=gym"),
        ("//evil.example.com", "/"),
        ("/\\evil.example.com", "/"),
        ("https://evil.example.com", "/"),
        ("people", "/"),
        ("", "/"),
    ],
)
def test_next_redirect_is_local_only(client: TestClient, next_: str, expected: str) -> None:
    response = login(client, next_=next_)
    assert response.headers["location"] == expected


def test_redirect_preserves_query(client: TestClient) -> None:
    response = client.get("/?x=1", follow_redirects=False)
    assert response.headers["location"] == "/login?next=%2F%3Fx%3D1"


def test_cookie_secure_when_https(make_settings) -> None:
    app = create_app(make_settings(orbit_url="https://orbit.example.com"))
    with TestClient(app, base_url="https://testserver") as client:
        response = login(client)
    assert "secure" in response.headers["set-cookie"].lower()


def test_tampered_cookie_rejected(client: TestClient) -> None:
    client.cookies.set("orbit_session", "eyJhdXRoIjogdHJ1ZX0=.forged.signature")
    assert client.get("/", follow_redirects=False).status_code == 303
