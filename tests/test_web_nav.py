"""The "More" menu: one list of links on /more and in the desktop dropdown (SPEC §2.24)."""

from __future__ import annotations

import re
from typing import Any

from fastapi.testclient import TestClient

from orbit.app import create_app
from orbit.web.templating import MORE_LINKS
from tests.conftest import TEST_PASSWORD

CORE = ("School", "Goals", "Review", "Reports", "Import", "Schedule", "Context")


def test_more_page_lists_every_extra_link_and_log_out(web: TestClient) -> None:
    page = web.get("/more")
    assert page.status_code == 200
    for href, label, _ in MORE_LINKS:
        if href == "/ops/claude":
            continue  # worker backend only
        assert f'href="{href}"' in page.text and label in page.text
    assert 'action="/logout"' in page.text
    assert "/ops/claude" not in page.text


def test_the_dropdown_uses_the_same_links_in_the_same_order(web: TestClient) -> None:
    html = web.get("/").text
    panel = html[html.index('class="more-panel"') :]
    positions = [
        panel.index(f'href="{href}"') for href, _, _ in MORE_LINKS if href != "/ops/claude"
    ]
    assert positions == sorted(positions)
    assert 'action="/logout"' in panel
    assert 'href="/more"' in html  # the phone tab
    assert len(re.findall(r'<a class="tab(?: tab-more)?"', html)) == 6


def test_claude_link_only_with_the_worker_backend(make_settings: Any) -> None:
    from tests.conftest import reset_schema

    app = create_app(make_settings(orbit_review_token="worker-token-" + "w" * 32))
    reset_schema(app.state.engine)
    with TestClient(app) as client:
        client.post("/login", data={"password": TEST_PASSWORD})
        assert 'href="/ops/claude"' in client.get("/more").text


def test_more_is_current_on_an_extra_page_and_on_more(web: TestClient) -> None:
    for path in ("/more", "/goals", "/school"):
        html = web.get(path).text
        assert '<summary aria-current="page">More</summary>' in html, path
        assert 'href="/more" aria-current="page"' in html, path
    assert "<summary >More</summary>" in web.get("/people").text


def test_more_needs_login(client: TestClient) -> None:
    response = client.get("/more", follow_redirects=False)
    assert response.status_code in (303, 401)
