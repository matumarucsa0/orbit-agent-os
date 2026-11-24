from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from orbit.domain.enums import HandleType, InteractionKind
from orbit.domain.money import format_amount, format_plain
from orbit.services.contact_links import links_for
from orbit.services.inbox import pending_count

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"


def _inbox_count(request: Request) -> dict[str, Any]:
    """Pending Inbox items for the nav badge (logged-in pages only)."""
    if "session" not in request.scope or not request.session.get("auth"):
        return {}
    # Its own short session: the endpoint's session is already closed when the page renders, and
    # using it again would leave a connection idle in a transaction (it blocks DDL on Postgres).
    with request.app.state.sessionmaker() as session:
        return {"inbox_count": pending_count(session)}


templates = Jinja2Templates(directory=WEB_DIR / "templates", context_processors=[_inbox_count])

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_date(value: date | None) -> str:
    if value is None:
        return ""
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def format_birthday(month: int | None, day: int | None, year: int | None = None) -> str:
    if month is None or day is None:
        return ""
    text = f"{day} {_MONTHS[month - 1]}"
    return f"{text} {year}" if year else text


@pass_context
def local_datetime(context: Any, value: datetime | None) -> str:
    """UTC datetime -> "3 Sep 2026 18:05" in the configured timezone."""
    if value is None:
        return ""
    request: Request = context["request"]
    local = value.astimezone(request.app.state.settings.timezone)
    return f"{format_date(local.date())} {local:%H:%M}"


@pass_context
def local_date(context: Any, value: datetime | None) -> str:
    """UTC datetime -> "3 Sep 2026", the day it was in the configured timezone."""
    if value is None:
        return ""
    request: Request = context["request"]
    return format_date(value.astimezone(request.app.state.settings.timezone).date())


@pass_context
def local_time(context: Any, value: datetime | None) -> str:
    """UTC datetime -> "18:05" in the configured timezone."""
    if value is None:
        return ""
    request: Request = context["request"]
    return f"{value.astimezone(request.app.state.settings.timezone):%H:%M}"


def today_local(request: Request) -> date:
    return datetime.now(UTC).astimezone(request.app.state.settings.timezone).date()


def flash(
    request: Request,
    message: str,
    kind: str = "info",
    link: str | None = None,
    link_label: str | None = None,
    action: str | None = None,
    action_label: str = "Undo",
    fields: dict[str, str] | None = None,
) -> None:
    """Queue a one-time message shown on the next rendered page. `action` adds a button that
    POSTs `fields` there (e.g. Undo), so a reversible change needs no "Are you sure?" first."""
    messages = list(request.session.get("flash", []))
    messages.append(
        {
            "message": message,
            "kind": kind,
            "link": link,
            "link_label": link_label,
            "action": action,
            "action_label": action_label,
            "fields": fields or {},
        }
    )
    request.session["flash"] = messages


def full_name(first: str | None, last: str | None, display_name: str) -> str:
    """First + last name, or "" when that just repeats the display name."""
    name = " ".join(part for part in (first, last) if part)
    return "" if name.casefold() == display_name.casefold() else name


def _hash_static_files() -> dict[str, str]:
    """A short content hash per static file, computed once at startup."""
    return {
        path.relative_to(STATIC_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        for path in STATIC_DIR.rglob("*")
        if path.is_file()
    }


_STATIC_HASHES = _hash_static_files()


def static_url(path: str) -> str:
    """`/static/<path>?v=<hash>`: the URL changes with the file, so it can be cached for good."""
    version = _STATIC_HASHES.get(path)
    return f"/static/{path}?v={version}" if version else f"/static/{path}"


# The "More" menu (SPEC §2.24): (href, label, path prefixes that mark it current), most used first.
MORE_LINKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("/school", "School", ("/school",)),
    ("/goals", "Goals", ("/goals",)),
    ("/fitness", "Fitness", ("/fitness",)),
    ("/health", "Health", ("/health",)),
    ("/reviews", "Review", ("/reviews",)),
    ("/reports", "Reports", ("/reports",)),
    ("/import", "Import", ("/import", "/transactions")),
    ("/settings/schedule", "Schedule", ("/settings",)),
    ("/context", "Context", ("/context",)),
    ("/ops/claude", "Claude", ("/ops",)),
)


def more_links(request: Request) -> list[dict[str, Any]]:
    """The More links for this request: Claude only with the worker backend; `current` set."""
    path = request.url.path
    worker = request.app.state.settings.resolved_claude_backend in ("worker", "fake")
    return [
        {"href": href, "label": label, "current": path.startswith(prefixes)}
        for href, label, prefixes in MORE_LINKS
        if worker or href != "/ops/claude"
    ]


templates.env.globals["more_links"] = more_links
templates.env.globals["static_url"] = static_url
templates.env.filters["date"] = format_date
templates.env.filters["local_dt"] = local_datetime
templates.env.filters["local_date"] = local_date
templates.env.filters["local_time"] = local_time
templates.env.globals["money"] = format_amount
templates.env.globals["amount_input"] = lambda minor, currency: (
    format_plain(minor, currency) if minor is not None else ""
)
templates.env.globals["format_birthday"] = format_birthday
templates.env.globals["full_name"] = full_name
templates.env.globals["contact_links"] = lambda handle: links_for(handle.type, handle.value)
templates.env.globals["handle_types"] = list(HandleType)
templates.env.globals["interaction_kinds"] = list(InteractionKind)
