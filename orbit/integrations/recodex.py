"""ReCodEx (the Uni code-assignment evaluator): the iCal deadline feed and the optional read-only
API (SPEC §2.20 "Sources beyond SIS"). GET only, through the same polite client as SIS.

- Feed `users/ical/<RECODEX_ICAL_TOKEN>`: the first deadline of every assignment. Its times are
  floating but really UTC (verified against the API). An expired token answers HTTP 400.
- API with a `read-all` token (`RECODEX_API_TOKEN`, a JWT): `users/<me>/groups` (groups and the
  points stats in one call) and `groups/<id>/assignments` (both deadlines and max points). The
  user id is the JWT `sub`; the JWT is decoded only to read `sub` and `exp`, never verified.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import quote

from orbit.domain.enums import FetchStatus
from orbit.integrations.sis import (
    FeedFetch,
    PoliteClient,
    SisError,
    calendar_result,
    ticket_course_code,
)

SUMMARY_PREFIX = "ReCodEx deadline:"
_TICKET_RE = re.compile(r"`(\d{2}[ab][A-Z]{3,5}\d{3}[A-Z]?[px]\d+)`")
_CODE_RE = re.compile(r"`([A-Z]{3,5}\d{3}[A-Z]?)`")


class RecodexDenied(SisError):
    """The token was rejected or has expired."""


def clean_title(summary: str | None) -> str:
    """ "ReCodEx deadline: 5P  0.1 Hello World" -> "5P 0.1 Hello World"."""
    text = (summary or "").strip()
    if text.startswith(SUMMARY_PREFIX):
        text = text[len(SUMMARY_PREFIX) :]
    return " ".join(text.split()) or "(assignment)"


def assignment_id(uid: str) -> str | None:
    """The assignment id of a feed UID "recodex/<user>/<assignment>"."""
    parts = uid.split("/")
    return parts[2] if len(parts) == 3 and parts[0] == "recodex" else None


@dataclass(frozen=True)
class TokenClaims:
    user_id: str | None
    expires: datetime | None


def token_claims(token: str) -> TokenClaims:
    """`sub` and `exp` of a JWT, unverified; empty when it isn't a JWT."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return TokenClaims(None, None)
    exp = data.get("exp") if isinstance(data, dict) else None
    sub = data.get("sub") if isinstance(data, dict) else None
    return TokenClaims(
        str(sub) if sub else None,
        datetime.fromtimestamp(exp, UTC) if isinstance(exp, int | float) else None,
    )


# --- API shapes ---


@dataclass(frozen=True)
class GroupStats:
    gained: Decimal
    total: Decimal
    passes: bool | None
    points: dict[str, Decimal]  # assignment id -> points gained


@dataclass(frozen=True)
class RecodexGroup:
    id: str
    name: str  # Czech name if there is one ("Programování 1 (Čt, 9:50, N8)")
    names: tuple[str, ...]  # every localized name (for matching feed LOCATION)
    ticket: str | None
    course_code: str | None
    threshold: Decimal | None  # share of points needed (0.7)
    points_limit: Decimal | None  # absolute points needed
    assignments: tuple[str, ...]
    stats: GroupStats | None


@dataclass(frozen=True)
class RecodexAssignment:
    id: str
    group_id: str
    name: str
    first_deadline: datetime | None
    second_deadline: datetime | None
    max_points: Decimal | None
    is_bonus: bool


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _payload(data: Any) -> Any:
    if not isinstance(data, dict) or not data.get("success", True):
        raise SisError("unexpected ReCodEx answer")
    return data.get("payload")


def _texts(item: dict[str, Any]) -> list[dict[str, Any]]:
    texts = item.get("localizedTexts") or []
    return sorted((t for t in texts if isinstance(t, dict)), key=lambda t: t.get("locale") != "cs")


def parse_groups(data: Any) -> list[RecodexGroup]:
    """The student groups of `users/<me>/groups`, with their points stats."""
    payload = _payload(data)
    if not isinstance(payload, dict):
        raise SisError("unexpected ReCodEx groups answer")
    stats: dict[str, GroupStats] = {}
    for row in payload.get("stats") or []:
        points = row.get("points") or {}
        stats[str(row.get("groupId"))] = GroupStats(
            gained=_decimal(points.get("gained")) or Decimal(0),
            total=_decimal(points.get("total")) or Decimal(0),
            passes=row.get("passesLimit") if isinstance(row.get("passesLimit"), bool) else None,
            points={
                str(a.get("id")): _decimal((a.get("points") or {}).get("gained")) or Decimal(0)
                for a in row.get("assignments") or []
            },
        )
    groups = []
    for item in payload.get("student") or []:
        if item.get("archived") or item.get("organizational"):
            continue
        texts = _texts(item)
        descriptions = " ".join(str(t.get("description") or "") for t in texts)
        ticket = _TICKET_RE.search(descriptions)
        code = _CODE_RE.search(descriptions)
        private = item.get("privateData") or {}
        names = tuple(str(t.get("name")) for t in texts if t.get("name"))
        groups.append(
            RecodexGroup(
                id=str(item.get("id")),
                name=names[0] if names else "",
                names=names,
                ticket=ticket.group(1) if ticket else None,
                course_code=code.group(1)
                if code
                else ticket_course_code(ticket.group(1) if ticket else None),
                threshold=_decimal(private.get("threshold")),
                points_limit=_decimal(private.get("pointsLimit")),
                assignments=tuple(str(a) for a in private.get("assignments") or []),
                stats=stats.get(str(item.get("id"))),
            )
        )
    return groups


def _time(value: Any) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if isinstance(value, int) and value > 0 else None


def parse_assignments(data: Any) -> list[RecodexAssignment]:
    payload = _payload(data)
    if not isinstance(payload, list):
        raise SisError("unexpected ReCodEx assignments answer")
    found = []
    for item in payload:
        texts = _texts(item)
        second = _time(item.get("secondDeadline")) if item.get("allowSecondDeadline") else None
        found.append(
            RecodexAssignment(
                id=str(item.get("id")),
                group_id=str(item.get("groupId")),
                name=" ".join(str(texts[0].get("name") or "").split()) if texts else "",
                first_deadline=_time(item.get("firstDeadline")),
                second_deadline=second,
                max_points=_decimal(item.get("maxPointsBeforeFirstDeadline")),
                is_bonus=bool(item.get("isBonus")),
            )
        )
    return found


# --- The source ---


class RecodexSource(Protocol):
    @property
    def has_feed(self) -> bool: ...

    @property
    def claims(self) -> TokenClaims | None:
        """The API token's `sub` and `exp`; None without an API token."""
        ...

    def fetch_feed(self) -> FeedFetch: ...

    def assignment_url(self, assignment: str) -> str:
        """The assignment's page in the ReCodEx web app (no token in it)."""
        ...

    def fetch_groups(self) -> list[RecodexGroup]:
        """Raises RecodexDenied or SisError."""
        ...

    def fetch_assignments(self, group_id: str) -> list[RecodexAssignment]:
        """Raises RecodexDenied or SisError."""
        ...


class RecodexHttpSource:
    def __init__(
        self,
        client: PoliteClient,
        base_url: str,
        *,
        ical_token: str | None,
        api_token: str | None,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._ical = ical_token
        self._api = api_token
        self._claims = token_claims(api_token) if api_token else None

    @property
    def has_feed(self) -> bool:
        return bool(self._ical)

    @property
    def claims(self) -> TokenClaims | None:
        return self._claims

    def assignment_url(self, assignment: str) -> str:
        app = self._base.removesuffix("/api/v1")
        return f"{app}/app/assignment/{quote(assignment, safe='')}"

    def fetch_feed(self) -> FeedFetch:
        if not self._ical:
            return FeedFetch(FetchStatus.ERROR, error="RECODEX_ICAL_TOKEN isn't set")
        try:
            result = self._client.get(f"{self._base}/users/ical/{quote(self._ical, safe='')}")
        except SisError as exc:
            return FeedFetch(FetchStatus.ERROR, error=str(exc))
        if result.status_code == 400:
            return FeedFetch(FetchStatus.DENIED, error="HTTP 400 (the iCal token has expired)")
        return calendar_result(result)

    def _json(self, path: str) -> Any:
        if not self._api:
            raise SisError("RECODEX_API_TOKEN isn't set")
        result = self._client.get(
            f"{self._base}/{path}", headers={"Authorization": f"Bearer {self._api}"}
        )
        if result.status_code in (401, 403):
            raise RecodexDenied(f"HTTP {result.status_code}")
        if result.status_code != 200:
            raise SisError(f"HTTP {result.status_code}")
        try:
            return json.loads(result.body)
        except ValueError as exc:
            raise SisError("not JSON") from exc

    def fetch_groups(self) -> list[RecodexGroup]:
        user = self._claims.user_id if self._claims else None
        if not user:
            raise RecodexDenied("RECODEX_API_TOKEN isn't a ReCodEx token (no user id)")
        return parse_groups(self._json(f"users/{quote(user, safe='')}/groups"))

    def fetch_assignments(self, group_id: str) -> list[RecodexAssignment]:
        return parse_assignments(self._json(f"groups/{quote(group_id, safe='')}/assignments"))
