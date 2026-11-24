from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

import orbit.services.inbox  # noqa: F401  (importing it registers the kinds)
from orbit.domain.enums import ProposalKind
from orbit.services import proposal_kinds
from orbit.services.proposal_kinds import Applied, ProposalHandler


class Empty(BaseModel):
    pass


def apply(session: Session, payload: Empty, ctx: proposal_kinds.ApplyContext) -> Applied:
    return Applied("done")


def test_every_kind_is_registered() -> None:
    assert proposal_kinds.registered_kinds() == set(ProposalKind)


def test_handlers_know_their_payload_model() -> None:
    for kind in ProposalKind:
        handler = proposal_kinds.handler_for(kind)
        assert handler.kind is kind
        assert issubclass(handler.payload_model, BaseModel)


def test_only_bank_row_kinds_check_reopen() -> None:
    with_check = {k for k in ProposalKind if proposal_kinds.handler_for(k).check_reopen}
    assert with_check == {ProposalKind.SPLIT_TRANSACTION, ProposalKind.PAYMENT_MATCH}


def test_only_editable_kinds_rebuild_the_summary() -> None:
    with_summary = {k for k in ProposalKind if proposal_kinds.handler_for(k).summary}
    assert with_summary == {ProposalKind.CAPTURE, ProposalKind.PAYMENT_MATCH}


def test_registering_a_kind_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="already registered"):
        proposal_kinds.register(ProposalHandler(ProposalKind.CAPTURE, Empty, apply))
