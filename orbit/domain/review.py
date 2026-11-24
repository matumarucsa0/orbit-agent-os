"""The weekly review: what the review agent was given and what it answered (SPEC §2.17)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from orbit.domain.base import Base, TimestampMixin, enum_column
from orbit.domain.enums import ReviewStatus


class WeeklyReview(TimestampMixin, Base):
    """One review per planned week. The row is inserted before Claude is asked, so two runs of
    the same week cannot both write one."""

    __tablename__ = "weekly_review"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Monday of the planned week; the review's identity.
    plan_start: Mapped[date] = mapped_column(unique=True)
    plan_end: Mapped[date]
    review_start: Mapped[date]  # the week reviewed (Monday to Sunday)
    review_end: Mapped[date]
    status: Mapped[ReviewStatus] = mapped_column(enum_column(ReviewStatus, "review_status"))
    # Validated ReviewOutput, as JSON; None while failed.
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # What Claude was given (the two snapshots and the rest), kept so any claim can be checked.
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_chars: Mapped[int | None]
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    pushed_at: Mapped[datetime | None]
