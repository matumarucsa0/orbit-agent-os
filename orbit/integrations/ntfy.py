"""Push notifications through ntfy (self-hosted or ntfy.sh), published as JSON.

Action buttons: `view` opens a URL; `http` POSTs to one. HTTP actions must point at single-purpose
signed `/a/{token}` URLs, never at the bearer-token API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

MAX_ACTIONS = 3  # ntfy's limit per message
TIMEOUT_SECONDS = 10.0


class NtfyError(Exception):
    """The push could not be delivered (network error or a non-2xx answer)."""


@dataclass(frozen=True)
class Action:
    label: str
    url: str
    kind: Literal["view", "http"] = "view"
    # Dismiss the notification once the button is tapped.
    clear: bool = False

    def to_ntfy(self) -> dict[str, Any]:
        data: dict[str, Any] = {"action": self.kind, "label": self.label, "url": self.url}
        if self.kind == "http":
            data["method"] = "POST"
        if self.clear:
            data["clear"] = True
        return data


def view_action(label: str, url: str) -> Action:
    return Action(label, url, "view")


def http_action(label: str, url: str) -> Action:
    return Action(label, url, "http", clear=True)


@dataclass(frozen=True)
class PushMessage:
    title: str
    body: str
    click: str | None = None
    tags: tuple[str, ...] = ()
    actions: tuple[Action, ...] = field(default=())
    priority: int | None = None  # 1 (min) … 5 (max); None = ntfy's default (3)

    def __post_init__(self) -> None:
        if len(self.actions) > MAX_ACTIONS:
            raise ValueError(f"ntfy allows at most {MAX_ACTIONS} action buttons")

    def to_dict(self) -> dict[str, Any]:
        """Stored form (Notification.payload_json); the inverse of `from_dict`."""
        return {
            "title": self.title,
            "body": self.body,
            "click": self.click,
            "tags": list(self.tags),
            "actions": [
                {"label": a.label, "url": a.url, "kind": a.kind, "clear": a.clear}
                for a in self.actions
            ],
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PushMessage:
        return cls(
            title=data["title"],
            body=data["body"],
            click=data.get("click"),
            tags=tuple(data.get("tags") or ()),
            actions=tuple(Action(**a) for a in data.get("actions") or ()),
            priority=data.get("priority"),
        )

    def to_ntfy(self, topic: str) -> dict[str, Any]:
        data: dict[str, Any] = {"topic": topic, "title": self.title, "message": self.body}
        if self.click:
            data["click"] = self.click
        if self.tags:
            data["tags"] = list(self.tags)
        if self.actions:
            data["actions"] = [a.to_ntfy() for a in self.actions]
        if self.priority is not None:
            data["priority"] = self.priority
        return data


class PushSender(Protocol):
    def send(self, message: PushMessage) -> None:
        """Deliver the push or raise NtfyError."""


class NtfyClient:
    def __init__(
        self,
        base_url: str,
        topic: str,
        token: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/"
        self._topic = topic
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = http or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._headers = headers

    def send(self, message: PushMessage) -> None:
        try:
            response = self._http.post(
                self._url, json=message.to_ntfy(self._topic), headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise NtfyError(f"ntfy unreachable: {exc.__class__.__name__}") from exc
        if response.is_error:
            raise NtfyError(f"ntfy answered {response.status_code}: {response.text[:200]}")

    def close(self) -> None:
        self._http.close()
