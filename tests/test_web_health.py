from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def test_empty_page_points_at_the_shortcut(web: TestClient) -> None:
    page = web.get("/health")
    assert page.status_code == 200
    assert "No data yet" in page.text


def test_posted_days_show_with_dashes_for_missing_values(web: TestClient, api: TestClient) -> None:
    day = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    api.post(
        "/api/v1/health/daily",
        json={"day": day, "sleep_minutes": 423, "steps": 9120},
        headers={**api.headers},
    )
    page = web.get("/health")
    assert "7 h 03 min" in page.text  # 423 minutes
    assert "9120 steps" in page.text
    assert "resting HR -" in page.text  # no data is a dash, never zero


def test_the_page_needs_a_login(client: TestClient) -> None:
    assert client.get("/health", follow_redirects=False).status_code in (303, 401)
