"""What's between me and a person: debts (money, items, favors) and promises."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, TimestampMixin, enum_column
from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection, PromiseStatus

if TYPE_CHECKING:
    from orbit.domain.goals import Goal
    from orbit.domain.people import Person


class Debt(TimestampMixin, Base):
    """A money / item / favor debt in either direction.

    A partial settlement settles the original (its amount stays as the historical record) and
    creates an open remainder debt pointing back to it via `parent_id`.
    """

    __tablename__ = "debt"
    __table_args__ = (
        CheckConstraint(
            "type != 'money' OR (amount_minor IS NOT NULL AND amount_minor > 0"
            " AND currency IS NOT NULL)",
            name="money_amount",
        ),
        CheckConstraint(
            "type = 'money' OR (amount_minor IS NULL AND currency IS NULL"
            " AND description IS NOT NULL)",
            name="thing_description",
        ),
        CheckConstraint("reminder_days IS NULL OR reminder_days >= 1", name="reminder_days"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    direction: Mapped[DebtDirection] = mapped_column(enum_column(DebtDirection, "debt_direction"))
    type: Mapped[DebtType] = mapped_column(enum_column(DebtType, "debt_type"))
    amount_minor: Mapped[int | None]
    currency: Mapped[str | None] = mapped_column(String(3))
    # Required for items and favors ("charger", "helped me move"); optional for money ("pizza").
    description: Mapped[str | None] = mapped_column(Text)
    # "D-0142", money debts only; the user can put it in a payment note for exact matching.
    reference_code: Mapped[str | None] = mapped_column(String(20), unique=True)
    due: Mapped[date | None]
    # Items only: overrides ITEM_REMINDER_DAYS.
    reminder_days: Mapped[int | None]
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("debt.id", ondelete="SET NULL"), index=True
    )
    settled_at: Mapped[datetime | None]
    # The bank row this debt was split from, if any.
    source_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transaction.id", ondelete="SET NULL"), index=True
    )
    # The incoming bank payment this debt was settled with, if any (set on confirm, never auto).
    settled_by_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transaction.id", ondelete="SET NULL"), index=True
    )

    person: Mapped[Person] = relationship(back_populates="debts")
    parent: Mapped[Debt | None] = relationship(remote_side=[id], back_populates="remainder")
    remainder: Mapped[Debt | None] = relationship(back_populates="parent")

    @property
    def is_open(self) -> bool:
        return self.settled_at is None


class Promise(TimestampMixin, Base):
    __tablename__ = "promise"
    __table_args__ = (CheckConstraint("(status = 'open') = (closed_at IS NULL)", name="closed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    direction: Mapped[PromiseDirection] = mapped_column(
        enum_column(PromiseDirection, "promise_direction")
    )
    text: Mapped[str] = mapped_column(Text)
    due: Mapped[date | None] = mapped_column(index=True)
    status: Mapped[PromiseStatus] = mapped_column(enum_column(PromiseStatus, "promise_status"))
    closed_at: Mapped[datetime | None]
    # The meeting it was made at, if it came from a post-meeting capture.
    meeting_id: Mapped[int | None] = mapped_column(
        ForeignKey("meeting.id", ondelete="SET NULL"), index=True
    )

    # The goal this promise serves, if any (cleared when the goal is deleted).
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("goal.id", ondelete="SET NULL"), index=True
    )

    person: Mapped[Person] = relationship(back_populates="promises")
    goal: Mapped[Goal | None] = relationship(back_populates="promises")
