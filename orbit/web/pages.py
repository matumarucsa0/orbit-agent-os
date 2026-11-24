from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import groupby
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from orbit.deps import DbSession, SettingsDep
from orbit.domain.enums import PromiseDirection
from orbit.services import (
    cadence,
    export,
    goals,
    ledger,
    meetings,
    people,
    reminders,
    review,
    school,
)
from orbit.services.dates import UpcomingDate, upcoming
from orbit.services.errors import InvalidInputError
from orbit.settings import Settings
from orbit.web.auth import require_login
from orbit.web.templating import flash, format_date, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

UPCOMING_DAYS = 7
HOME_GOALS = 3  # top goals on Home; the rest are on /goals
HOME_ATTENTION = 5  # "Needs attention" rows on Home in total; the rest are on /attention
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def day_label(day: date, today: date) -> str:
    offset = (day - today).days
    if offset == 0:
        return "Today"
    if offset == 1:
        return "Tomorrow"
    return f"{_WEEKDAYS[day.weekday()]} {format_date(day).rsplit(' ', 1)[0]}"


def _attention(
    db: Session, settings: Settings, today: date
) -> tuple[list[Any], list[Any], list[Any]]:
    """Promises due or overdue, lent items to ask back, catch-ups: most urgent first."""
    promises = reminders.due_promises(db, today) + [
        p
        for p in reminders.overdue_promises(db, today)
        if p.direction is PromiseDirection.THEY_PROMISED
    ]
    promises.sort(key=lambda p: (p.due or today, p.id))
    items = reminders.due_items(db, today, settings.item_reminder_days, settings.timezone)
    return promises, items, cadence.due(db, today, settings.timezone)


def _cap(
    promises: list[Any], items: list[Any], catch_ups: list[Any], limit: int
) -> tuple[list[Any], list[Any], list[Any], int]:
    """The first `limit` rows across the three kinds, and how many were left out."""
    shown_promises = promises[:limit]
    shown_items = items[: limit - len(shown_promises)]
    shown_catch_ups = catch_ups[: limit - len(shown_promises) - len(shown_items)]
    total = len(promises) + len(items) + len(catch_ups)
    shown = len(shown_promises) + len(shown_items) + len(shown_catch_ups)
    return shown_promises, shown_items, shown_catch_ups, total - shown


@router.get("/attention")
def attention(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    today = today_local(request)
    promises, items, catch_ups = _attention(db, settings, today)
    return templates.TemplateResponse(
        request,
        "attention.html",
        {"today": today, "promises": promises, "items": items, "catch_ups": catch_ups},
    )


@router.get("/")
def home(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    today = today_local(request)
    items = upcoming(db, today, UPCOMING_DAYS)
    groups: list[tuple[str, list[UpcomingDate]]] = [
        (day_label(day, today), list(entries))
        for day, entries in groupby(items, key=lambda u: u.date)
    ]
    feed_url = f"{settings.orbit_url}/calendar/{settings.ics_token.get_secret_value()}.ics"
    now = datetime.now(UTC)
    midnight = datetime.combine(today + timedelta(days=1), time(), tzinfo=settings.timezone)
    promises, items, catch_ups, attention_more = _cap(
        *_attention(db, settings, today), HOME_ATTENTION
    )
    active_goals = goals.active_goals(db)
    strip = school.home_strip(db, now)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "groups": groups,
            "feed_url": feed_url,
            "today": today,
            "weekday": _WEEKDAYS[today.weekday()],
            "promises": promises,
            "items": items,
            "catch_ups": catch_ups,
            "attention_more": attention_more,
            "totals": ledger.totals(ledger.balances(db)),
            "top_goals": active_goals[:HOME_GOALS],
            "review_days": goals.review_overdue_days(db, now),
            "latest_review": review.latest_ready(db),
            # What's left of today: meetings still going on or yet to start.
            "meetings": meetings.list_meetings(db, now, midnight.astimezone(UTC)),
            "school": strip,
            "school_days": (
                school.days_until(strip.next_item.start_utc, now, settings.timezone)
                if strip and strip.next_item
                else None
            ),
        },
    )


@router.get("/more")
def more(request: Request) -> Response:
    """The links that do not fit the tab bar (a phone's "More" tab, SPEC §2.24)."""
    return templates.TemplateResponse(request, "more.html", {})


@router.get("/keep-in-touch")
def keep_in_touch(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    today = today_local(request)
    return templates.TemplateResponse(
        request,
        "keep_in_touch.html",
        {
            "today": today,
            "due": cadence.due(db, today, settings.timezone),
            "tags": people.list_tags(db),
        },
    )


@router.post("/tags/{tag_id:int}/cadence")
def tag_cadence(
    request: Request, db: DbSession, tag_id: int, days: Annotated[str, Form()] = ""
) -> Response:
    try:
        value = int(days) if days.strip() else None
    except ValueError:
        value = 0  # rejected below with the same message as an out-of-range number
    try:
        tag = cadence.set_tag_cadence(db, tag_id, value)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/keep-in-touch#tags", status_code=303)
    db.commit()
    flash(
        request,
        f"#{tag.name}: every {tag.cadence_days} days."
        if tag.cadence_days
        else f"#{tag.name}: no keep-in-touch cadence.",
    )
    return RedirectResponse("/keep-in-touch#tags", status_code=303)


@router.get("/export.json")
def export_all(request: Request, db: DbSession) -> Response:
    """Everything in the database as one JSON file (see services/export.py)."""
    body = export.to_json(export.export_data(db))
    filename = f"orbit-export-{today_local(request).isoformat()}.json"
    return Response(
        body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
