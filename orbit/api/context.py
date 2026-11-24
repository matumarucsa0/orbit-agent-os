from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter

from orbit.deps import DbSession, SettingsDep
from orbit.services.context import ContextFocus, ContextSnapshot, build_context, resolve_window

router = APIRouter(tags=["context"])


@router.get("/context", response_model=ContextSnapshot)
def get_context(
    db: DbSession,
    settings: SettingsDep,
    preset: str | None = None,
    start: date | None = None,
    end: date | None = None,
    person_id: int | None = None,
) -> ContextSnapshot:
    """What an agent would read: people, ledger, meetings and promises for a window (default
    `this_week`; `next_7_days`, or `start` + `end`), optionally about one person."""
    tz = settings.timezone
    today = datetime.now(UTC).astimezone(tz).date()
    window = resolve_window(today, tz, preset, start, end)
    focus = ContextFocus(person_id=person_id) if person_id is not None else None
    return build_context(db, window, focus, budget_chars=settings.context_budget_chars)
