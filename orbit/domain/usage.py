"""A record of every Claude API call, for token usage totals."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from orbit.domain.base import Base, utcnow


class ClaudeCall(Base):
    __tablename__ = "claude_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    # What it was for: capture, classification, meeting_link, brief, draft.
    purpose: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    # False when the call failed or its answer was unusable (tokens may then be unknown: 0).
    ok: Mapped[bool] = mapped_column(default=True)
    # Wall-clock time of the call in milliseconds (None: not measured, e.g. rows from before).
    latency_ms: Mapped[int | None]
    # The prompt that was sent, as "capture@3" (orbit.integrations.prompts_registry).
    prompt_version: Mapped[str | None] = mapped_column(String(60))
    # Estimated price in millionths of a euro (orbit.services.pricing); 0 for fake and worker calls.
    cost_micro_eur: Mapped[int] = mapped_column(default=0)
