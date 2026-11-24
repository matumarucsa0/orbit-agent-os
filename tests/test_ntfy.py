from __future__ import annotations

import json

import httpx
import pytest

from orbit.integrations.ntfy import (
    NtfyClient,
    NtfyError,
    PushMessage,
    http_action,
    view_action,
)


def message() -> PushMessage:
    return PushMessage(
        title="Received 24.00 € from Patrik Veselý",
        body="“pizza + kino” · Matches: pizza 12 € + kino 12 €",
        click="https://orbit.example.com/inbox/173",
        tags=("moneybag",),
        actions=(
            http_action("Settle", "https://orbit.example.com/a/tok1"),
            view_action("Choose…", "https://orbit.example.com/inbox/173"),
            http_action("Not a debt", "https://orbit.example.com/a/tok2"),
        ),
    )


def test_publish_json_matches_the_shortcuts_contract() -> None:
    assert message().to_ntfy("orbit-topic") == {
        "topic": "orbit-topic",
        "title": "Received 24.00 € from Patrik Veselý",
        "message": "“pizza + kino” · Matches: pizza 12 € + kino 12 €",
        "tags": ["moneybag"],
        "click": "https://orbit.example.com/inbox/173",
        "actions": [
            {
                "action": "http",
                "label": "Settle",
                "url": "https://orbit.example.com/a/tok1",
                "method": "POST",
                "clear": True,
            },
            {"action": "view", "label": "Choose…", "url": "https://orbit.example.com/inbox/173"},
            {
                "action": "http",
                "label": "Not a debt",
                "url": "https://orbit.example.com/a/tok2",
                "method": "POST",
                "clear": True,
            },
        ],
    }


def test_minimal_message_leaves_optional_fields_out() -> None:
    assert PushMessage("Hi", "there").to_ntfy("t") == {
        "topic": "t",
        "title": "Hi",
        "message": "there",
    }


def test_stored_form_round_trips() -> None:
    original = message()
    assert PushMessage.from_dict(json.loads(json.dumps(original.to_dict()))) == original


def test_at_most_three_actions() -> None:
    with pytest.raises(ValueError, match="at most 3"):
        PushMessage("t", "b", actions=tuple(view_action(str(i), "https://x") for i in range(4)))


def client_with(handler: object, token: str | None = "tk_secret") -> NtfyClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return NtfyClient(
        "https://ntfy.example.com/", "orbit-topic", token, httpx.Client(transport=transport)
    )


def test_send_posts_json_to_the_server_root_with_the_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    client_with(handler).send(message())
    (request,) = seen
    assert str(request.url) == "https://ntfy.example.com/"
    assert request.headers["Authorization"] == "Bearer tk_secret"
    body = json.loads(request.content)
    assert body["topic"] == "orbit-topic"
    assert body["actions"][0]["url"] == "https://orbit.example.com/a/tok1"


def test_send_without_token_sends_no_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client_with(handler, token=None).send(PushMessage("t", "b"))
    assert "Authorization" not in seen[0].headers


def test_http_error_raises_ntfy_error() -> None:
    client = client_with(lambda request: httpx.Response(403, text="forbidden"))
    with pytest.raises(NtfyError, match="403"):
        client.send(PushMessage("t", "b"))


def test_network_error_raises_ntfy_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(NtfyError, match="unreachable"):
        client_with(handler).send(PushMessage("t", "b"))
