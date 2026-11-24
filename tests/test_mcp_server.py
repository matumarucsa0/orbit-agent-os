import json

import httpx
import pytest

from orbit.mcp_server import OrbitMcpError, build_server, fetch_context, make_client


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return make_client("http://orbit.test/", "secret-token", transport=handler)


def test_fetch_context_sends_token_and_params() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"fitness": {}})

    text = fetch_context(_client(httpx.MockTransport(handler)), preset="next_7_days", person_id=3)

    assert json.loads(text) == {"fitness": {}}
    request = seen[0]
    assert request.url.path == "/api/v1/context"
    assert request.url.params["preset"] == "next_7_days"
    assert request.url.params["person_id"] == "3"
    assert "start" not in request.url.params
    assert request.headers["Authorization"] == "Bearer secret-token"


def test_fetch_context_error_shows_detail_not_token() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(422, json={"detail": "Bad window."}))

    with pytest.raises(OrbitMcpError) as excinfo:
        fetch_context(_client(transport), preset="this_week")

    assert "422" in str(excinfo.value)
    assert "Bad window." in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)


def test_fetch_context_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(OrbitMcpError, match="Cannot reach Orbit"):
        fetch_context(_client(httpx.MockTransport(handler)))


async def test_server_exposes_get_context_tool() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    server = build_server(_client(transport))

    tools = await server.list_tools()
    assert [t.name for t in tools] == ["get_context"]
    result = await server.call_tool("get_context", {"preset": "this_week"})
    assert "ok" in str(result)
