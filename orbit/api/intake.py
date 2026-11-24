"""`POST /api/v1/intake`: one share-sheet Shortcut for text, links and files (SPEC §2.29)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from pydantic import BaseModel

from orbit.api import imports, school_resources
from orbit.deps import (
    CaptureExtractorDep,
    DbSession,
    PushSenderDep,
    RateSourceDep,
    SettingsDep,
    TransactionClassifierDep,
    run_capture,
)
from orbit.services import intake

router = APIRouter(tags=["intake"])


class IntakeOut(BaseModel):
    route: (
        str  # where it went: capture, bank_import, contacts, school_resource, school_resource_url
    )
    summary: str  # shown by the Shortcut as a notification
    ids: list[int] = []  # proposal ids (capture, bank import) or the resource id
    course: str | None = None  # the course code a PDF or link was filed under, if one matched


@router.post("/intake", response_model=IntakeOut)
async def share(
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    settings: SettingsDep,
    extractor: CaptureExtractorDep,
    classifier: TransactionClassifierDep,
    rates: RateSourceDep,
    sender: PushSenderDep,
    text: Annotated[str | None, Form()] = None,
    url: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> IntakeOut:
    """Whatever was shared goes where its shape says (`services/intake.py`); nothing needs a
    choice. Capture, bank import and the school reader keep their own rules (proposals only)."""
    data = await file.read() if file is not None else None
    decision = intake.decide(
        text=text, url=url, filename=file.filename if file else None, data=data
    )
    course = intake.match_course(db, decision.course_hint)
    code = course.code if course is not None else None
    if decision.route is intake.Route.CAPTURE:
        assert text is not None
        done = run_capture(request, background, db, settings, extractor, text)
        return IntakeOut(
            route=decision.route, summary=done.summary, ids=[p.id for p in done.proposals]
        )
    if decision.route is intake.Route.URL:
        assert decision.url is not None
        body = school_resources.ResourceIn(url=decision.url, course_code=code)
        resource = school_resources.add_resource(body, db)
        where = f"filed under {code}" if code else "waiting in Unsorted"
        return IntakeOut(
            route=decision.route,
            summary=f"Saved the link, {where}.",
            ids=[resource.id],
            course=code,
        )
    assert file is not None and data is not None
    await file.seek(0)
    if decision.route is intake.Route.PDF:
        resource = await school_resources.add_file(db, settings, file, code)
        where = f"filed under {code}" if code else "waiting in Unsorted"
        return IntakeOut(
            route=decision.route,
            summary=f"Saved the PDF, {where}.",
            ids=[resource.id],
            course=code,
        )
    if decision.route is intake.Route.CONTACTS:
        report = imports.import_vcard(file, db)
        return IntakeOut(route=decision.route, summary=report.summary)
    result = imports.import_bank(file, db, settings, classifier, rates, sender)
    ids = [*result.proposal_ids, *result.payment_proposal_ids]
    return IntakeOut(route=decision.route, summary=result.summary, ids=ids)
