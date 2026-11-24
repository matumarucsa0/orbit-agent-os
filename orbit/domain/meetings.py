"""Calendar meetings (synced from iCloud) and the people they're with."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, TimestampMixin, enum_column
from orbit.domain.enums import MeetingLinkSource
from orbit.domain.people import Person


class Meeting(TimestampMixin, Base):
    """One calendar event (one occurrence of a recurring one), upserted by `calendar_uid`.

    Nothing is ever deleted: an event that was cancelled or vanished from the calendar is kept
    with `cancelled` set, and comes back if it reappears.
    """

    __tablename__ = "meeting"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The event UID; "<UID>#<original start, UTC>" for an occurrence of a recurring event.
    calendar_uid: Mapped[str] = mapped_column(String(500), unique=True)
    calendar_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    start_utc: Mapped[datetime] = mapped_column(index=True)
    end_utc: Mapped[datetime]
    cancelled: Mapped[bool] = mapped_column(default=False)
    # What the automatic people links were worked out from (title, location, attendee emails);
    # links are only redone when it changes, so a manual unlink sticks.
    link_key: Mapped[str | None] = mapped_column(String(64))
    brief_sent_at: Mapped[datetime | None]
    capture_sent_at: Mapped[datetime | None]
    # The optional Claude one-liner ("what's new with them"), written when the brief goes out.
    brief_summary: Mapped[str | None] = mapped_column(Text)

    links: Mapped[list[MeetingPerson]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", order_by="MeetingPerson.person_id"
    )

    @property
    def people(self) -> list[Person]:
        return sorted((link.person for link in self.links), key=lambda p: p.search_name_norm)


class MeetingPerson(Base):
    __tablename__ = "meeting_person"

    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meeting.id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("person.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    link_source: Mapped[MeetingLinkSource] = mapped_column(
        enum_column(MeetingLinkSource, "meeting_link_source")
    )

    meeting: Mapped[Meeting] = relationship(back_populates="links")
    person: Mapped[Person] = relationship()
