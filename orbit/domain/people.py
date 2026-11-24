"""People and what hangs off them: aliases, handles, tags, notes, interactions."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKey,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, TimestampMixin, enum_column, utcnow
from orbit.domain.enums import HandleType, InteractionKind

if TYPE_CHECKING:
    from orbit.domain.ledger import Debt, Promise


person_tag = Table(
    "person_tag",
    Base.metadata,
    Column("person_id", ForeignKey("person.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True, index=True),
)

interaction_person = Table(
    "interaction_person",
    Base.metadata,
    Column("interaction_id", ForeignKey("interaction.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", ForeignKey("person.id", ondelete="CASCADE"), primary_key=True, index=True),
)

# Pairs the user marked as "not a duplicate"; stored with person_a_id < person_b_id.
not_duplicate = Table(
    "not_duplicate",
    Base.metadata,
    Column("person_a_id", ForeignKey("person.id", ondelete="CASCADE"), primary_key=True),
    Column("person_b_id", ForeignKey("person.id", ondelete="CASCADE"), primary_key=True),
    CheckConstraint("person_a_id < person_b_id", name="ordered"),
)


class Person(TimestampMixin, Base):
    __tablename__ = "person"
    __table_args__ = (
        CheckConstraint("(birthday_month IS NULL) = (birthday_day IS NULL)", name="birthday_pair"),
        CheckConstraint(
            "birthday_month IS NULL OR birthday_month BETWEEN 1 AND 12", name="birthday_month"
        ),
        CheckConstraint(
            "birthday_day IS NULL OR birthday_day BETWEEN 1 AND 31", name="birthday_day"
        ),
        CheckConstraint("cadence_days IS NULL OR cadence_days >= 1", name="cadence_days"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    # normalize_text(display_name + first_name + last_name), for search and matching.
    search_name_norm: Mapped[str] = mapped_column(String(600), index=True)
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    birthday_month: Mapped[int | None] = mapped_column(SmallInteger)
    birthday_day: Mapped[int | None] = mapped_column(SmallInteger)
    birthday_year: Mapped[int | None] = mapped_column(SmallInteger)
    # The SK default lives in PersonCreate: a column default would also replace an explicit None.
    nameday_country: Mapped[str | None] = mapped_column(String(2))
    # A calendar name picked by the user (e.g. "Marián" for "Majo"); overrides first_name for
    # name days. Always stored in the calendar's own spelling.
    nameday_name: Mapped[str | None] = mapped_column(String(100))
    # ISO 3166-1 alpha-2; the region for phone numbers written without a prefix.
    country: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(100))
    how_met: Mapped[str | None] = mapped_column(Text)
    met_through_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL"), index=True
    )
    photo_url: Mapped[str | None] = mapped_column(String(500))
    archived: Mapped[bool] = mapped_column(default=False)
    # Keep in touch every N days; overrides the cadence of the person's tags.
    cadence_days: Mapped[int | None]
    # Keep in touch: not due again before this day ("snooze"); None = not snoozed.
    snoozed_until: Mapped[date | None]
    # UID of the source vCard / iCloud contact; matched first on re-import.
    vcard_uid: Mapped[str | None] = mapped_column(String(255), unique=True)

    met_through: Mapped[Person | None] = relationship(remote_side=[id])
    aliases: Mapped[list[Alias]] = relationship(
        back_populates="person", cascade="all, delete-orphan", order_by="Alias.value"
    )
    handles: Mapped[list[Handle]] = relationship(
        back_populates="person", cascade="all, delete-orphan", order_by="Handle.id"
    )
    tags: Mapped[list[Tag]] = relationship(secondary=person_tag, order_by="Tag.name_norm")
    notes: Mapped[list[Note]] = relationship(
        back_populates="person", cascade="all, delete-orphan", order_by="Note.created_at.desc()"
    )
    interactions: Mapped[list[Interaction]] = relationship(
        secondary=interaction_person,
        back_populates="people",
        order_by="Interaction.occurred_on.desc()",
    )
    debts: Mapped[list[Debt]] = relationship(
        back_populates="person", cascade="all, delete-orphan", order_by="Debt.created_at"
    )
    promises: Mapped[list[Promise]] = relationship(
        back_populates="person", cascade="all, delete-orphan", order_by="Promise.created_at"
    )


class Alias(Base):
    __tablename__ = "alias"
    __table_args__ = (UniqueConstraint("person_id", "value_norm"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    value: Mapped[str] = mapped_column(String(100))
    value_norm: Mapped[str] = mapped_column(String(100), index=True)

    person: Mapped[Person] = relationship(back_populates="aliases")


class Handle(Base):
    __tablename__ = "handle"
    __table_args__ = (UniqueConstraint("type", "value_norm"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    type: Mapped[HandleType] = mapped_column(enum_column(HandleType, "handle_type"))
    value: Mapped[str] = mapped_column(String(255))
    value_norm: Mapped[str] = mapped_column(String(255))
    label: Mapped[str | None] = mapped_column(String(60))

    person: Mapped[Person] = relationship(back_populates="handles")


class Tag(Base):
    __tablename__ = "tag"
    __table_args__ = (
        CheckConstraint("cadence_days IS NULL OR cadence_days >= 1", name="cadence_days"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    name_norm: Mapped[str] = mapped_column(String(60), unique=True)
    # Keep in touch with people carrying this tag every N days (the tightest tag wins).
    cadence_days: Mapped[int | None]


class Note(TimestampMixin, Base):
    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    text_norm: Mapped[str] = mapped_column(Text)
    # The meeting it was dictated after, if any.
    meeting_id: Mapped[int | None] = mapped_column(
        ForeignKey("meeting.id", ondelete="SET NULL"), index=True
    )

    person: Mapped[Person] = relationship(back_populates="notes")


class Interaction(Base):
    __tablename__ = "interaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_on: Mapped[date] = mapped_column(index=True)
    kind: Mapped[InteractionKind] = mapped_column(enum_column(InteractionKind, "interaction_kind"))
    summary: Mapped[str | None] = mapped_column(Text)
    # The meeting it records (at most one interaction per meeting, from the post-meeting capture).
    meeting_id: Mapped[int | None] = mapped_column(
        ForeignKey("meeting.id", ondelete="SET NULL"), index=True
    )

    people: Mapped[list[Person]] = relationship(
        secondary=interaction_person, back_populates="interactions", order_by="Person.display_name"
    )


class MergeRecord(Base):
    """A merge of two people, with what's needed to undo it (see services/duplicates.py)."""

    __tablename__ = "merge_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    keep_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    # The merged (deleted) person's former id and name; no foreign key, the row is gone.
    merged_id: Mapped[int]
    merged_name: Mapped[str] = mapped_column(String(200))
    # Always reassign, never mutate in place (the JSON column doesn't track nested changes).
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    undone_at: Mapped[datetime | None]

    keep: Mapped[Person] = relationship()
