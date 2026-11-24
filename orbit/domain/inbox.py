"""The Inbox (proposals waiting for my confirmation) and the record of sent push notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from orbit.domain.base import Base, enum_column, utcnow
from orbit.domain.enums import ProposalKind, ProposalStatus


class Proposal(Base):
    """Something automation or Claude wants to change; nothing is applied until I confirm it.

    Rejected proposals are kept (dedupe/learning); the Inbox lists recent ones so a mis-tap can
    be reopened.
    """

    __tablename__ = "proposal"
    __table_args__ = (
        CheckConstraint("(status = 'pending') = (resolved_at IS NULL)", name="resolved_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ProposalKind] = mapped_column(enum_column(ProposalKind, "proposal_kind"))
    status: Mapped[ProposalStatus] = mapped_column(
        enum_column(ProposalStatus, "proposal_status"), index=True
    )
    summary: Mapped[str] = mapped_column(Text)
    # Kind-specific, validated by the service for that kind. Always reassign, never mutate in
    # place: the JSON column doesn't track nested changes.
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    # What was proposed before any edit (capture and new_person only): the difference to
    # `payload_json` is what the user corrected (SPEC §2.28). None on older rows.
    original_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confidence: Mapped[float | None]
    # Where it came from: "capture", "bank_import", …; source_id is that source's own id.
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]
    # The Claude prompt that produced it, "capture@3" (None for proposals Claude did not make).
    prompt_version: Mapped[str | None] = mapped_column(String(60))
    # Set when a rejected proposal is reopened: push action links made before it are spent.
    reopened_at: Mapped[datetime | None]


class Notification(Base):
    """Every push ever sent (or queued), keyed so the same one is never sent twice."""

    __tablename__ = "notification"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(250))
    body: Mapped[str] = mapped_column(Text)
    # The full push (click URL, tags, action buttons), so a queued one can be sent later.
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Not sent before this moment: the end of quiet hours, or a retry after a failure.
    not_before: Mapped[datetime] = mapped_column(index=True)
    sent_at: Mapped[datetime | None]
    delivered: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
