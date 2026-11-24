"""ICS feed for Apple Calendar: `/calendar/{ICS_TOKEN}.ics` (the token in the path is the auth)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from orbit.deps import DbSession, SettingsDep
from orbit.services.ics import build_calendar

router = APIRouter()


@router.get("/calendar/{token}.ics")
def calendar_feed(token: str, settings: SettingsDep, db: DbSession) -> Response:
    expected = settings.ics_token.get_secret_value().encode()
    if not secrets.compare_digest(token.encode(), expected):
        # 404 rather than 401/403: don't confirm that a feed lives here.
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return Response(
        build_calendar(db, settings.item_reminder_days, settings.timezone),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="orbit.ics"'},
    )
