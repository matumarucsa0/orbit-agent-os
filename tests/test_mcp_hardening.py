"""MCP hardening (W6): the scoped token, argument validation, the audit log."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit import mcp_server
from orbit.mcp_server import (
    AuditLog,
    InvalidArguments,
    build_server,
    make_client,
    resolve_token,
    validate_arguments,
)
from tests.conftest import AUTH
from tests.route_inventory import all_routes

MCP_TOKEN = "mcp-only-token-" + "m" * 32
MCP_AUTH = {"Authorization": f"Bearer {MCP_TOKEN}"}


@pytest.fixture
def mcp_app(make_settings: Any) -> FastAPI:
    from orbit.app import create_app
    from tests.conftest import reset_schema

    app = create_app(make_settings(orbit_mcp_token=MCP_TOKEN))
    reset_schema(app.state.engine)
    return app


@pytest.fixture
def mcp_client(mcp_app: FastAPI) -> TestClient:
    return TestClient(mcp_app)


# --- The token opens GET /api/v1/context and nothing else ---


def test_mcp_token_reads_context(mcp_client: TestClient) -> None:
    assert mcp_client.get("/api/v1/context", headers=MCP_AUTH).status_code == 200
    assert mcp_client.get("/api/v1/context", headers=AUTH).status_code == 200


def test_mcp_token_not_accepted_without_setting(client: TestClient) -> None:
    assert client.get("/api/v1/context", headers=MCP_AUTH).status_code == 401


def test_mcp_token_cannot_write_context(mcp_client: TestClient) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = mcp_client.request(method, "/api/v1/context", headers=MCP_AUTH)
        assert response.status_code in (401, 403, 405), method


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _routes(app: FastAPI) -> list[tuple[str, str]]:
    return all_routes(app)


def test_mcp_token_is_rejected_on_every_other_api_route(mcp_client: TestClient) -> None:
    routes = _routes(mcp_client.app)  # type: ignore[arg-type]
    api_routes = [(m, p) for m, p in routes if p.startswith("/api/")]
    assert len(api_routes) > 40  # the enumeration really found the API
    checked = 0
    for method, path in api_routes:
        if (method, path) == ("GET", "/api/v1/context"):
            continue
        response = mcp_client.request(
            method, _concrete(path), headers=MCP_AUTH, follow_redirects=False
        )
        assert response.status_code in (401, 403), (method, path, response.status_code)
        checked += 1
    assert checked > 40


def test_mcp_token_gets_no_web_session(mcp_client: TestClient) -> None:
    """The bearer header means nothing to the web pages: same answer as no header."""
    web_routes = [
        (m, p) for m, p in _routes(mcp_client.app) if m == "GET" and not p.startswith("/api/")
    ]
    for _, path in web_routes:
        if "{" in path:
            continue
        with_token = mcp_client.get(path, headers=MCP_AUTH, follow_redirects=False)
        without = mcp_client.get(path, follow_redirects=False)
        assert with_token.status_code == without.status_code, path


def test_context_token_match_is_by_route_not_by_url_tricks(mcp_client: TestClient) -> None:
    for path in (
        "/api/v1/context/",
        "/api/v1/context/../people",
        "/api/v1//context",
        "/api/v1/people",
    ):
        response = mcp_client.get(path, headers=MCP_AUTH, follow_redirects=False)
        assert response.status_code != 200, path


# --- Validation ---


def test_validate_accepts_good_arguments() -> None:
    assert validate_arguments(None, None, None, None) == {}
    assert validate_arguments("next_7_days", None, None, 3) == {
        "preset": "next_7_days",
        "person_id": 3,
    }
    assert validate_arguments(None, "2026-10-01", "2026-10-31", None) == {
        "start": "2026-10-01",
        "end": "2026-10-31",
    }


@pytest.mark.parametrize(
    ("args", "why"),
    [
        (("this_month", None, None, None), "preset"),
        (("this_week; drop", None, None, None), "preset"),
        (("x" * 10_000, None, None, None), "preset"),
        ((None, "2026-10-01", None, None), "both"),
        ((None, None, "2026-10-01", None), "both"),
        ((None, "2026-13-01", "2026-13-02", None), "real date"),
        ((None, "2026-02-30", "2026-03-01", None), "real date"),
        ((None, "2026-10-01\n", "2026-10-02", None), "date like"),
        ((None, "20261001", "20261002", None), "date like"),
        ((None, "2026-10-01" + "0" * 5000, "2026-10-02", None), "date like"),
        ((None, "1999-12-31", "2000-01-02", None), "between"),
        ((None, "2100-12-31", "2101-01-01", None), "between"),
        ((None, "2026-10-05", "2026-10-01", None), "before"),
        ((None, "2026-01-01", "2026-12-31", None), "at most"),
        ((None, None, None, 0), "range"),
        ((None, None, None, -1), "range"),
        ((None, None, None, 2**31), "range"),
        ((None, None, None, 10**30), "range"),
        ((None, None, None, True), "whole number"),
        ((None, None, None, "3"), "whole number"),
        ((None, None, None, 1.5), "whole number"),
    ],
)
def test_validate_rejects_hostile_arguments(args: tuple[Any, ...], why: str) -> None:
    with pytest.raises(InvalidArguments, match=why):
        validate_arguments(*args)


def test_window_of_exactly_the_maximum_is_allowed() -> None:
    first = date(2026, 1, 1)
    last = date.fromordinal(first.toordinal() + mcp_server.MAX_WINDOW_DAYS - 1)
    assert validate_arguments(None, first.isoformat(), last.isoformat(), None)


def test_max_window_matches_the_api() -> None:
    from orbit.services.context.windows import MAX_WINDOW_DAYS

    assert mcp_server.MAX_WINDOW_DAYS == MAX_WINDOW_DAYS


# --- Audit log ---


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _call(server: Any, **arguments: Any) -> None:
    await server.call_tool("get_context", arguments)


def _server(tmp_path: Path, handler: Any, max_bytes: int = 1_000_000) -> tuple[Any, Path]:
    log_path = tmp_path / "audit" / "mcp.jsonl"
    client = make_client("http://orbit.test", "t", transport=httpx.MockTransport(handler))
    return build_server(client, AuditLog(log_path, max_bytes)), log_path


async def test_audit_log_records_ok_rejected_and_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("person_id") == "9":
            return httpx.Response(404, json={"detail": "Person not found."})
        return httpx.Response(200, json={"people": []})

    server, path = _server(tmp_path, handler)
    await _call(server, preset="this_week")
    for bad in ({"preset": "evil' OR 1=1"}, {"person_id": 9}):
        with pytest.raises(Exception, match=r"preset|404|Error"):
            await _call(server, **bad)

    ok, rejected, error = _lines(path)
    assert (ok["status"], ok["tool"], ok["args"]) == ("ok", "get_context", {"preset": "this_week"})
    assert ok["bytes"] == len('{"people":[]}') and ok["http_status"] == 200
    assert isinstance(ok["duration_ms"], int) and ok["ts"].endswith("+00:00")
    assert rejected["status"] == "rejected" and "args" not in rejected
    assert "evil" not in json.dumps(rejected)  # hostile input is hashed, not stored
    assert len(rejected["args_sha256"]) == 32
    assert error["status"] == "error" and error["http_status"] == 404
    assert oct(path.stat().st_mode & 0o777) == "0o600"


async def test_audit_log_never_holds_token_or_response(tmp_path: Path) -> None:
    server, path = _server(tmp_path, lambda r: httpx.Response(200, json={"secret_note": "private"}))
    await _call(server)
    text = path.read_text()
    assert "private" not in text and "Bearer" not in text


async def test_audit_log_rotates_by_size(tmp_path: Path) -> None:
    server, path = _server(tmp_path, lambda r: httpx.Response(200, json={}), max_bytes=10_000)
    for _ in range(200):
        await _call(server, preset="this_week")
    names = sorted(p.name for p in path.parent.iterdir())
    assert names == ["mcp.jsonl", "mcp.jsonl.1", "mcp.jsonl.2", "mcp.jsonl.3"]
    assert all(p.stat().st_size <= 10_000 for p in path.parent.iterdir())


def test_audit_log_appends_and_survives_unwritable_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "a.jsonl"
    AuditLog(path).write({"n": 1})
    AuditLog(path).write({"n": 2})
    assert [r["n"] for r in _lines(path)] == [1, 2]
    blocked = tmp_path / "file"
    blocked.write_text("x")
    AuditLog(blocked / "sub" / "a.jsonl").write({"n": 3})  # parent is a file: no crash
    assert "audit log write failed" in capsys.readouterr().err


def test_audit_log_env_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ORBIT_MCP_AUDIT_LOG", str(tmp_path / "x.jsonl"))
    monkeypatch.setenv("ORBIT_MCP_AUDIT_MAX_BYTES", "not a number")
    log = mcp_server.audit_log_from_env()
    assert log.path == tmp_path / "x.jsonl"


# --- Token choice ---


def test_resolve_token_prefers_mcp_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="orbit.mcp"):
        assert resolve_token({"ORBIT_MCP_TOKEN": "m", "ORBIT_API_TOKEN": "a"}) == "m"
    assert not caplog.records


def test_resolve_token_falls_back_with_deprecation_warning(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    with caplog.at_level(logging.WARNING, logger="orbit.mcp"):
        assert resolve_token({"ORBIT_API_TOKEN": "a"}) == "a"
    assert "deprecated" in caplog.text.lower()
    assert "DEPRECATED" in capsys.readouterr().err
    assert resolve_token({}) is None


async def test_tool_list_is_static_and_read_only() -> None:
    server = build_server(
        make_client(
            "http://orbit.test", "t", transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
    )
    tools = await server.list_tools()
    assert [t.name for t in tools] == ["get_context"]
    assert set(tools[0].input_schema["properties"]) == {"preset", "start", "end", "person_id"}
