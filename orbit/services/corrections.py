"""What the user changed or rejected on earlier capture cards, told back to Claude (SPEC §2.28).

Claude proposes, the user fixes the card. The difference between `Proposal.original_json` and the
confirmed `payload_json` is a lesson ("Janko" was Janko Kováč; a "they owe me" was "I owe them"),
and a rejected card is a thing that was not wanted. Both are short lines in the capture prompt.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.domain.people import Person

MAX_CORRECTIONS = 6  # lines of corrections in the prompt
MAX_REJECTED = 6  # lines of rejected cards in the prompt
LOOKBACK_DAYS = 60  # corrections older than this are not told
SCAN_LIMIT = 40  # confirmed captures looked at
SUMMARY_CHARS = 160


def _names(session: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = session.execute(select(Person.id, Person.display_name).where(Person.id.in_(ids)))
    return {pid: name for pid, name in rows}


def _debt_lines(original: dict[str, Any], final: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for before, after in zip(original.get("debts", []), final.get("debts", []), strict=False):
        if not after.get("include", True):
            continue
        what = after.get("description") or after.get("type", "debt")
        if before.get("direction") != after.get("direction"):
            lines.append(
                f'a debt "{what}" was read as {before["direction"]}, it is {after["direction"]}'
            )
        if before.get("type") != after.get("type"):
            lines.append(f'a debt "{what}" was read as {before["type"]}, it is {after["type"]}')
        if before.get("currency") != after.get("currency") and after.get("currency"):
            lines.append(
                f'a debt "{what}" was read as {before.get("currency")}, it is {after["currency"]}'
            )
    return lines


def _promise_lines(original: dict[str, Any], final: dict[str, Any]) -> list[str]:
    return [
        f'a promise "{after.get("text", "")}" was read as {before["direction"]}, '
        f"it is {after['direction']}"
        for before, after in zip(
            original.get("promises", []), final.get("promises", []), strict=False
        )
        if after.get("include", True) and before.get("direction") != after.get("direction")
    ]


def _name_lines(
    original: dict[str, Any], final: dict[str, Any], names: dict[int, str]
) -> list[str]:
    lines: list[str] = []
    for before, after in zip(
        original.get("new_people", []), final.get("new_people", []), strict=False
    ):
        person_id = after.get("person_id")
        if (
            before.get("action", "create") == "create"
            and after.get("action") == "link"
            and person_id
        ):
            lines.append(
                f'the unknown name "{after.get("name", "")}" was an existing person: '
                f"{names.get(person_id, 'someone known')}"
            )
    return lines


def recent_corrections(
    session: Session, *, now: datetime | None = None, limit: int = MAX_CORRECTIONS
) -> list[str]:
    """Newest first: what the user changed on confirmed captures in the last `LOOKBACK_DAYS`."""
    since = (now or utcnow()) - timedelta(days=LOOKBACK_DAYS)
    rows = list(
        session.scalars(
            select(Proposal)
            .where(
                Proposal.kind == ProposalKind.CAPTURE,
                Proposal.status == ProposalStatus.CONFIRMED,
                Proposal.original_json.is_not(None),
                Proposal.resolved_at >= since,
            )
            .order_by(Proposal.resolved_at.desc(), Proposal.id.desc())
            .limit(SCAN_LIMIT)
        )
    )
    ids = {
        pid
        for p in rows
        for d in (p.payload_json or {}).get("new_people", [])
        if (pid := d.get("person_id"))
    }
    names = _names(session, ids)
    lines: list[str] = []
    for proposal in rows:
        original, final = proposal.original_json or {}, proposal.payload_json or {}
        for line in (
            *_name_lines(original, final, names),
            *_debt_lines(original, final),
            *_promise_lines(original, final),
        ):
            if line not in lines:
                lines.append(line)
    return lines[:limit]


def recent_rejections(
    session: Session, *, now: datetime | None = None, limit: int = MAX_REJECTED
) -> list[str]:
    """Summaries of capture and new-person cards rejected in the last `LOOKBACK_DAYS`."""
    since = (now or utcnow()) - timedelta(days=LOOKBACK_DAYS)
    found = session.scalars(
        select(Proposal.summary)
        .where(
            Proposal.kind.in_((ProposalKind.CAPTURE, ProposalKind.NEW_PERSON)),
            Proposal.status == ProposalStatus.REJECTED,
            Proposal.resolved_at >= since,
        )
        .order_by(Proposal.resolved_at.desc(), Proposal.id.desc())
        .limit(limit)
    )
    return [text[:SUMMARY_CHARS] for text in found]
