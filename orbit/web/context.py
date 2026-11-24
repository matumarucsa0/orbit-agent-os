"""The context debug view: exactly what an agent would receive, for a window and a focus."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from orbit.deps import DbSession, SettingsDep
from orbit.domain.people import Person
from orbit.services.context import (
    DEFAULT_PRESET,
    PRESETS,
    ContextFocus,
    build_context,
    resolve_window,
)
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

MODULES = ("goals", "school", "people", "ledger", "meetings", "promises")  # as in the snapshot
PRESET_LABELS = {"this_week": "This week (Mon to Sun)", "next_7_days": "Next 7 days"}


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise InvalidInputError(f"Not a date: {value}") from None


@router.get("/context")
def context_page(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    window: Annotated[str, Query()] = DEFAULT_PRESET,
    start: Annotated[str, Query()] = "",
    end: Annotated[str, Query()] = "",
    person: Annotated[str, Query()] = "",
) -> Response:
    tz = settings.timezone
    today = today_local(request)
    custom = window == "custom"
    snapshot = None
    try:
        if custom:
            resolved = resolve_window(today, tz, None, _date(start), _date(end))
        else:
            resolved = resolve_window(today, tz, window)
        focus = ContextFocus(person_id=int(person)) if person.isdigit() else None
        snapshot = build_context(db, resolved, focus, budget_chars=settings.context_budget_chars)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
    everyone = list(
        db.scalars(
            select(Person).where(Person.archived.is_(False)).order_by(Person.search_name_norm)
        )
    )
    modules = [(name, getattr(snapshot, name)) for name in MODULES] if snapshot else []
    return templates.TemplateResponse(
        request,
        "context.html",
        {
            "snapshot": snapshot,
            "modules": [(n, m, m.model_dump_json(indent=2)) for n, m in modules],
            "raw": snapshot.model_dump_json() if snapshot else "",
            "presets": [(p, PRESET_LABELS[p]) for p in PRESETS],
            "window": window,
            "start": start,
            "end": end,
            "person": person,
            "everyone": everyone,
        },
    )
