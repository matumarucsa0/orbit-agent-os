"""`/a/{token}`: push notification action buttons (ntfy `http` actions).

No bearer token: the signed, single-purpose token in the path is the authorization.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, status
from fastapi.responses import PlainTextResponse

from orbit.deps import DbSession, SettingsDep
from orbit.domain.enums import ProposalStatus
from orbit.services import action_tokens, inbox
from orbit.services.errors import InvalidInputError, NotFoundError

router = APIRouter()


@router.post("/a/{token}", response_class=PlainTextResponse)
def push_action(token: str, db: DbSession, settings: SettingsDep) -> PlainTextResponse:
    secret = settings.orbit_secret_key.get_secret_value()
    try:
        action = action_tokens.read_token(secret, token)
        proposal = inbox.get_proposal(db, action.proposal_id)
    except (InvalidInputError, NotFoundError) as exc:
        return PlainTextResponse(str(exc), status_code=status.HTTP_403_FORBIDDEN)
    if proposal.reopened_at is not None and action.issued_at < proposal.reopened_at:
        # Reopened in the Inbox since this push went out: finish it there.
        return PlainTextResponse(
            "Reopened since; open the Inbox instead.", status_code=status.HTTP_409_CONFLICT
        )
    if proposal.status is not ProposalStatus.PENDING:
        # The single-use part: every token of a resolved proposal is spent.
        return PlainTextResponse(
            f"Already {proposal.status.value}.", status_code=status.HTTP_409_CONFLICT
        )
    try:
        if action.action == "reject":
            inbox.reject(db, proposal)
            message = "Rejected."
        else:
            result = inbox.confirm(
                db,
                proposal,
                today=datetime.now(UTC).astimezone(settings.timezone).date(),
                default_currency=settings.default_currency,
            )
            message = result.message
    except InvalidInputError as exc:
        db.rollback()
        return PlainTextResponse(
            f"{exc} Open the Inbox to finish it.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    db.commit()
    return PlainTextResponse(message)
