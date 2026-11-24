"""`seed-demo`: the fictional persona, idempotent, and no network (DECISIONS #35)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from orbit import cli
from orbit.db import make_engine, make_sessionmaker
from orbit.domain.bank import Transaction
from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    HandleType,
    ProposalKind,
    ProposalStatus,
    ReviewStatus,
)
from orbit.domain.fitness import Workout
from orbit.domain.health import HealthDay
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt
from orbit.domain.meetings import Meeting
from orbit.domain.people import Handle, Person
from orbit.domain.review import WeeklyReview
from orbit.domain.usage import ClaudeCall
from orbit.services import demo
from orbit.services.errors import InvalidInputError
from orbit.services.people import PersonCreate, create_person
from tests.conftest import TEST_DATABASE_URL, reset_schema

TODAY = date(2026, 11, 24)
TZ = ZoneInfo("Europe/Prague")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("seed-demo must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = make_engine(TEST_DATABASE_URL)
    reset_schema(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


def count(sessions: sessionmaker[Session], model: type) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_seeds_the_persona(sessions: sessionmaker[Session]) -> None:
    report = demo.seed_demo(sessions, today=TODAY, tz=TZ)
    assert not report.already_seeded
    assert 8 <= count(sessions, Person) <= 12
    assert count(sessions, Meeting) >= 8
    assert count(sessions, Workout) >= 6
    assert count(sessions, HealthDay) == 30
    assert count(sessions, Transaction) >= 30
    with sessions() as session:
        debts = list(session.scalars(select(Debt)))
        assert {d.type for d in debts} == {DebtType.MONEY, DebtType.ITEM, DebtType.FAVOR}
        assert {d.direction for d in debts} == {DebtDirection.THEY_OWE_ME, DebtDirection.I_OWE_THEM}
        assert any(d.is_open and d.due and d.due < TODAY for d in debts)
        assert any(not d.is_open for d in debts)
        kinds = {p.kind for p in session.scalars(select(Proposal))}
        assert {
            ProposalKind.CAPTURE,
            ProposalKind.SPLIT_TRANSACTION,
            ProposalKind.PAYMENT_MATCH,
            ProposalKind.GOAL_RERANK,
            ProposalKind.FOLLOW_UP,
        } <= kinds
        assert all(p.status is ProposalStatus.PENDING for p in session.scalars(select(Proposal)))
        review = session.scalars(select(WeeklyReview)).one()
        assert review.status is ReviewStatus.READY and review.model == "fake"
        calls = list(session.scalars(select(ClaudeCall)))
        assert {c.purpose for c in calls} >= {"capture", "classification", "weekly_review"}
        assert {(c.model, c.input_tokens, c.output_tokens) for c in calls} == {("fake", 0, 0)}


def test_everything_is_fictional(sessions: sessionmaker[Session]) -> None:
    demo.seed_demo(sessions, today=TODAY, tz=TZ)
    with sessions() as session:
        for handle in session.scalars(select(Handle)):
            if handle.type is HandleType.IBAN:
                assert set(handle.value_norm[2:]) == {"0"} or set(handle.value_norm[4:-2]) == {"0"}
            if handle.type is HandleType.EMAIL:
                assert handle.value.endswith("@example.com")
            if handle.type is HandleType.PHONE:
                assert handle.value.startswith("+44 7700 900")
        for tx in session.scalars(select(Transaction)):
            iban = tx.counterparty_iban
            assert iban is None or iban.startswith("SK00000000000000")


def test_is_deterministic(sessions: sessionmaker[Session]) -> None:
    demo.seed_demo(sessions, today=TODAY, tz=TZ)
    with sessions() as session:
        first = sorted(
            (
                t.amount_minor,
                t.counterparty or "",
                t.classification.value if t.classification else "",
            )
            for t in session.scalars(select(Transaction))
        )
    engine = make_engine(TEST_DATABASE_URL)
    reset_schema(engine)
    other = make_sessionmaker(engine)
    demo.seed_demo(other, today=TODAY, tz=TZ)
    with other() as session:
        second = sorted(
            (
                t.amount_minor,
                t.counterparty or "",
                t.classification.value if t.classification else "",
            )
            for t in session.scalars(select(Transaction))
        )
    engine.dispose()
    assert first == second


def test_second_run_changes_nothing(sessions: sessionmaker[Session]) -> None:
    demo.seed_demo(sessions, today=TODAY, tz=TZ)
    before = {m: count(sessions, m) for m in (Person, Debt, Transaction, Proposal, ClaudeCall)}
    report = demo.seed_demo(sessions, today=TODAY, tz=TZ)
    assert report.already_seeded
    assert before == {m: count(sessions, m) for m in before}


def test_refuses_a_database_with_other_data(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        create_person(session, PersonCreate(display_name="Someone Real"))
        session.commit()
    with pytest.raises(demo.NotEmptyError):
        demo.seed_demo(sessions, today=TODAY, tz=TZ)
    assert issubclass(demo.NotEmptyError, InvalidInputError)
    assert count(sessions, Person) == 1


def test_force_replaces_the_data(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        create_person(session, PersonCreate(display_name="Someone Real"))
        session.commit()
    demo.seed_demo(sessions, today=TODAY, tz=TZ, force=True)
    with sessions() as session:
        assert session.scalar(select(Person).where(Person.display_name == "Someone Real")) is None
    assert count(sessions, Person) == len(demo.FRIENDS)


def test_cli_seed_demo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from orbit.settings import get_settings
    from tests.conftest import TEST_API_TOKEN

    url = f"sqlite:///{tmp_path}/demo.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ORBIT_API_TOKEN", TEST_API_TOKEN)
    monkeypatch.setenv("ORBIT_SECRET_KEY", "s" * 40)
    monkeypatch.setenv("ICS_TOKEN", "i" * 40)
    monkeypatch.setenv("ORBIT_WEB_PASSWORD_HASH", "$argon2id$x")
    get_settings.cache_clear()
    try:
        engine = make_engine(url)
        reset_schema(engine)
        engine.dispose()
        assert cli.main(["seed-demo"]) == 0
        assert "ORBIT_CLAUDE_MODE=fake" in capsys.readouterr().out
        assert cli.main(["seed-demo"]) == 0
        assert "already there" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_home_and_pages_render_with_demo_data(web: TestClient, app: FastAPI) -> None:
    """The demo data reaches rows (an old lent item on Home) that plain fixtures never did."""
    demo.seed_demo(app.state.sessionmaker, today=date.today(), tz=TZ)
    for path in ("/", "/attention", "/people", "/ledger", "/inbox", "/reviews", "/ops/claude"):
        response = web.get(path)
        assert response.status_code == 200, path
    assert "lent" in web.get("/").text
    assert "Usage" in web.get("/ops/claude").text
