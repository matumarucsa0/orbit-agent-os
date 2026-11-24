from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from orbit.db import make_engine, make_sessionmaker
from orbit.domain import Base, Debt, Notification, Person, Proposal, Tag
from orbit.domain.enums import DebtDirection, DebtType, ProposalKind, ProposalStatus
from orbit.services.db_copy import copy_database
from orbit.services.errors import InvalidInputError
from tests.conftest import TEST_DATABASE_URL, reset_schema
from tests.test_migrations import alembic_config

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def stamped_engine(url: str, revision: str = "head") -> Engine:
    """An engine on an empty Orbit schema, recorded as migrated to `revision`."""
    engine = make_engine(url)
    reset_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    command.stamp(alembic_config(url), revision)
    return engine


@pytest.fixture
def source(tmp_path: Path) -> Iterator[Engine]:
    engine = stamped_engine(f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    yield engine
    engine.dispose()


@pytest.fixture
def target(tmp_path: Path) -> Iterator[Engine]:
    # Postgres when the suite runs on it (CI): the real production direction.
    url = (
        f"sqlite:///{(tmp_path / 'target.db').as_posix()}"
        if TEST_DATABASE_URL.startswith("sqlite")
        else TEST_DATABASE_URL
    )
    engine = stamped_engine(url)
    yield engine
    engine.dispose()


def person(name: str, **fields: object) -> Person:
    return Person(display_name=name, search_name_norm=name.lower(), archived=False, **fields)


def seed(engine: Engine) -> None:
    with make_sessionmaker(engine)() as session:
        pato = person("Paťo")
        terka = person("Terka", birthday_month=2, birthday_day=29)
        session.add_all([pato, terka])
        session.flush()
        # Points at a later row of the same table.
        pato.met_through_id = terka.id
        pato.tags.append(Tag(name="Close", name_norm="close"))
        original = Debt(
            person=pato,
            direction=DebtDirection.THEY_OWE_ME,
            type=DebtType.MONEY,
            amount_minor=1200,
            currency="EUR",
            description="pizza",
            reference_code="D-0001",
            settled_at=NOW,
        )
        session.add(original)
        session.flush()
        session.add(
            Debt(
                person=pato,
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.MONEY,
                amount_minor=700,
                currency="EUR",
                description="pizza",
                reference_code="D-0002",
                parent_id=original.id,
            )
        )
        session.add(
            Proposal(
                kind=ProposalKind.CAPTURE,
                status=ProposalStatus.PENDING,
                summary="Note for Paťo",
                payload_json={"items": [{"text": "sľúbil som mu rozvrh"}]},
                source_type="capture",
            )
        )
        session.add(
            Notification(
                dedupe_key="test:1",
                kind="test",
                title="t",
                body="b",
                payload_json={"title": "t"},
                not_before=NOW,
                sent_at=NOW,
                delivered=True,
            )
        )
        session.commit()


def test_copies_every_row(source: Engine, target: Engine) -> None:
    seed(source)

    counts = copy_database(source, target)

    assert counts["person"] == 2
    assert counts["debt"] == 2
    assert counts["person_tag"] == 1
    assert counts["alias"] == 0
    with Session(target) as session:
        pato = session.scalars(select(Person).where(Person.display_name == "Paťo")).one()
        assert pato.met_through is not None and pato.met_through.display_name == "Terka"
        assert [t.name for t in pato.tags] == ["Close"]
        remainder = session.scalars(select(Debt).where(Debt.reference_code == "D-0002")).one()
        assert remainder.parent is not None and remainder.parent.settled_at == NOW
        proposal = session.scalars(select(Proposal)).one()
        assert proposal.payload_json == {"items": [{"text": "sľúbil som mu rozvrh"}]}
        assert session.scalars(select(Notification)).one().delivered


def test_new_rows_after_a_copy_get_fresh_ids(source: Engine, target: Engine) -> None:
    seed(source)
    copy_database(source, target)

    with Session(target) as session:
        newcomer = person("Newcomer")
        session.add(newcomer)
        session.commit()
        assert newcomer.id == 3


def test_refuses_a_target_with_data(source: Engine, target: Engine) -> None:
    seed(source)
    with Session(target) as session:
        session.add(person("Already here"))
        session.commit()

    with pytest.raises(InvalidInputError, match="isn't empty"):
        copy_database(source, target)


def test_refuses_different_revisions(source: Engine, tmp_path: Path) -> None:
    older = stamped_engine(f"sqlite:///{(tmp_path / 'older.db').as_posix()}", "0004")
    with pytest.raises(InvalidInputError, match="revisions differ"):
        copy_database(source, older)
    older.dispose()


def test_refuses_a_target_without_schema(source: Engine) -> None:
    bare = make_engine("sqlite://")
    Base.metadata.create_all(bare)
    with pytest.raises(InvalidInputError, match="alembic upgrade head"):
        copy_database(source, bare)
    bare.dispose()
