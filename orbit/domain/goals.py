"""Goals: what I want, in what order. The spine other modules link to (`VISION.md` principle 3)."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Column, ForeignKey, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, TimestampMixin, enum_column
from orbit.domain.enums import GoalHorizon, GoalStatus
from orbit.domain.people import Person

if TYPE_CHECKING:
    from orbit.domain.ledger import Promise

goal_person = Table(
    "goal_person",
    Base.metadata,
    Column("goal_id", ForeignKey("goal.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", ForeignKey("person.id", ondelete="CASCADE"), primary_key=True, index=True),
)


class Goal(TimestampMixin, Base):
    """Something I want. Only active goals have a rank (1 = most important, contiguous)."""

    __tablename__ = "goal"
    __table_args__ = (
        CheckConstraint("(status = 'active') = (rank IS NOT NULL)", name="rank_when_active"),
        CheckConstraint("rank IS NULL OR rank >= 1", name="rank_positive"),
        CheckConstraint(
            "(status IN ('done', 'dropped')) = (closed_at IS NOT NULL)", name="closed_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # normalize_text(title); unique among active goals (kept by the service).
    title_norm: Mapped[str] = mapped_column(String(200), index=True)
    why: Mapped[str | None] = mapped_column(Text)
    horizon: Mapped[GoalHorizon | None] = mapped_column(enum_column(GoalHorizon, "goal_horizon"))
    # What "done" looks like, in words. Numbers come from the module that measures them.
    target: Mapped[str | None] = mapped_column(Text)
    target_date: Mapped[date | None]
    status: Mapped[GoalStatus] = mapped_column(enum_column(GoalStatus, "goal_status"), index=True)
    rank: Mapped[int | None]
    # When I last confirmed or moved it in the weekly review.
    reviewed_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]

    people: Mapped[list[Person]] = relationship(
        secondary=goal_person, order_by="Person.display_name"
    )
    promises: Mapped[list[Promise]] = relationship(back_populates="goal", order_by="Promise.id")
