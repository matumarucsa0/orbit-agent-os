"""Static files are versioned by content and cached for good when asked for with the version."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from orbit.app import IMMUTABLE
from orbit.web.templating import STATIC_DIR, static_url


def test_static_url_carries_a_hash_of_the_file() -> None:
    url = static_url("css/app.css")
    assert re.fullmatch(r"/static/css/app\.css\?v=[0-9a-f]{10}", url)
    assert static_url("css/app.css") == url
    assert static_url("css/missing.css") == "/static/css/missing.css"


def test_a_versioned_file_is_cached_for_a_year(client: TestClient) -> None:
    response = client.get(static_url("css/app.css"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE
    assert response.content == (STATIC_DIR / "css/app.css").read_bytes()


def test_an_unversioned_file_is_not_marked_immutable(client: TestClient) -> None:
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert "immutable" not in response.headers.get("cache-control", "")
    assert client.get("/static/css/nope.css?v=1").headers.get("cache-control") != IMMUTABLE


def test_the_login_page_links_versioned_assets(client: TestClient) -> None:
    html = client.get("/login").text
    for path in ("css/app.css", "js/app.js", "vendor/htmx.min.js"):
        assert static_url(path) in html
