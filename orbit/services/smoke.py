"""Read-only checks of a running Orbit instance over HTTP(S), for after a deploy.

Nothing is written: every request either reads or is expected to be refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _check(name: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(name, ok, "" if ok else detail)


def _status(
    client: httpx.Client, method: str, url: str, headers: dict[str, str] | None = None
) -> int | str:
    try:
        return client.request(method, url, headers=headers).status_code
    except httpx.HTTPError as exc:
        return f"request failed: {exc}"


def run_checks(
    client: httpx.Client, base_url: str, api_token: str, ics_token: str
) -> list[CheckResult]:
    base = base_url.rstrip("/")
    results: list[CheckResult] = []

    try:
        health = client.get(f"{base}/healthz")
    except httpx.HTTPError as exc:
        # Nothing else can work; say why and stop.
        return [CheckResult("healthz", False, f"request failed: {exc}")]
    healthy = (
        health.status_code == 200
        and health.headers.get("content-type", "").startswith("application/json")
        and health.json().get("db") == "ok"
    )
    results.append(_check("healthz", healthy, f"{health.status_code} {health.text[:200]}"))

    if base.startswith("https://"):
        hsts = health.headers.get("strict-transport-security", "")
        results.append(
            _check("HSTS header", "max-age" in hsts, "Strict-Transport-Security missing")
        )
        parts = urlsplit(base)
        plain = urlunsplit(("http", parts.netloc, "/healthz", "", ""))
        try:
            redirect = client.get(plain, follow_redirects=False)
            location = redirect.headers.get("location", "")
            results.append(
                _check(
                    "http redirects to https",
                    redirect.is_redirect and location.startswith("https://"),
                    f"{redirect.status_code} location={location!r}",
                )
            )
        except httpx.HTTPError as exc:
            results.append(CheckResult("http redirects to https", False, f"request failed: {exc}"))

    login = _status(client, "GET", f"{base}/login")
    results.append(_check("login page", login == 200, f"got {login}"))

    anonymous = _status(client, "GET", f"{base}/api/v1/inbox")
    results.append(_check("API refuses a missing token", anonymous == 401, f"got {anonymous}"))
    authorized = _status(
        client, "GET", f"{base}/api/v1/inbox", headers={"Authorization": f"Bearer {api_token}"}
    )
    results.append(_check("API accepts ORBIT_API_TOKEN", authorized == 200, f"got {authorized}"))

    try:
        feed = client.get(f"{base}/calendar/{ics_token}.ics")
        results.append(
            _check(
                "ICS feed",
                feed.status_code == 200
                and feed.headers.get("content-type", "").startswith("text/calendar")
                and feed.text.startswith("BEGIN:VCALENDAR"),
                f"got {feed.status_code} {feed.headers.get('content-type', '')}",
            )
        )
    except httpx.HTTPError as exc:
        results.append(CheckResult("ICS feed", False, f"request failed: {exc}"))

    action = _status(client, "POST", f"{base}/a/not-a-valid-token")
    results.append(_check("push actions refuse a bad token", action == 403, f"got {action}"))
    return results
