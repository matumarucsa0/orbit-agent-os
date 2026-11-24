"""Contact sync review: Sync button, the diff screen, apply, undo."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from orbit.deps import ContactSourceDep, DbSession
from orbit.domain.contacts import SyncChange, SyncRun
from orbit.domain.enums import SyncDirection, SyncKind, SyncRunStatus, SyncSource, SyncState
from orbit.domain.normalize import normalize_text
from orbit.domain.people import Person
from orbit.integrations.carddav import CardDavError
from orbit.integrations.vcard import parse_vcards
from orbit.services import contact_sync as sync
from orbit.services.errors import InvalidInputError
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates

router = APIRouter(dependencies=[Depends(require_login)])

# (key, title, which changes belong): "Into Orbit" first, then what will be written to Apple.
SECTIONS: tuple[tuple[str, str, Callable[[SyncChange], bool]], ...] = (
    ("new", "New contacts", lambda c: c.kind is SyncKind.NEW_CONTACT),
    ("match", "Maybe someone you already have", lambda c: c.kind is SyncKind.POSSIBLE_MATCH),
    (
        "changes",
        "Into Orbit",
        lambda c: (
            c.kind in (SyncKind.AUTO, SyncKind.PROPOSAL) and c.direction is SyncDirection.PULL
        ),
    ),
    ("conflicts", "Different in both places", lambda c: c.kind is SyncKind.CONFLICT),
    (
        "apple",
        "Into Apple contacts",
        lambda c: (
            c.kind in (SyncKind.AUTO, SyncKind.PROPOSAL) and c.direction is SyncDirection.PUSH
        ),
    ),
    ("gone", "Gone from Apple Contacts", lambda c: c.kind is SyncKind.REMOVED_UPSTREAM),
)
SHOWS = {"all": "All", "auto": "Fills only", "conflicts": "Conflicts"}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _run_url(run: SyncRun) -> str:
    return f"/contacts/sync/{run.id}"


@router.get("/contacts/sync")
def latest(db: DbSession) -> Response:
    run = sync.latest_run(db)
    return _redirect(_run_url(run) if run else "/people")


@router.post("/contacts/sync")
def start(request: Request, db: DbSession, source: ContactSourceDep) -> Response:
    if source is None:
        flash(request, "iCloud isn't set up: add CALDAV_USERNAME and CALDAV_PASSWORD.", "error")
        return _redirect("/people")
    try:
        cards = sync.cards_from_raw(source.fetch_cards())
    except CardDavError as exc:
        flash(request, f"iCloud sync failed: {exc}", "error")
        return _redirect("/people")
    run = sync.start_run(db, cards, SyncSource.CARDDAV, include_push=True)
    db.commit()
    return _redirect(_run_url(run))


@router.post("/contacts/sync/vcf")
def start_from_file(request: Request, db: DbSession, file: UploadFile) -> Response:
    try:
        text = file.file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash(request, "That file is not UTF-8 vCard text.", "error")
        return _redirect("/people")
    cards = [sync.CardInput(card) for card in parse_vcards(text)]
    run = sync.start_run(db, cards, SyncSource.VCF)
    db.commit()
    return _redirect(_run_url(run))


def _matches(view: sync.ChangeView, q: str, show: str) -> bool:
    change = view.change
    if q and q not in normalize_text(change.person_name):
        return False
    if show == "auto":
        return change.kind is SyncKind.AUTO
    if show == "conflicts":
        return change.kind is SyncKind.CONFLICT
    return True


@router.get("/contacts/sync/{run_id:int}")
def review(
    request: Request, db: DbSession, run_id: int, q: str = "", show: str = "all"
) -> Response:
    run = sync.get_run(db, run_id)
    show = show if show in SHOWS else "all"
    needle = normalize_text(q)
    views = [sync.change_view(c) for c in run.changes]
    open_run = run.status is SyncRunStatus.REVIEW
    pending = [v for v in views if v.change.state is SyncState.PENDING]
    shown = [v for v in pending if _matches(v, needle, show)] if open_run else []
    sections = []
    for key, title, belongs in SECTIONS:
        rows = sorted((v for v in shown if belongs(v.change)), key=lambda v: v.change.person_name)
        if rows:
            sections.append({"key": key, "title": title, "rows": rows})
    done = [v for v in views if v.change.state is not SyncState.PENDING]
    return templates.TemplateResponse(
        request,
        "contacts/review.html",
        {
            "run": run,
            "sections": sections,
            "pending_total": len(pending),
            "done": done,
            "q": q,
            "show": show,
            "shows": SHOWS,
            "auto_total": sum(1 for v in pending if v.change.kind is SyncKind.AUTO),
            "State": SyncState,
            "push_enabled": run.source is SyncSource.CARDDAV,
            "backups": sync.list_backups(db, run.id),
        },
    )


@router.post("/contacts/sync/{run_id:int}/apply")
async def apply(request: Request, db: DbSession, source: ContactSourceDep, run_id: int) -> Response:
    form = await request.form()
    choices: dict[int, str] = {}
    seen: set[int] = set()
    for name, value in form.multi_items():
        if name == "seen" and isinstance(value, str) and value.isdigit():
            seen.add(int(value))
        elif name.startswith("c") and name[1:].isdigit() and isinstance(value, str):
            choices[int(name[1:])] = value
    try:
        report = sync.apply_run(db, run_id, choices, seen=seen, writer=source)
    except InvalidInputError as exc:
        flash(request, str(exc), "error")
        return _redirect(f"/contacts/sync/{run_id}")
    db.commit()
    flash(request, f"Sync: {report.summary}.")
    for failure in report.failed[:10]:
        flash(request, failure, "error")
    return _redirect(f"/contacts/sync/{run_id}")


@router.post("/contacts/sync/changes/{change_id:int}/undo")
def undo(
    request: Request,
    db: DbSession,
    source: ContactSourceDep,
    change_id: int,
    run_id: Annotated[int, Form()],
) -> Response:
    try:
        change = sync.undo_change(db, change_id, writer=source)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/contacts/sync/{run_id}")
    db.commit()
    flash(request, f"Undone: {change.person_name}.")
    return _redirect(f"/contacts/sync/{run_id}")


@router.post("/contacts/sync/forget")
def forget(request: Request, db: DbSession) -> Response:
    count = sync.forget_rejections(db)
    db.commit()
    flash(request, f"Forgot {count} skipped change(s); the next sync proposes them again.")
    return _redirect("/people")


@router.post("/contacts/sync/backups/{backup_id:int}/restore")
def restore(
    request: Request,
    db: DbSession,
    source: ContactSourceDep,
    backup_id: int,
    run_id: Annotated[int, Form()],
) -> Response:
    try:
        backup = sync.restore_backup(db, backup_id, source)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect(f"/contacts/sync/{run_id}")
    db.commit()
    person = db.get(Person, backup.person_id)
    flash(
        request, f"Restored {person.display_name if person else 'the contact'} in Apple Contacts."
    )
    return _redirect(f"/contacts/sync/{run_id}")
