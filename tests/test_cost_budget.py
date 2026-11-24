"""Cost estimates, the monthly budget guard and its pushes (W5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orbit.db import make_engine, make_sessionmaker
from orbit.domain.inbox import Notification
from orbit.domain.usage import ClaudeCall
from orbit.integrations.claude import BudgetExceeded, ClaudeClient, ClaudeError, Usage
from orbit.integrations.fake_claude import FakeClaude
from orbit.integrations.ntfy import PushMessage
from orbit.integrations.pricing import cost_micro_eur, format_eur, price_eur_per_mtok
from orbit.jobs.scheduler import flush_usage
from orbit.services import budget, capture, usage
from orbit.services.notifications import QuietHours
from tests.conftest import TEST_DATABASE_URL, reset_schema
from tests.test_claude import EMPTY, FakeMessages, request
from tests.test_usage import with_usage

PRAGUE = ZoneInfo("Europe/Prague")
QUIET = QuietHours(time(22, 0), time(8, 0), PRAGUE)
NOON = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


class Sender:
    def __init__(self) -> None:
        self.sent: list[PushMessage] = []

    def send(self, message: PushMessage) -> None:
        self.sent.append(message)


# --- Prices ---


def test_family_prices_convert_with_the_rate() -> None:
    assert price_eur_per_mtok("claude-haiku-4-5", 1.0) == (1.0, 5.0)
    assert price_eur_per_mtok("claude-sonnet-4-6", 0.5) == (1.5, 7.5)
    assert price_eur_per_mtok("claude-opus-4-5", 1.0) == (5.0, 25.0)


def test_unknown_model_is_priced_like_sonnet_and_free_models_cost_nothing() -> None:
    assert price_eur_per_mtok("mystery-model", 1.0) == (3.0, 15.0)
    assert price_eur_per_mtok("fake", 1.0) == (0.0, 0.0)
    assert price_eur_per_mtok("claude-code", 1.0) == (0.0, 0.0)


def test_override_by_exact_name_wins() -> None:
    assert price_eur_per_mtok("claude-haiku-4-5", 0.9, {"claude-haiku-4-5": (2.0, 9.0)}) == (
        2.0,
        9.0,
    )


def test_cost_in_micro_euro() -> None:
    # 1M input tokens of sonnet at rate 1.0 is 3 euros = 3_000_000 micro-euros.
    assert cost_micro_eur("sonnet", 1_000_000, 0, 1.0) == 3_000_000
    assert cost_micro_eur("sonnet", 1_200, 300, 1.0) == 1_200 * 3 + 300 * 15
    assert cost_micro_eur("sonnet", 0, 0, 0.92) == 0
    assert format_eur(1_234_567) == "€1.23"
    assert format_eur(1_234) == "€0.0012"


# --- The client prices and reports every call ---


def priced_client(*outcomes: object, guard=None) -> tuple[ClaudeClient, list[Usage], FakeMessages]:
    seen: list[Usage] = []
    messages = FakeMessages(*outcomes)
    sdk = SimpleNamespace(messages=messages, close=lambda: None)
    client = ClaudeClient(
        "key",
        "claude-sonnet-x",
        client=sdk,  # type: ignore[arg-type]
        on_usage=seen.append,
        pricer=lambda model, i, o: cost_micro_eur(model, i, o, 1.0),
        budget_guard=guard,
    )
    return client, seen, messages


def test_client_attaches_cost_to_each_call() -> None:
    client, seen, _ = priced_client(with_usage(EMPTY, 1_000_000, 100_000))
    client.extract_capture(request())
    assert seen[0].cost_micro_eur == 3_000_000 + 1_500_000


def test_flush_stores_cost_latency_and_version(db: Session) -> None:
    log = usage.UsageLog()
    log.add(
        Usage(
            "capture",
            "m",
            1,
            1,
            ok=True,
            prompt_version="capture@1",
            latency_ms=420,
            cost_micro_eur=77,
        )
    )
    log.flush(db)
    row = db.scalars(select(ClaudeCall)).one()
    assert (row.latency_ms, row.prompt_version, row.cost_micro_eur) == (420, "capture@1", 77)


def test_totals_and_month_cost(db: Session) -> None:
    for day, cost in (
        (date(2026, 9, 2), 1_000_000),
        (date(2026, 9, 20), 2_500_000),
        (date(2026, 8, 30), 9),
    ):
        db.add(
            ClaudeCall(
                created_at=datetime.combine(day, time(12), tzinfo=UTC),
                purpose="capture",
                model="m",
                cost_micro_eur=cost,
            )
        )
    db.flush()
    assert usage.month_cost_micro_eur(db, date(2026, 9, 24), PRAGUE) == 3_500_000
    assert usage.month_summary(db, date(2026, 9, 24), PRAGUE).cost_micro_eur == 3_500_000
    rows = usage.totals(db, date(2026, 8, 1), date(2026, 9, 1), PRAGUE)
    assert [(r.month.month, r.cost_micro_eur) for r in rows] == [(8, 9), (9, 3_500_000)]


# --- The guard ---


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = make_engine(TEST_DATABASE_URL)
    reset_schema(engine)
    return make_sessionmaker(engine)


def spend(sessions: sessionmaker[Session], micro: int, at: datetime = NOON) -> None:
    with sessions() as session:
        session.add(ClaudeCall(created_at=at, purpose="capture", model="m", cost_micro_eur=micro))
        session.commit()


def test_guard_allows_below_budget_and_stops_at_it(sessions: sessionmaker[Session]) -> None:
    log = usage.UsageLog()
    guard = budget.BudgetGuard(sessions, log, 10.0, PRAGUE, clock=lambda: NOON)
    spend(sessions, 9_999_999)
    guard()  # just below: allowed
    spend(sessions, 1)
    with pytest.raises(BudgetExceeded, match="budget"):
        guard()


def test_guard_counts_calls_still_in_the_buffer(sessions: sessionmaker[Session]) -> None:
    log = usage.UsageLog()
    guard = budget.BudgetGuard(sessions, log, 1.0, PRAGUE, clock=lambda: NOON)
    log.add(Usage("capture", "m", 1, 1, ok=True, cost_micro_eur=1_000_000))
    with pytest.raises(BudgetExceeded):
        guard()


def test_guard_resets_in_a_new_month(sessions: sessionmaker[Session]) -> None:
    log = usage.UsageLog()
    spend(sessions, 5_000_000, at=datetime(2026, 9, 23, 10, tzinfo=UTC))
    october = datetime(2026, 10, 1, 10, tzinfo=UTC)
    budget.BudgetGuard(sessions, log, 1.0, PRAGUE, clock=lambda: october)()  # no raise
    with pytest.raises(BudgetExceeded):
        budget.BudgetGuard(sessions, log, 1.0, PRAGUE, clock=lambda: NOON)()


def test_zero_budget_means_no_limit(sessions: sessionmaker[Session]) -> None:
    spend(sessions, 999_000_000)
    budget.BudgetGuard(sessions, usage.UsageLog(), 0, PRAGUE, clock=lambda: NOON)()


def test_guard_fails_open_when_the_database_is_unreadable() -> None:
    def broken() -> Session:
        raise RuntimeError("db down")

    guard = budget.BudgetGuard(broken, usage.UsageLog(), 1.0, PRAGUE)  # type: ignore[arg-type]
    guard()


def test_api_client_sends_nothing_once_the_budget_is_used_up(
    sessions: sessionmaker[Session],
) -> None:
    spend(sessions, 2_000_000, at=datetime.now(UTC))
    guard = budget.BudgetGuard(sessions, usage.UsageLog(), 1.0, PRAGUE)
    client, seen, messages = priced_client(with_usage(EMPTY, 1, 1), guard=guard)
    with pytest.raises(BudgetExceeded):
        client.extract_capture(request())
    assert messages.calls == [] and seen == []
    assert issubclass(BudgetExceeded, ClaudeError)  # every caller's fallback already handles it


def test_capture_falls_back_to_a_note_draft_when_over_budget(db: Session) -> None:
    def over() -> None:
        raise BudgetExceeded("used up")

    sdk = SimpleNamespace(messages=FakeMessages(), close=lambda: None)
    client = ClaudeClient("k", "m", client=sdk, budget_guard=over)  # type: ignore[arg-type]
    result = capture.capture(
        db, client, "Lent Pato 10 euro", today=date(2026, 9, 23), default_currency="EUR", tz=PRAGUE
    )
    assert result.proposals  # the note draft, as without a key
    assert all(p.prompt_version is None for p in result.proposals)


def test_fake_backend_is_never_blocked() -> None:
    fake = FakeClaude(on_usage=lambda u: None)
    assert getattr(fake, "_budget_guard", None) is None  # it costs nothing, so there is no guard
    fake.extract_capture(request())


# --- Pushes ---


def test_alerts_at_80_and_100_percent_once_per_month(db: Session) -> None:
    sender = Sender()
    db.add(ClaudeCall(created_at=NOON, purpose="capture", model="m", cost_micro_eur=7_000_000))
    db.flush()
    assert budget.check_alerts(db, sender, 10.0, PRAGUE, QUIET, now=NOON) == []
    db.add(ClaudeCall(created_at=NOON, purpose="capture", model="m", cost_micro_eur=1_000_000))
    db.flush()
    assert budget.check_alerts(db, sender, 10.0, PRAGUE, QUIET, now=NOON) == [
        "claude-budget-80:2026-09"
    ]
    assert budget.check_alerts(db, sender, 10.0, PRAGUE, QUIET, now=NOON) == []  # deduped
    assert len(sender.sent) == 1 and "80 percent" in sender.sent[0].title
    db.add(ClaudeCall(created_at=NOON, purpose="capture", model="m", cost_micro_eur=2_000_000))
    db.flush()
    assert budget.check_alerts(db, sender, 10.0, PRAGUE, QUIET, now=NOON) == [
        "claude-budget-100:2026-09"
    ]
    assert len(sender.sent) == 2
    # next month: spend is 0 again, nothing to say
    october = datetime(2026, 10, 2, 10, tzinfo=UTC)
    assert budget.check_alerts(db, sender, 10.0, PRAGUE, QUIET, now=october) == []
    keys = {n.dedupe_key for n in db.scalars(select(Notification))}
    assert keys == {"claude-budget-80:2026-09", "claude-budget-100:2026-09"}


def test_jumping_past_100_sends_both_once(db: Session) -> None:
    sender = Sender()
    db.add(ClaudeCall(created_at=NOON, purpose="capture", model="m", cost_micro_eur=20_000_000))
    db.flush()
    assert len(budget.check_alerts(db, sender, 10.0, PRAGUE, QUIET, now=NOON)) == 2


def test_no_alerts_without_a_budget(db: Session) -> None:
    db.add(ClaudeCall(created_at=NOON, purpose="capture", model="m", cost_micro_eur=20_000_000))
    db.flush()
    assert budget.check_alerts(db, Sender(), 0, PRAGUE, QUIET, now=NOON) == []


def test_flush_usage_sends_the_alert(make_settings, sessions: sessionmaker[Session]) -> None:
    log = usage.UsageLog()
    log.add(Usage("capture", "m", 1, 1, ok=True, cost_micro_eur=9_000_000))
    sender = Sender()
    settings = make_settings(claude_monthly_budget_eur=10, quiet_hours="22:00-22:00")
    flush_usage(sessions, log, sender, settings)  # type: ignore[arg-type]
    assert len(sender.sent) == 1


# --- The page ---


def test_ops_page_shows_cost_and_budget(web: TestClient, app) -> None:  # type: ignore[no-untyped-def]
    with app.state.sessionmaker() as session:
        session.add(
            ClaudeCall(
                created_at=datetime.now(UTC), purpose="capture", model="m", cost_micro_eur=1_500_000
            )
        )
        session.commit()
    page = web.get("/ops/claude").text
    assert "€1.50" in page and "total (estimate)" in page and "estimates" in page
