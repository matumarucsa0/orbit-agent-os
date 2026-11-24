from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anthropic
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit import cli
from orbit.domain.usage import ClaudeCall
from orbit.integrations.claude import CaptureExtraction, ClaudeClient, ClaudeError, Usage
from orbit.jobs.scheduler import flush_usage
from orbit.services import reminders, usage
from tests.test_claude import EMPTY, FakeMessages, request

PRAGUE = ZoneInfo("Europe/Prague")


def reporting_client(*outcomes: object) -> tuple[ClaudeClient, list[Usage]]:
    seen: list[Usage] = []
    sdk = SimpleNamespace(messages=FakeMessages(*outcomes), close=lambda: None)
    client = ClaudeClient("key", "model-x", client=sdk, on_usage=seen.append)  # type: ignore[arg-type]
    return client, seen


def with_usage(parsed: object, tokens_in: int, tokens_out: int, stop: str = "end_turn") -> object:
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
    )


def test_client_reports_every_call() -> None:
    try:
        CaptureExtraction.model_validate({})
    except ValidationError as exc:
        invalid = exc
    client, seen = reporting_client(invalid, with_usage(EMPTY, 1200, 300))
    client.extract_capture(request())
    assert [replace(u, latency_ms=None) for u in seen] == [
        Usage("capture", "model-x", 0, 0, ok=False, prompt_version="capture@1"),
        Usage("capture", "model-x", 1200, 300, ok=True, prompt_version="capture@1"),
    ]
    assert all(u.latency_ms is not None for u in seen)


def test_client_reports_failures() -> None:
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client, seen = reporting_client(error)
    with pytest.raises(ClaudeError):
        client.extract_capture(request())
    assert [replace(u, latency_ms=None) for u in seen] == [
        Usage("capture", "model-x", 0, 0, ok=False, prompt_version="capture@1")
    ]

    client, seen = reporting_client(with_usage(None, 50, 0, "refusal"))
    with pytest.raises(ClaudeError, match="declined"):
        client.extract_capture(request())
    assert [replace(u, latency_ms=None) for u in seen] == [
        Usage("capture", "model-x", 50, 0, ok=False, prompt_version="capture@1")
    ]


def test_a_broken_usage_callback_never_breaks_the_call() -> None:
    def explode(_usage: Usage) -> None:
        raise RuntimeError("db down")

    sdk = SimpleNamespace(messages=FakeMessages(with_usage(EMPTY, 1, 1)), close=lambda: None)
    client = ClaudeClient("key", "m", client=sdk, on_usage=explode)  # type: ignore[arg-type]
    assert client.extract_capture(request()) is EMPTY


def test_log_buffers_until_flushed(db: Session) -> None:
    log = usage.UsageLog()
    log.add(Usage("capture", "m", 100, 10, ok=True))
    log.add(Usage("draft", "m", 50, 5, ok=False))
    assert db.scalars(select(ClaudeCall)).all() == []
    assert log.flush(db) == 2
    assert log.flush(db) == 0
    calls = db.scalars(select(ClaudeCall).order_by(ClaudeCall.id)).all()
    assert [(c.purpose, c.input_tokens, c.output_tokens, c.ok) for c in calls] == [
        ("capture", 100, 10, True),
        ("draft", 50, 5, False),
    ]


def call(db: Session, at: datetime, purpose: str, tokens_in: int, ok: bool = True) -> None:
    db.add(
        ClaudeCall(
            created_at=at,
            purpose=purpose,
            model="m",
            input_tokens=tokens_in,
            output_tokens=tokens_in // 10,
            ok=ok,
        )
    )
    db.flush()


def test_totals_by_local_month_and_purpose(db: Session) -> None:
    call(db, datetime(2026, 8, 31, 22, 30, tzinfo=UTC), "capture", 100)  # 1 Sep in Prague
    call(db, datetime(2026, 8, 31, 21, 0, tzinfo=UTC), "capture", 999)  # 31 Aug 23:00 local
    call(db, datetime(2026, 9, 10, tzinfo=UTC), "capture", 200, ok=False)
    call(db, datetime(2026, 9, 11, tzinfo=UTC), "brief", 1000)
    rows = usage.totals(db, date(2026, 8, 1), date(2026, 9, 24), PRAGUE)
    assert [(r.month, r.purpose, r.calls, r.failed, r.input_tokens) for r in rows] == [
        (date(2026, 8, 1), "capture", 1, 0, 999),
        (date(2026, 9, 1), "brief", 1, 0, 1000),
        (date(2026, 9, 1), "capture", 2, 1, 300),
    ]
    summary = usage.month_summary(db, date(2026, 9, 24), PRAGUE)
    assert (summary.calls, summary.input_tokens, summary.output_tokens) == (3, 1300, 130)


def test_months_back_and_token_format() -> None:
    assert usage.months_back(date(2026, 2, 14), 0) == date(2026, 2, 1)
    assert usage.months_back(date(2026, 2, 14), 2) == date(2025, 12, 1)
    assert usage.months_back(date(2026, 1, 31), 13) == date(2024, 12, 1)
    assert [usage.format_tokens(n) for n in (999, 1234, 2_500_000)] == ["999", "1.2k", "2.5M"]


def test_digest_line(db: Session) -> None:
    sunday = datetime(2026, 9, 27, 16, 0, tzinfo=UTC)
    empty = reminders.build_digest(db, sunday, PRAGUE)
    assert reminders.claude_line(empty) is None
    call(db, datetime(2026, 9, 10, tzinfo=UTC), "capture", 12_000)
    digest = reminders.build_digest(db, sunday, PRAGUE)
    line = "🤖 Claude in September: 1 call, 12.0k in / 1.2k out tokens"
    assert reminders.claude_line(digest) == line
    body = reminders.digest_push(digest, "https://o.x", PRAGUE).body
    assert body.endswith(line)


def test_scheduler_flush_and_shutdown(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    log: usage.UsageLog = app.state.usage_log
    log.add(Usage("brief", "m", 10, 1, ok=True))
    assert flush_usage(app.state.sessionmaker, log) == 1
    log.add(Usage("draft", "m", 10, 1, ok=True))
    monkeypatch.setattr(app.state.engine, "dispose", lambda: None)  # keep the in-memory DB
    with TestClient(app):
        pass  # the lifespan's shutdown writes what's left
    with app.state.sessionmaker() as session:
        assert len(session.scalars(select(ClaudeCall)).all()) == 2


def test_flush_errors_are_swallowed(app: FastAPI) -> None:
    def broken() -> Session:
        raise RuntimeError("db down")

    log = usage.UsageLog()
    log.add(Usage("brief", "m", 1, 1, ok=True))
    assert flush_usage(broken, log) == 0  # type: ignore[arg-type]


def test_cli_usage(app: FastAPI, monkeypatch: pytest.MonkeyPatch, capsys, settings) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "make_engine", lambda _url: app.state.engine)
    monkeypatch.setattr(app.state.engine, "dispose", lambda: None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["usage"]) == 0
    assert "No Claude calls recorded." in capsys.readouterr().out
    with app.state.sessionmaker() as session:
        call(session, datetime.now(UTC), "capture", 2000)
        session.commit()
    assert cli.main(["usage", "--months", "1"]) == 0
    out = capsys.readouterr().out
    assert "capture" in out and "2.0k" in out
