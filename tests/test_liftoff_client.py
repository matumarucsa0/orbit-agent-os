import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from orbit.integrations.liftoff import (
    LiftoffClient,
    LiftoffDenied,
    LiftoffDeprecated,
    LiftoffError,
)

BASE = "https://api.example.test"
REFRESH = "refresh-secret"


def _batch(data: object) -> httpx.Response:
    return httpx.Response(200, json=[{"result": {"data": {"json": data}}}])


def _expiry(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class Server:
    def __init__(self, *, expires_in: int = 60, workouts: object = None) -> None:
        self.calls: list[httpx.Request] = []
        self.expires_in = expires_in
        self.workouts = [{"id": "w1"}] if workouts is None else workouts

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path.endswith("/user.refreshToken"):
            return _batch(
                {"accessToken": "access-1", "accessTokenExpiresAt": _expiry(self.expires_in)}
            )
        return _batch(self.workouts)


def _client(server: Server) -> LiftoffClient:
    return LiftoffClient(BASE, REFRESH, transport=httpx.MockTransport(server))


def test_refresh_sends_the_token_as_trpc_input_then_calls_with_bearer() -> None:
    server = Server()
    assert _client(server).workouts() == [{"id": "w1"}]
    refresh, workouts = server.calls
    assert json.loads(parse_qs(urlparse(str(refresh.url)).query)["input"][0]) == {
        "0": {"json": REFRESH}
    }
    assert "authorization" not in refresh.headers
    assert workouts.headers["authorization"] == "Bearer access-1"
    assert json.loads(parse_qs(urlparse(str(workouts.url)).query)["input"][0]) == {
        "0": {"json": None, "meta": {"values": ["undefined"]}}
    }


def test_access_token_is_reused_until_it_expires() -> None:
    server = Server(expires_in=60)
    client = _client(server)
    client.workouts()
    client.workouts()
    assert sum(c.url.path.endswith("/user.refreshToken") for c in server.calls) == 1


def test_token_close_to_expiry_is_refreshed_again() -> None:
    server = Server(expires_in=3)  # inside the 5 minute safety margin
    client = _client(server)
    client.workouts()
    client.workouts()
    assert sum(c.url.path.endswith("/user.refreshToken") for c in server.calls) == 2


def test_deprecated_server_is_reported_with_the_fix() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="This Server is Deprecated")

    client = LiftoffClient(BASE, REFRESH, transport=httpx.MockTransport(handler))
    with pytest.raises(LiftoffDeprecated, match="LIFTOFF_API_BASE"):
        client.workouts()


def test_rejected_refresh_token_is_denied_and_never_leaks() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"error": {"json": {"message": "invalid token"}}}])

    client = LiftoffClient(BASE, REFRESH, transport=httpx.MockTransport(handler))
    with pytest.raises(LiftoffDenied) as caught:
        client.workouts()
    assert REFRESH not in str(caught.value)


def test_transport_error_message_has_no_url_or_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=None)

    client = LiftoffClient(BASE, REFRESH, transport=httpx.MockTransport(handler))
    with pytest.raises(LiftoffDenied) as caught:
        client.workouts()
    assert REFRESH not in str(caught.value)


def test_workouts_must_be_a_list() -> None:
    with pytest.raises(LiftoffError, match="not a list"):
        _client(Server(workouts={"oops": 1})).workouts()
