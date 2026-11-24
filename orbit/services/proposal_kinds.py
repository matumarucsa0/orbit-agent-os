"""The proposal kind registry: what each `ProposalKind` is, in one place per kind.

A kind is a `ProposalHandler`: its payload model, the function that applies a confirmed payload,
and optionally how to rebuild the summary after an edit and when a rejected proposal may be
reopened. The module that owns a kind defines its handler; `services/inbox.py` registers it with
one `register(...)` line and looks it up by kind. Adding a kind touches the owning module and
that line (plus the enum, the UI card and the SPEC).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from orbit.domain.enums import ProposalKind
from orbit.domain.inbox import Proposal


@dataclass(frozen=True)
class ApplyContext:
    today: date
    default_currency: str
    now: datetime | None = None


@dataclass(frozen=True)
class Applied:
    message: str  # "Saved: 1 note, 1 debt."
    person_id: int | None = None  # the person a new_person proposal created, or the payer


@dataclass(frozen=True)
class ProposalHandler[P: BaseModel]:
    kind: ProposalKind
    payload_model: type[P]
    # Applies a validated payload in the caller's transaction; raises InvalidInputError.
    apply: Callable[[Session, P, ApplyContext], Applied]
    # The summary after the payload was edited; None keeps the stored summary.
    summary: Callable[[Session, P], str] | None = None
    # Raises InvalidInputError when a rejected proposal can't go back to pending.
    check_reopen: Callable[[Session, Proposal], None] | None = None


_REGISTRY: dict[ProposalKind, ProposalHandler[Any]] = {}


def register(handler: ProposalHandler[Any]) -> None:
    """Register the handler of a proposal kind; registering a kind twice raises ValueError."""
    if handler.kind in _REGISTRY:
        raise ValueError(f"Proposal kind {handler.kind.value} is already registered.")
    _REGISTRY[handler.kind] = handler


def handler_for(kind: ProposalKind) -> ProposalHandler[Any]:
    """The registered handler for a kind; raises LookupError when none is registered."""
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise LookupError(f"No handler registered for proposal kind {kind.value}.") from None


def registered_kinds() -> set[ProposalKind]:
    """The set of kinds that have a handler."""
    return set(_REGISTRY)
