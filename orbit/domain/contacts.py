"""Contact sync with Apple Contacts: the link per person, each sync run and its changes."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, enum_column, utcnow
from orbit.domain.enums import SyncDirection, SyncKind, SyncRunStatus, SyncSource, SyncState

if TYPE_CHECKING:
    from orbit.domain.people import Person


class ContactLink(Base):
    """A person's Apple contact. `base_json` is what both sides had at the last sync (field key ->
    value): the base of the three-way diff. Always reassign, never mutate in place."""

    __tablename__ = "contact_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), unique=True)
    carddav_uid: Mapped[str] = mapped_column(String(255), unique=True)
    href: Mapped[str | None] = mapped_column(String(500))
    etag: Mapped[str | None] = mapped_column(String(255))
    base_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_synced_at: Mapped[datetime] = mapped_column(default=utcnow)


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[SyncSource] = mapped_column(enum_column(SyncSource, "sync_source"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    applied_at: Mapped[datetime | None]
    status: Mapped[SyncRunStatus] = mapped_column(
        enum_column(SyncRunStatus, "sync_run_status"), index=True
    )
    # Cards seen, people linked, conflicts from matching ...
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    changes: Mapped[list[SyncChange]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="SyncChange.id"
    )


class SyncChange(Base):
    __tablename__ = "sync_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_run.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL"), index=True
    )
    person_name: Mapped[str] = mapped_column(String(200))
    # "name", "birthday", "phone:+447700900123", "social:instagram:pato" ... or "new_contact".
    field: Mapped[str] = mapped_column(String(255))
    direction: Mapped[SyncDirection] = mapped_column(enum_column(SyncDirection, "sync_direction"))
    kind: Mapped[SyncKind] = mapped_column(enum_column(SyncKind, "sync_kind"))
    state: Mapped[SyncState] = mapped_column(enum_column(SyncState, "sync_state"), index=True)
    # Pull: old = the Orbit value, new = the Apple value. Push: old = Apple, new = Orbit.
    # A conflict: old = Orbit, new = Apple.
    old_json: Mapped[Any] = mapped_column(JSON)
    new_json: Mapped[Any] = mapped_column(JSON)
    # The link's base value for this field before the change was applied: {"present", "value"}.
    base_old_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # What I chose when applying: "apple" / "orbit" / "link" / "new" ...
    choice: Mapped[str | None] = mapped_column(String(20))
    # sha256 of (person, field, direction, new value): a rejected change is not proposed again.
    reject_hash: Mapped[str] = mapped_column(String(64), index=True)
    applied_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[SyncRun] = relationship(back_populates="changes")


class ContactBackup(Base):
    """An Apple card as it was just before Orbit wrote to it, so the write can be put back."""

    __tablename__ = "contact_backup"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_run.id", ondelete="SET NULL"), index=True
    )
    href: Mapped[str] = mapped_column(String(500))
    vcard_text: Mapped[str] = mapped_column(Text)
    # The card's version right after our write; a restore only goes ahead while it still matches.
    etag_after: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    restored_at: Mapped[datetime | None]

    person: Mapped[Person] = relationship()
