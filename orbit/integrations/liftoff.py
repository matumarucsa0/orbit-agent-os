"""Liftoff (getgymbros.com) workouts: the app's private tRPC API, read only (SPEC §2.35).

Verified against the source of the unofficial `quantcli/liftoff-export-cli` (not against a live
response yet; the fixture is still missing, so nothing here parses workout fields).
- Base `https://v2-16-3.api.getgymbros.com`: the version is in the host name and the server
  answers "server is deprecated" after an app update, so the base comes from `LIFTOFF_API_BASE`.
- `user.refreshToken` (GET, `input={"0":{"json":"<refresh token>"}}`) returns `accessToken` and
  `accessTokenExpiresAt`; the refresh token is reused. The owner signs in with Google, so
  there is no password sign-in: the refresh token is captured from the app's own traffic.
- Every call is a tRPC batch GET `/api/trpc/<procedure>?batch=1&input=...` with a bearer token.
The access token lives in memory only. Errors never contain a token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

USER_AGENT = "Liftoff/528 CFNetwork/3860.400.51 Darwin/25.3.0"
TIMEOUT_SECONDS = 30.0
REFRESH_PROCEDURE = "user.refreshToken"
WORKOUTS_PROCEDURE = "post.getMyPosts"
_EARLY = timedelta(minutes=5)


class LiftoffError(Exception):
    """A call failed; the message is safe to show and log."""


class LiftoffDeprecated(LiftoffError):
    """The API version in the base URL is retired: set `LIFTOFF_API_BASE` to the new host."""


class LiftoffDenied(LiftoffError):
    """The refresh token was rejected: capture a new one."""


@dataclass
class _Access:
    token: str
    expires: datetime


class LiftoffClient:
    def __init__(
        self,
        base_url: str,
        refresh_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._refresh_token = refresh_token
        self._access: _Access | None = None
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def workouts(self) -> list[dict[str, Any]]:
        """Every workout of the account as the API returns it (`post.getMyPosts`)."""
        data = self._call(WORKOUTS_PROCEDURE, None, authorized=True)
        if not isinstance(data, list):
            raise LiftoffError("unexpected workouts response (not a list)")
        return [item for item in data if isinstance(item, dict)]

    def _token(self) -> str:
        now = datetime.now(UTC)
        if self._access is None or self._access.expires <= now:
            self._access = self._refresh(now)
        return self._access.token

    def _refresh(self, now: datetime) -> _Access:
        try:
            data = self._call(REFRESH_PROCEDURE, self._refresh_token, authorized=False)
        except LiftoffError as exc:
            if isinstance(exc, LiftoffDeprecated):
                raise
            raise LiftoffDenied(f"token refresh failed: {exc}") from None
        token = data.get("accessToken") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise LiftoffDenied("token refresh returned no access token")
        raw = data.get("accessTokenExpiresAt")
        try:
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            expires = now  # unparseable: refresh again on the next call
        return _Access(token, expires - _EARLY)

    def _call(self, procedure: str, argument: Any, *, authorized: bool) -> Any:
        if argument is None:
            payload: Any = {"json": None, "meta": {"values": ["undefined"]}}
        else:
            payload = {"json": argument}
        encoded = quote(json.dumps({"0": payload}, separators=(",", ":")), safe="")
        url = f"{self._base}/api/trpc/{procedure}?batch=1&input={encoded}"
        headers = {"Authorization": f"Bearer {self._token()}"} if authorized else {}
        try:
            response = self._client.get(url, headers=headers)
        except httpx.TimeoutException:
            raise LiftoffError("timed out") from None
        except httpx.HTTPError as exc:
            raise LiftoffError(exc.__class__.__name__) from None
        if "server is deprecated" in response.text.lower():
            raise LiftoffDeprecated(
                "the Liftoff API version is retired; set LIFTOFF_API_BASE to the current host"
            )
        if response.status_code != 200:
            raise LiftoffError(f"{procedure}: HTTP {response.status_code}")
        try:
            batch = response.json()
            item = batch[0]
        except (ValueError, IndexError, KeyError, TypeError):
            raise LiftoffError(f"{procedure}: not a tRPC batch response") from None
        if not isinstance(item, dict):
            raise LiftoffError(f"{procedure}: not a tRPC batch response")
        error = item.get("error")
        if error:
            message = error.get("json", {}).get("message") if isinstance(error, dict) else None
            raise LiftoffError(f"{procedure}: {str(message or 'error')[:200]}")
        try:
            return item["result"]["data"]["json"]
        except (KeyError, TypeError):
            raise LiftoffError(f"{procedure}: no data in response") from None
