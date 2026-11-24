from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.api.router import build_api_router
from tests.conftest import TEST_API_TOKEN


@pytest.fixture
def api_client(app: FastAPI) -> Iterator[TestClient]:
    # Exercise the real router-level dependency with a probe route.
    router = build_api_router()

    @router.get("/_probe")
    def probe() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TEST_API_TOKEN}"},
        {"Authorization": TEST_API_TOKEN},
        {"Authorization": f"Bearer {TEST_API_TOKEN}x"},
    ],
)
def test_rejects_missing_or_wrong_token(api_client: TestClient, headers: dict[str, str]) -> None:
    response = api_client.get("/api/v1/_probe", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_accepts_valid_token(api_client: TestClient) -> None:
    response = api_client.get(
        "/api/v1/_probe", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_session_cookie_does_not_grant_api_access(api_client: TestClient) -> None:
    from tests.conftest import TEST_PASSWORD

    api_client.post("/login", data={"password": TEST_PASSWORD})
    assert api_client.get("/api/v1/_probe").status_code == 401
