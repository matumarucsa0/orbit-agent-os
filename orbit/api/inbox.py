from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Request

from orbit.api.schemas import CaptureIn, CaptureOut, ConfirmIn, ConfirmOut, ProposalOut
from orbit.deps import CaptureExtractorDep, DbSession, SettingsDep, run_capture
from orbit.domain.enums import ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.services import inbox

router = APIRouter(tags=["inbox"])


@router.post("/capture", response_model=CaptureOut)
def capture_text(
    body: CaptureIn,
    request: Request,
    background: BackgroundTasks,
    db: DbSession,
    settings: SettingsDep,
    extractor: CaptureExtractorDep,
) -> CaptureOut:
    """Dictated text -> Inbox proposals (never applied directly). Used by `Orbit Capture`.
    Answers at once with the text saved as a draft; Claude reads it afterwards and a push says
    when the proposals replace the draft (SPEC §2.33)."""
    result = run_capture(
        request, background, db, settings, extractor, body.text, meeting_id=body.meeting_id
    )
    return CaptureOut(
        summary=result.summary,
        proposal_ids=[p.id for p in result.proposals],
        items=result.item_count,
    )


@router.get("/inbox", response_model=list[ProposalOut])
def list_inbox(
    db: DbSession,
    status: Literal["pending", "confirmed", "rejected", "all"] = "pending",
    kind: ProposalKind | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Proposal]:
    return inbox.list_proposals(
        db,
        status=None if status == "all" else ProposalStatus(status),
        kind=kind,
        limit=limit,
        offset=offset,
    )


@router.get("/inbox/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: int, db: DbSession) -> Proposal:
    return inbox.get_proposal(db, proposal_id)


@router.post("/inbox/{proposal_id}/confirm", response_model=ConfirmOut)
def confirm_proposal(
    proposal_id: int, db: DbSession, settings: SettingsDep, body: ConfirmIn | None = None
) -> ConfirmOut:
    proposal = inbox.get_proposal(db, proposal_id)
    # On an error nothing is committed: the request's session rolls back when it closes.
    result = inbox.confirm(
        db,
        proposal,
        today=datetime.now(UTC).astimezone(settings.timezone).date(),
        default_currency=settings.default_currency,
        payload=body.payload if body else None,
    )
    db.commit()
    return ConfirmOut(
        proposal=ProposalOut.model_validate(result.proposal),
        message=result.message,
        person_id=result.person_id,
    )


@router.post("/inbox/{proposal_id}/reject", response_model=ProposalOut)
def reject_proposal(proposal_id: int, db: DbSession) -> Proposal:
    proposal = inbox.reject(db, inbox.get_proposal(db, proposal_id))
    db.commit()
    return proposal


@router.post("/inbox/{proposal_id}/reopen", response_model=ProposalOut)
def reopen_proposal(proposal_id: int, db: DbSession) -> Proposal:
    proposal = inbox.reopen(db, inbox.get_proposal(db, proposal_id))
    db.commit()
    return proposal
