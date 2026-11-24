"""Duplicate candidates, "not a duplicate", the merge preview/confirm flow, and undoing a merge."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from orbit.deps import DbSession
from orbit.services import duplicates, people
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.search import search_people
from orbit.web.auth import require_login
from orbit.web.templating import flash, templates

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/people/duplicates")
def duplicates_list(request: Request, db: DbSession) -> Response:
    return templates.TemplateResponse(
        request,
        "people/duplicates.html",
        {"candidates": duplicates.find_candidates(db), "merges": duplicates.list_merges(db)},
    )


@router.post("/people/duplicates/dismiss")
def duplicates_dismiss(
    request: Request, db: DbSession, a_id: Annotated[int, Form()], b_id: Annotated[int, Form()]
) -> Response:
    duplicates.dismiss(db, a_id, b_id)
    db.commit()
    flash(request, "Marked as not a duplicate.")
    return RedirectResponse("/people/duplicates", status_code=303)


@router.get("/people/{person_id:int}/merge")
def merge_preview(
    request: Request,
    db: DbSession,
    person_id: int,
    q: str = "",
    other_id: Annotated[int | None, Query(alias="with")] = None,
) -> Response:
    person = people.get_person(db, person_id)
    other = None
    if other_id is not None and other_id != person_id:
        other = people.get_person(db, other_id)
    results = [p for p in search_people(db, q, limit=20) if p.id != person.id] if q else []
    return templates.TemplateResponse(
        request,
        "people/merge.html",
        {"person": person, "other": other, "q": q, "results": results},
    )


@router.post("/people/merge")
def merge_confirm(
    request: Request,
    db: DbSession,
    keep_id: Annotated[int, Form()],
    merge_id: Annotated[int, Form()],
) -> Response:
    try:
        merged_name = people.get_person(db, merge_id).display_name
        keep = duplicates.merge(db, keep_id, merge_id)
    except InvalidInputError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(f"/people/{keep_id}", status_code=303)
    db.commit()
    flash(
        request,
        f"Merged {merged_name} into {keep.display_name}.",
        link="/people/duplicates#merges",
        link_label="Undo…",
    )
    return RedirectResponse(f"/people/{keep.id}", status_code=303)


@router.post("/people/merges/{record_id:int}/undo")
def merge_undo(request: Request, db: DbSession, record_id: int) -> Response:
    try:
        restored = duplicates.undo_merge(db, record_id)
    except (InvalidInputError, NotFoundError) as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/people/duplicates#merges", status_code=303)
    db.commit()
    flash(request, f"Merge undone: {restored.display_name} is back.")
    return RedirectResponse(f"/people/{restored.id}", status_code=303)
