from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orbit.app import create_app


def test_healthz_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_healthz_needs_no_auth(client: TestClient) -> None:
    assert client.get("/healthz", headers={"Authorization": "Bearer wrong"}).status_code == 200


def test_healthz_db_unreachable(make_settings, tmp_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist" / "orbit.db"
    app = create_app(make_settings(database_url=f"sqlite:///{missing_dir.as_posix()}"))
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "error"
