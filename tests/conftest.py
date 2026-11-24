from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from orbit.app import create_app
from orbit.db import make_engine, make_sessionmaker
from orbit.domain import Base
from orbit.services.passwords import hash_password
from orbit.settings import Settings

TEST_PASSWORD = "correct horse battery staple"
TEST_API_TOKEN = "test-api-token-" + "x" * 32
AUTH = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

# `ORBIT_TEST_DATABASE_URL=postgresql+psycopg://...` runs the suite against that (throwaway!)
# database, as CI does; every test starts from an empty schema. Default: in-memory SQLite.
TEST_DATABASE_URL = os.environ.get("ORBIT_TEST_DATABASE_URL") or "sqlite://"


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testserver"}


def _settings_env_names() -> set[str]:
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        names.add(name.upper())
        if isinstance(field.validation_alias, str):
            names.add(field.validation_alias)
    return names


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see the developer's (or the agent's) Orbit environment variables.

    `.claude/settings.json` sets ORBIT_CLAUDE_MODE=fake and friends so that running the app is
    safe, but the suite asserts the real defaults (for example "no key means Claude is none").
    A test that needs a variable sets it itself with monkeypatch.setenv.
    """
    for name in _settings_env_names():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a socket to a host outside this machine (and the test database).

    External services are faked behind small interfaces or `httpx.MockTransport`; a test that
    reaches the real network is a bug that could spend money or touch real accounts.
    """
    allowed = _LOOPBACK | {urlsplit(TEST_DATABASE_URL).hostname or ""}
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any, *args: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else None
        if host is not None and host not in allowed:
            raise RuntimeError(f"tests must not use the network (tried to connect to {host})")
        return real_connect(self, address, *args)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def concurrent_database_url(tmp_path: Path) -> str:
    """For tests where two threads call the app at once. In-memory SQLite shares one connection
    between threads, so one thread's rollback wipes the other's uncommitted rows; use a file."""
    if TEST_DATABASE_URL in ("sqlite://", "sqlite:///:memory:"):
        return f"sqlite:///{tmp_path / 'concurrent.db'}"
    return TEST_DATABASE_URL


def reset_schema(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    # Not a model table: a migration or stamp test on the shared Postgres database leaves it behind.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    Base.metadata.create_all(engine)


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(TEST_PASSWORD)


@pytest.fixture
def make_settings(password_hash: str):
    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "orbit_url": "http://testserver",
            "orbit_api_token": TEST_API_TOKEN,
            "orbit_web_password_hash": password_hash,
            "orbit_secret_key": "test-secret-key-" + "y" * 32,
            "ics_token": "test-ics-token-" + "z" * 32,
            "database_url": TEST_DATABASE_URL,
            "scheduler_enabled": False,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    reset_schema(app.state.engine)
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def api(client: TestClient) -> TestClient:
    """A client that sends the API bearer token."""
    client.headers.update(AUTH)
    return client


@pytest.fixture
def web(client: TestClient) -> TestClient:
    """A client with a logged-in web session."""
    response = client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return client


@pytest.fixture
def db() -> Iterator[Session]:
    """A fresh in-memory database with the full schema, for service tests."""
    engine = make_engine(TEST_DATABASE_URL)
    reset_schema(engine)
    with make_sessionmaker(engine)() as session:
        yield session
    engine.dispose()


# Files that take several seconds each (they parse the whole tree, run alembic, spawn the worker
# script or walk every route). `make fast` skips them; `make test` and CI run them.
SLOW_FILES = frozenset(
    {
        "test_select_tests.py",
        "test_claude_worker_script.py",
        "test_mcp_hardening.py",
        "test_migrations.py",
        "test_demo.py",
        "test_review_agent.py",
        "test_payments.py",
        "test_meetings.py",
        "test_claude_worker_api.py",
        "test_agent_tooling.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Add markers: `endpoint` for tests that boot the app (client, api, web or app fixture) and
    `slow` for the files in SLOW_FILES.

    `uv run pytest -m "not endpoint and not slow"` is the fast loop (`make fast`): services,
    parsers and pure logic. Nothing is deselected unless you ask with -m.
    """
    for item in items:
        if {"client", "api", "web", "app"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.endpoint)
        if item.path.name in SLOW_FILES:
            item.add_marker(pytest.mark.slow)
