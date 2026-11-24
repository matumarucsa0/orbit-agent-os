"""The health page: the last 30 days from the watch (SPEC §2.36). Read only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from orbit.deps import DbSession, SettingsDep
from orbit.services import health
from orbit.services.context.health import averages
from orbit.web.auth import require_login
from orbit.web.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])
DAYS = 30


def hours(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    return f"{minutes // 60} h {minutes % 60:02d} min"


templates.env.filters["sleep"] = hours


@router.get("/health")
def health_page(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    today = datetime.now(UTC).astimezone(settings.timezone).date()
    rows = health.list_days(db, today - timedelta(days=DAYS - 1), today)
    return templates.TemplateResponse(
        request,
        "health/list.html",
        {
            "rows": list(reversed(rows)),  # newest first
            "week": averages([r for r in rows if r.day > today - timedelta(days=7)], 7),
            "month": averages(rows, DAYS),
            "last_received": max((r.received_at for r in rows), default=None),
        },
    )
