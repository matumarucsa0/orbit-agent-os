"""A custom review report over a date range: what was asked and what came back (SPEC §2.22)."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from orbit.domain.base import Base, TimestampMixin, enum_column
from orbit.domain.enums import ReviewStatus


class ReviewReport(TimestampMixin, Base):
    """One report. The row is inserted `pending` before Claude is asked, so two requests cannot
    both run. Reports are never cached or deduplicated: the same range asked again is a new one."""

    __tablename__ = "review_report"

    id: Mapped[int] = mapped_column(primary_key=True)
    range_start: Mapped[date]
    range_end: Mapped[date]
    # What to look at: free text, plus optional links (the person also narrows the context).
    focus_text: Mapped[str | None] = mapped_column(Text)
    focus_goal_id: Mapped[int | None] = mapped_column(ForeignKey("goal.id", ondelete="SET NULL"))
    focus_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL")
    )
    focus_course_id: Mapped[int | None] = mapped_column(
        ForeignKey("course.id", ondelete="SET NULL")
    )
    status: Mapped[ReviewStatus] = mapped_column(enum_column(ReviewStatus, "report_status"))
    # Validated ReportOutput as JSON; None when Claude was not used or the report failed.
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Facts counted by Orbit from the context snapshot: [{label, value, note?}].
    numbers_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    # The context snapshot and the rest of what Claude was given, kept for "What Claude saw".
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_chars: Mapped[int | None]
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    # False for the plain facts-only report made when Claude is not available.
    claude_used: Mapped[bool] = mapped_column(default=False)
    # Why the plain report was made instead of a Claude one (shown on the page).
    plain_reason: Mapped[str | None] = mapped_column(String(300))
