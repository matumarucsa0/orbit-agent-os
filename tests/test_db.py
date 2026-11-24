from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import String, select, text, update
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from orbit.db import make_engine, make_sessionmaker
from orbit.domain.base import TimestampMixin, UTCDateTime


# A throwaway model on its own metadata, so the app's Base.metadata stays clean for Alembic.
class _TestBase(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime()}  # noqa: RUF012 (SQLAlchemy idiom)


class Thing(TimestampMixin, _TestBase):
    __tablename__ = "thing"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    happened_at: Mapped[datetime | None]


@pytest.fixture
def session() -> Session:
    engine = make_engine("sqlite://")
    _TestBase.metadata.create_all(engine)
    with make_sessionmaker(engine)() as session:
        yield session
    engine.dispose()


def test_timestamps_set_on_insert_and_update(session: Session) -> None:
    thing = Thing(name="charger")
    session.add(thing)
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Thing)).one()
    assert loaded.created_at.tzinfo is UTC
    assert loaded.updated_at.tzinfo is UTC
    assert abs(loaded.created_at - datetime.now(UTC)) < timedelta(seconds=5)
    created = loaded.created_at

    # Backdate explicitly (an explicit value bypasses onupdate) so the check below doesn't
    # depend on the OS clock ticking between two fast commits.
    an_hour_ago = created - timedelta(hours=1)
    session.execute(update(Thing).values(updated_at=an_hour_ago))
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Thing)).one()
    assert loaded.updated_at == an_hour_ago
    loaded.name = "charger (USB-C)"
    session.commit()
    session.expire_all()

    reloaded = session.scalars(select(Thing)).one()
    assert reloaded.created_at == created
    assert reloaded.updated_at >= created


def test_aware_datetimes_round_trip_as_utc(session: Session) -> None:
    prague_summer = timezone(timedelta(hours=2))
    session.add(Thing(name="x", happened_at=datetime(2026, 7, 1, 20, 0, tzinfo=prague_summer)))
    session.commit()
    session.expire_all()

    loaded = session.scalars(select(Thing)).one()
    assert loaded.happened_at == datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
    assert loaded.happened_at.tzinfo is UTC


def test_naive_datetime_rejected(session: Session) -> None:
    session.add(Thing(name="x", happened_at=datetime(2026, 7, 1, 20, 0)))
    with pytest.raises(StatementError, match="naive datetime"):
        session.commit()


def test_sqlite_foreign_keys_enabled(session: Session) -> None:
    assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
