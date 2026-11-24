from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from orbit import cli
from orbit.services.smoke import run_checks
from orbit.settings import Settings
from tests.conftest import TEST_API_TOKEN
from tests.test_db_copy import seed, stamped_engine


def failures(results: list[Any]) -> dict[str, str]:
    return {r.name: r.detail for r in results if not r.ok}


def test_all_checks_pass_against_the_app(client: TestClient, settings: Settings) -> None:
    results = run_checks(
        client, "http://testserver", TEST_API_TOKEN, settings.ics_token.get_secret_value()
    )
    assert failures(results) == {}
    assert {r.name for r in results} >= {"healthz", "API accepts ORBIT_API_TOKEN", "ICS feed"}


def test_wrong_tokens_fail(client: TestClient) -> None:
    results = run_checks(client, "http://testserver", "wrong-token", "wrong-ics-token")
    assert set(failures(results)) == {"API accepts ORBIT_API_TOKEN", "ICS feed"}


def fake_https_server(hsts: bool, redirect: bool, api_token: str = "api") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            if redirect:
                return httpx.Response(308, headers={"location": f"https://{request.url.host}/"})
            return httpx.Response(200)
        headers = {"strict-transport-security": "max-age=31536000"} if hsts else {}
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "db": "ok"}, headers=headers)
        if path == "/api/v1/inbox":
            authorized = request.headers.get("authorization") == f"Bearer {api_token}"
            return httpx.Response(200 if authorized else 401)
        if path.startswith("/calendar/"):
            return httpx.Response(
                200, text="BEGIN:VCALENDAR\r\n", headers={"content-type": "text/calendar"}
            )
        if path.startswith("/a/"):
            return httpx.Response(403)
        return httpx.Response(200)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_https_checks() -> None:
    with fake_https_server(hsts=True, redirect=True) as client:
        assert failures(run_checks(client, "https://orbit.example.com", "api", "ics")) == {}
    with fake_https_server(hsts=False, redirect=False) as client:
        failed = failures(run_checks(client, "https://orbit.example.com/", "api", "ics"))
    assert set(failed) == {"HSTS header", "http redirects to https"}


def test_unreachable_server_stops_early() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        results = run_checks(client, "https://orbit.example.com", "api", "ics")
    assert [(r.name, r.ok) for r in results] == [("healthz", False)]
    assert "connection refused" in results[0].detail


def test_bad_gateway_is_not_healthy() -> None:
    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    with httpx.Client(transport=httpx.MockTransport(gateway)) as client:
        results = run_checks(client, "http://orbit.local", "api", "ics")
    assert "healthz" in failures(results)


# --- CLI ---


def use(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


def test_cli_smoke_test_reports_failures(
    monkeypatch: pytest.MonkeyPatch, make_settings: Any, capsys: Any
) -> None:
    use(monkeypatch, make_settings(orbit_url="https://orbit.example.com"))
    fake = fake_https_server(hsts=False, redirect=True, api_token=TEST_API_TOKEN)
    monkeypatch.setattr(cli.httpx, "Client", lambda **_: fake)

    assert cli.main(["smoke-test"]) == 1
    out = capsys.readouterr().out
    assert "Checking https://orbit.example.com" in out
    assert "FAIL  HSTS header" in out
    assert "1 check(s) failed." in out
    assert TEST_API_TOKEN not in out


def test_cli_copy_db(
    monkeypatch: pytest.MonkeyPatch, make_settings: Any, tmp_path: Path, capsys: Any
) -> None:
    source_url = f"sqlite:///{(tmp_path / 'source.db').as_posix()}"
    target_url = f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
    seed(stamped_engine(source_url))
    stamped_engine(target_url).dispose()
    use(monkeypatch, make_settings(database_url=target_url))

    assert cli.main(["copy-db", source_url]) == 0
    assert "person                    2" in capsys.readouterr().out

    # A second run finds the data already there and refuses.
    assert cli.main(["copy-db", source_url]) == 1
    assert "Nothing copied" in capsys.readouterr().err


def test_cli_copy_db_refuses_the_same_database(
    monkeypatch: pytest.MonkeyPatch, make_settings: Any, capsys: Any
) -> None:
    use(monkeypatch, make_settings(database_url="sqlite:///./orbit.db"))
    assert cli.main(["copy-db", "sqlite:///./orbit.db"]) == 1
    assert "same database" in capsys.readouterr().err
