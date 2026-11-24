"""Bearer-token auth for the JSON API (used by iOS Shortcuts)."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orbit.api.context import get_context
from orbit.deps import SettingsDep

_bearer = HTTPBearer(auto_error=False)


def _accepts_mcp_token(request: Request) -> bool:
    """True only for `GET /api/v1/context`, judged by the matched endpoint function, not the URL."""
    route = request.scope.get("route")
    return request.method == "GET" and getattr(route, "endpoint", None) is get_context


def require_api_token(
    request: Request,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """The API token, on every route. `GET /api/v1/context` also accepts ORBIT_MCP_TOKEN, a
    read-only token for the MCP server (docs/MCP.md); no other route does."""
    allowed = [settings.orbit_api_token.get_secret_value()]
    if settings.orbit_mcp_token is not None and _accepts_mcp_token(request):
        allowed.append(settings.orbit_mcp_token.get_secret_value())
    given = credentials.credentials.encode() if credentials is not None else b""
    # Compare against every allowed token (no early exit on the first match).
    matches = [secrets.compare_digest(given, token.encode()) for token in allowed]
    if credentials is None or not any(matches):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
