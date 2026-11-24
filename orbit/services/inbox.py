"""The Inbox: proposals wait here until I confirm (optionally after editing) or reject them.

Confirming applies the payload in the caller's transaction: everything is validated first and the
caller rolls back on an error, so a proposal is applied completely or not at all. Callers commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.services import capture, goals, payments, people, promises, school_reader, split
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.proposal_kinds import ApplyContext, handler_for, register

# One line per proposal kind; the module that owns a kind defines its handler.
register(capture.CAPTURE_HANDLER)
register(capture.NEW_PERSON_HANDLER)
register(split.HANDLER)
register(payments.HANDLER)
register(promises.FOLLOW_UP_HANDLER)
register(promises.PROMISE_DUE_HANDLER)
register(goals.GOAL_RERANK_HANDLER)
register(goals.GOAL_LINK_HANDLER)
register(school_reader.DEADLINE_HANDLER)
register(school_reader.FACT_HANDLER)
register(people.ADD_ALIAS_HANDLER)


@dataclass(frozen=True)
class ConfirmResult:
    proposal: Proposal
    message: str  # "Saved: 1 note, 1 debt."
    person_id: int | None = None  # the person a new_person proposal created, or the payer


def get_proposal(session: Session, proposal_id: int) -> Proposal:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise NotFoundError(f"Proposal {proposal_id} not found.")
    return proposal


def list_proposals(
    session: Session,
    status: ProposalStatus | None = ProposalStatus.PENDING,
    kind: ProposalKind | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Proposal]:
    """Newest first. Rejected ones are only listed when asked for explicitly."""
    query = select(Proposal)
    if status is not None:
        query = query.where(Proposal.status == status)
    if kind is not None:
        query = query.where(Proposal.kind == kind)
    query = query.order_by(Proposal.created_at.desc(), Proposal.id.desc())
    return list(session.scalars(query.limit(limit).offset(offset)))


def recently_rejected(
    session: Session, *, now: datetime | None = None, days: int = 30, limit: int = 20
) -> list[Proposal]:
    """Rejected in the last `days`, most recently rejected first (to reopen a mis-tap)."""
    since = (now or utcnow()) - timedelta(days=days)
    return list(
        session.scalars(
            select(Proposal)
            .where(Proposal.status == ProposalStatus.REJECTED, Proposal.resolved_at >= since)
            .order_by(Proposal.resolved_at.desc(), Proposal.id.desc())
            .limit(limit)
        )
    )


def pending_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count(Proposal.id)).where(Proposal.status == ProposalStatus.PENDING)
        )
        or 0
    )


def _check_pending(proposal: Proposal) -> None:
    if proposal.status is not ProposalStatus.PENDING:
        raise InvalidInputError(f"This proposal was already {proposal.status.value}.")


def _validated(kind: ProposalKind, payload: dict[str, Any]) -> dict[str, Any]:
    model: type[BaseModel] = handler_for(kind).payload_model
    try:
        return model.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise InvalidInputError("Invalid proposal data: " + ", ".join(fields)) from exc


def update_payload(session: Session, proposal: Proposal, payload: dict[str, Any]) -> Proposal:
    """Save an edited payload without confirming (e.g. an amount filled in)."""
    _check_pending(proposal)
    handler = handler_for(proposal.kind)
    proposal.payload_json = _validated(proposal.kind, payload)
    if handler.summary is not None:
        proposal.summary = handler.summary(
            session, handler.payload_model.model_validate(proposal.payload_json)
        )
    session.flush()
    return proposal


def confirm(
    session: Session,
    proposal: Proposal,
    *,
    today: date,
    default_currency: str,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ConfirmResult:
    """Apply the proposal (with an edited payload, if given) and mark it confirmed."""
    _check_pending(proposal)
    if payload is not None:
        update_payload(session, proposal, payload)
    handler = handler_for(proposal.kind)
    data = handler.payload_model.model_validate(_validated(proposal.kind, proposal.payload_json))
    applied = handler.apply(session, data, ApplyContext(today, default_currency, now))
    proposal.status = ProposalStatus.CONFIRMED
    proposal.resolved_at = now or utcnow()
    session.flush()
    return ConfirmResult(proposal, applied.message, applied.person_id)


def reject(session: Session, proposal: Proposal, now: datetime | None = None) -> Proposal:
    """Hide the proposal; it's kept (for dedupe/learning) but nothing is applied."""
    _check_pending(proposal)
    proposal.status = ProposalStatus.REJECTED
    proposal.resolved_at = now or utcnow()
    session.flush()
    return proposal


def reject_group(session: Session, proposal: Proposal, now: datetime | None = None) -> int:
    """Reject every pending proposal made from the same dictation as `proposal` (its `capture` and
    `new_person` cards). Reversible with Reopen; nothing is applied. Returns how many."""
    group = (proposal.payload_json or {}).get("group")
    siblings = capture.pending_in_group(session, group) or [proposal]
    for sibling in siblings:
        reject(session, sibling, now)
    return len(siblings)


def reopen(session: Session, proposal: Proposal, now: datetime | None = None) -> Proposal:
    """Put a rejected proposal back in the Inbox, as it was. Push action links made before this
    stay spent (see `reopened_at`); confirming re-validates everything as usual.

    A kind can refuse (its `check_reopen`): a bank row gets at most one open split and one open
    payment match, so if a newer one exists (e.g. I split it by hand since), reopening is refused.
    """
    if proposal.status is not ProposalStatus.REJECTED:
        raise InvalidInputError(
            f"Only rejected proposals can be reopened; this one is {proposal.status.value}."
        )
    check = handler_for(proposal.kind).check_reopen
    if check is not None:
        check(session, proposal)
    proposal.status = ProposalStatus.PENDING
    proposal.resolved_at = None
    proposal.reopened_at = now or utcnow()
    session.flush()
    return proposal
