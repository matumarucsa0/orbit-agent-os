"""People pages: list/search, add, detail (fields, handles, aliases, tags, timeline), import."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from orbit.deps import DbSession, MessageDrafterDep, SettingsDep
from orbit.domain.enums import DebtDirection, DebtType, HandleType, InteractionKind, PromiseStatus
from orbit.domain.people import Person
from orbit.integrations import namedays
from orbit.services import (
    cadence,
    drafts,
    duplicates,
    goals,
    interactions,
    ledger,
    meetings,
    messages,
    people,
    promises,
)
from orbit.services.dates import nameday_name, person_namedays, person_upcoming
from orbit.services.errors import HandleConflictError, InvalidInputError
from orbit.services.search import search_people
from orbit.web.auth import require_login, safe_next
from orbit.web.templating import flash, format_birthday, format_date, templates, today_local

router = APIRouter(dependencies=[Depends(require_login)])

PAGE_SIZE = 100
NAMEDAY_SEARCH_LIMIT = 12
IMPORT_COUNTRY = "SK"
DRAFT_DAYS_AHEAD = 14  # offer a birthday / name-day message this long before the day


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _person_url(person_id: int, section: str = "") -> str:
    """The person page, scrolled to `section` (an element id) when given."""
    return f"/people/{person_id}" + (f"#{section}" if section else "")


def _back(next_url: str, fallback: str) -> RedirectResponse:
    return _redirect(safe_next(next_url) if next_url else fallback)


# --- List, search, add ---


@router.get("/people")
def people_list(
    request: Request, db: DbSession, q: str = "", tag: str = "", offset: int = 0
) -> Response:
    found = search_people(db, q, tag=tag or None, limit=PAGE_SIZE + 1, offset=offset)
    context: dict[str, Any] = {
        "people": found[:PAGE_SIZE],
        "has_more": len(found) > PAGE_SIZE,
        "next_offset": offset + PAGE_SIZE,
        "q": q,
        "tag": tag,
    }
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "results":
        return templates.TemplateResponse(request, "people/_results.html", context)
    context["tags"] = people.list_tags(db)
    context["duplicate_count"] = len(duplicates.find_candidates(db))
    return templates.TemplateResponse(request, "people/list.html", context)


@router.post("/people")
def people_create(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()],
    handle_type: Annotated[str, Form()] = "",
    handle_value: Annotated[str, Form()] = "",
) -> Response:
    name = name.strip()
    # "@someone" alone creates an Instagram-only person named "someone".
    if name.startswith("@") and " " not in name and len(name) > 1 and not handle_value:
        handle_type, handle_value, name = HandleType.INSTAGRAM.value, name, name[1:]
    handle = None
    if handle_value.strip():
        try:
            handle = (HandleType(handle_type or HandleType.OTHER.value), handle_value.strip())
        except ValueError:
            flash(request, "Unknown handle type.", "error")
            return _redirect("/people")
        owner = people.find_person_by_handle(db, handle[0], handle[1], IMPORT_COUNTRY)
        if owner is not None:
            flash(request, f"{handle[1]} already belongs to {owner.display_name}.", "error")
            return _redirect(_person_url(owner.id))
    try:
        person = people.create_person(db, people.PersonCreate(display_name=name))
        if handle is not None:
            people.add_handle(db, person, handle[0], handle[1])
    except ValidationError:
        db.rollback()
        flash(request, "A person needs a name.", "error")
        return _redirect("/people")
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect("/people")
    db.commit()
    flash(request, f"Added {person.display_name}.")
    return _redirect(_person_url(person.id))


# --- Detail ---


def _timeline(request: Request, person: Person) -> list[dict[str, Any]]:
    tz = request.app.state.settings.timezone
    entries: list[dict[str, Any]] = [
        {"type": "note", "item": n, "sort": n.created_at.astimezone(tz).replace(tzinfo=None)}
        for n in person.notes
    ]
    entries += [
        {"type": "interaction", "item": i, "sort": datetime.combine(i.occurred_on, time(12))}
        for i in person.interactions
    ]
    entries.sort(key=lambda e: e["sort"], reverse=True)
    return entries


@router.get("/people/{person_id:int}")
def person_detail(
    request: Request, db: DbSession, settings: SettingsDep, person_id: int
) -> Response:
    person = people.get_person(db, person_id)
    last = interactions.last_interaction(db, person.id)
    now = datetime.now(UTC)
    today = today_local(request)
    open_debts = ledger.list_debts(db, person_id=person.id)
    # Birthday / name day coming up (or just past): offer a message draft.
    soon = person_upcoming(
        person,
        today - timedelta(days=drafts.LOOK_BACK_DAYS),
        DRAFT_DAYS_AHEAD + drafts.LOOK_BACK_DAYS,
    )
    draft_occasions = list(dict.fromkeys(u.kind for u in soon))
    return templates.TemplateResponse(
        request,
        "people/detail.html",
        {
            "person": person,
            "last": last,
            "keep_in_touch": cadence.status(
                person, last.occurred_on if last else None, settings.timezone
            ),
            "payment_requests": messages.payment_requests(
                person, open_debts, iban=settings.payment_iban, holder=settings.payment_name
            ),
            "ask_back": {
                d.id: messages.ask_back(
                    person, d, d.created_at.astimezone(settings.timezone).date()
                )
                for d in open_debts
                if d.type is DebtType.ITEM and d.direction is DebtDirection.THEY_OWE_ME
            },
            "draft_occasions": draft_occasions,
            "namedays": [format_birthday(m, d) for m, d in person_namedays(person)],
            "nameday_source": nameday_name(person),
            "timeline": _timeline(request, person),
            "all_tags": [t for t, _ in people.list_tags(db)],
            "today": today,
            "balances": ledger.balances(db, person.id),
            "open_debts": open_debts,
            "settled_debts": ledger.list_debts(db, person_id=person.id, is_open=False),
            "open_promises": promises.list_promises(db, person_id=person.id),
            "active_goals": goals.active_goals(db),
            "person_goals": goals.goals_of_person(db, person.id),
            "closed_promises": [
                p
                for p in promises.list_promises(db, person_id=person.id, status=None)
                if p.status is not PromiseStatus.OPEN
            ],
            "default_currency": request.app.state.settings.default_currency,
            "meetings": meetings.list_meetings(
                db, now, now + timedelta(days=meetings.SYNC_DAYS), person_id=person.id
            ),
        },
    )


@router.get("/people/{person_id:int}/draft")
def person_draft(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    drafter: MessageDrafterDep,
    person_id: int,
    occasion: drafts.Occasion = "birthday",
) -> Response:
    """A birthday / name-day message to copy or share (Claude's draft, else a plain wish)."""
    person = people.get_person(db, person_id)
    try:
        draft = drafts.draft(
            db, person, occasion, today_local(request), drafter, settings.message_style
        )
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id))
    return templates.TemplateResponse(
        request,
        "people/draft.html",
        {"person": person, "occasion": occasion, "draft": draft, "claude": drafter is not None},
    )


@router.get("/people/{person_id:int}/nameday-search")
def nameday_search(request: Request, db: DbSession, person_id: int, q: str = "") -> Response:
    """HTMX fragment: calendar names matching `q`, to link as the person's name-day name."""
    person = people.get_person(db, person_id)
    countries = [person.nameday_country] if person.nameday_country else list(namedays.COUNTRIES)
    results = [
        {
            "name": name,
            "country": country,
            "dates": ", ".join(format_birthday(m, d) for m, d in namedays.dates_for(name, country)),
        }
        for country in countries
        for name in namedays.get_calendar(country).search(q, limit=NAMEDAY_SEARCH_LIMIT)
    ]
    return templates.TemplateResponse(
        request, "people/_nameday_results.html", {"person": person, "results": results, "q": q}
    )


@router.post("/people/{person_id:int}/nameday")
def nameday_assign(
    request: Request,
    db: DbSession,
    person_id: int,
    name: Annotated[str, Form()],
    country: Annotated[str, Form()] = "",
) -> Response:
    person = people.get_person(db, person_id)
    fields: dict[str, Any] = {"nameday_name": name}
    if person.nameday_country is None and country:
        fields["nameday_country"] = country  # picked from the SK/CZ search
    try:
        people.update_person(db, person, people.PersonUpdate(**fields))
    except (InvalidInputError, ValidationError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, InvalidInputError) else "Unknown name-day country."
        flash(request, message, "error")
        return _redirect(_person_url(person_id, "nameday"))
    db.commit()
    flash(request, f"Name day linked to {person.nameday_name}.")
    return _redirect(_person_url(person_id, "nameday"))


@router.post("/people/{person_id:int}/nameday/clear")
def nameday_clear(request: Request, db: DbSession, person_id: int) -> Response:
    people.update_person(
        db, people.get_person(db, person_id), people.PersonUpdate(nameday_name=None)
    )
    db.commit()
    flash(request, "Name day matched from the first name again.")
    return _redirect(_person_url(person_id, "nameday"))


@router.post("/people/{person_id:int}/edit")
def person_edit(
    request: Request,
    db: DbSession,
    person_id: int,
    data: Annotated[people.PersonUpdate, Form()],
) -> Response:
    person = people.get_person(db, person_id)
    try:
        people.update_person(db, person, data)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id))
    db.commit()
    flash(request, "Saved.")
    return _redirect(_person_url(person_id))


@router.post("/people/{person_id:int}/archive")
def person_archive(request: Request, db: DbSession, person_id: int) -> Response:
    person = people.get_person(db, person_id)
    people.update_person(db, person, people.PersonUpdate(archived=not person.archived))
    db.commit()
    flash(request, "Archived." if person.archived else "Restored from archive.")
    return _redirect(_person_url(person_id))


@router.post("/people/{person_id:int}/delete")
def person_delete(request: Request, db: DbSession, person_id: int) -> Response:
    person = people.get_person(db, person_id)
    name = person.display_name
    try:
        people.delete_person(db, person)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id))
    db.commit()
    flash(request, f"Deleted {name}.")
    return _redirect("/people")


@router.post("/people/{person_id:int}/handles")
def handle_add(
    request: Request,
    db: DbSession,
    person_id: int,
    type: Annotated[HandleType, Form()],
    value: Annotated[str, Form()],
    label: Annotated[str, Form()] = "",
) -> Response:
    person = people.get_person(db, person_id)
    try:
        people.add_handle(db, person, type, value, label or None)
    except HandleConflictError as exc:
        db.rollback()
        flash(
            request,
            f"{value} already belongs to {exc.owner_name}.",
            "error",
            link=f"/people/{person_id}/merge?with={exc.owner_id}",
            link_label=f"Merge with {exc.owner_name}",
        )
        return _redirect(_person_url(person_id, "handles"))
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id, "handles"))
    db.commit()
    return _redirect(_person_url(person_id, "handles"))


@router.post("/people/{person_id:int}/handles/{handle_id:int}/delete")
def handle_remove(request: Request, db: DbSession, person_id: int, handle_id: int) -> Response:
    person = people.get_person(db, person_id)
    handle = next((h for h in person.handles if h.id == handle_id), None)
    people.remove_handle(db, person, handle_id)
    db.commit()
    if handle is not None:
        flash(
            request,
            f"Removed {handle.value}.",
            action=f"/people/{person_id}/handles",
            fields={"type": handle.type.value, "value": handle.value, "label": handle.label or ""},
        )
    return _redirect(_person_url(person_id, "handles"))


@router.post("/people/{person_id:int}/aliases")
def alias_add(
    request: Request, db: DbSession, person_id: int, value: Annotated[str, Form()]
) -> Response:
    try:
        people.add_alias(db, people.get_person(db, person_id), value)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id, "tags"))
    db.commit()
    return _redirect(_person_url(person_id, "tags"))


@router.post("/people/{person_id:int}/aliases/{alias_id:int}/delete")
def alias_remove(request: Request, db: DbSession, person_id: int, alias_id: int) -> Response:
    person = people.get_person(db, person_id)
    alias = next((a for a in person.aliases if a.id == alias_id), None)
    people.remove_alias(db, person, alias_id)
    db.commit()
    if alias is not None:
        flash(
            request,
            f"Removed alias {alias.value}.",
            action=f"/people/{person_id}/aliases",
            fields={"value": alias.value},
        )
    return _redirect(_person_url(person_id, "tags"))


@router.post("/people/{person_id:int}/tags")
def tag_add(
    request: Request, db: DbSession, person_id: int, name: Annotated[str, Form()]
) -> Response:
    try:
        people.add_tag(db, people.get_person(db, person_id), name)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id, "tags"))
    db.commit()
    return _redirect(_person_url(person_id, "tags"))


@router.post("/people/{person_id:int}/tags/{tag_id:int}/delete")
def tag_remove(request: Request, db: DbSession, person_id: int, tag_id: int) -> Response:
    person = people.get_person(db, person_id)
    tag = next((t for t in person.tags if t.id == tag_id), None)
    people.remove_tag(db, person, tag_id)
    db.commit()
    if tag is not None:
        flash(
            request,
            f"Removed #{tag.name}.",
            action=f"/people/{person_id}/tags",
            fields={"name": tag.name},
        )
    return _redirect(_person_url(person_id, "tags"))


@router.post("/people/{person_id:int}/notes")
def note_add(
    request: Request, db: DbSession, person_id: int, text: Annotated[str, Form()]
) -> Response:
    try:
        people.add_note(db, people.get_person(db, person_id), text)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id, "timeline"))
    db.commit()
    return _redirect(_person_url(person_id, "timeline"))


@router.post("/people/{person_id:int}/notes/{note_id:int}/edit")
def note_edit(
    request: Request,
    db: DbSession,
    person_id: int,
    note_id: int,
    text: Annotated[str, Form()],
) -> Response:
    person = people.get_person(db, person_id)
    try:
        people.update_note(db, people.get_note(db, person, note_id), text)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(_person_url(person_id, "timeline"))
    db.commit()
    flash(request, "Note saved.")
    return _redirect(_person_url(person_id, f"note-{note_id}"))


@router.post("/people/{person_id:int}/notes/{note_id:int}/delete")
def note_remove(db: DbSession, person_id: int, note_id: int) -> Response:
    person = people.get_person(db, person_id)
    people.delete_note(db, people.get_note(db, person, note_id))
    db.commit()
    return _redirect(_person_url(person_id, "timeline"))


_KIND_PAST = {
    InteractionKind.MEET: "met",
    InteractionKind.CALL: "called",
    InteractionKind.MESSAGE: "messaged",
    InteractionKind.OTHER: "talked to",
}


@router.post("/people/{person_id:int}/interactions")
def interaction_add(
    request: Request,
    db: DbSession,
    person_id: int,
    kind: Annotated[InteractionKind, Form()],
    occurred_on: Annotated[date | None, Form()] = None,
    summary: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
) -> Response:
    """The Timeline form, or a one-tap "met / called / messaged today" (with `next`)."""
    person = people.get_person(db, person_id)
    today = today_local(request)
    interaction = interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=occurred_on or today, kind=kind, summary=summary, person_ids=[person_id]
        ),
    )
    db.commit()
    when = "today" if interaction.occurred_on == today else format_date(interaction.occurred_on)
    flash(
        request,
        f"Logged: {_KIND_PAST[kind]} {person.display_name} {when}.",
        action=f"/people/{person_id}/interactions/{interaction.id}/delete",
        fields={"next": next} if next else None,
    )
    return _back(next, _person_url(person_id, "timeline"))


@router.post("/people/{person_id:int}/interactions/{interaction_id:int}/edit")
def interaction_edit(
    request: Request,
    db: DbSession,
    person_id: int,
    interaction_id: int,
    occurred_on: Annotated[date, Form()],
    kind: Annotated[InteractionKind, Form()],
    summary: Annotated[str, Form()] = "",
) -> Response:
    interaction = interactions.get_interaction(db, interaction_id)
    interactions.update_interaction(
        db,
        interaction,
        interactions.InteractionUpdate(occurred_on=occurred_on, kind=kind, summary=summary),
    )
    db.commit()
    flash(request, "Interaction saved.")
    return _redirect(_person_url(person_id, f"interaction-{interaction_id}"))


@router.post("/people/{person_id:int}/interactions/{interaction_id:int}/delete")
def interaction_remove(
    request: Request,
    db: DbSession,
    person_id: int,
    interaction_id: int,
    next: Annotated[str, Form()] = "",
) -> Response:
    interaction = interactions.get_interaction(db, interaction_id)
    if len(interaction.people) > 1:
        # Shared with others: only unlink this person.
        interactions.update_interaction(
            db,
            interaction,
            interactions.InteractionUpdate(
                person_ids=[p.id for p in interaction.people if p.id != person_id]
            ),
        )
    else:
        interactions.delete_interaction(db, interaction)
    db.commit()
    flash(request, "Removed that interaction.")
    return _back(next, _person_url(person_id, "timeline"))


# --- Keep in touch ---


@router.post("/people/{person_id:int}/snooze")
def person_snooze(
    request: Request,
    db: DbSession,
    person_id: int,
    days: Annotated[int, Form()] = 7,
    next: Annotated[str, Form()] = "",
) -> Response:
    """Keep in touch: don't list them as due for `days` days."""
    person = people.get_person(db, person_id)
    previous = person.snoozed_until
    try:
        until = cadence.snooze(person, today_local(request), days)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return _back(next, _person_url(person_id))
    db.commit()
    fields = {"next": next} if next else {}
    if previous is not None:
        fields["until"] = previous.isoformat()
    flash(
        request,
        f"Snoozed {person.display_name} until {format_date(until)}.",
        action=f"/people/{person_id}/snooze/clear",
        fields=fields,
    )
    return _back(next, _person_url(person_id))


@router.post("/people/{person_id:int}/snooze/clear")
def person_snooze_clear(
    request: Request,
    db: DbSession,
    person_id: int,
    until: Annotated[date | None, Form()] = None,
    next: Annotated[str, Form()] = "",
) -> Response:
    """Undo a snooze (back to `until`, the snooze before it, if any)."""
    person = people.get_person(db, person_id)
    person.snoozed_until = until
    db.commit()
    flash(request, "Snooze undone.")
    return _back(next, _person_url(person_id))
