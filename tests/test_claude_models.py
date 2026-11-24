"""A model for each kind of work in the API path (SPEC §2.31)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from orbit.integrations.claude import (
    BriefLine,
    BriefRequest,
    ClaudeClient,
    MeetingPeople,
    ReportOutput,
    ReviewOutput,
    ReviewRequest,
    Usage,
)
from tests.test_claude import EMPTY, FakeMessages, request


def client_with(
    *outcomes: Any, seen: list[Usage] | None = None, **models: Any
) -> tuple[ClaudeClient, FakeMessages]:
    messages = FakeMessages(*outcomes)
    sdk = SimpleNamespace(messages=messages, close=lambda: None)
    sdk.with_options = lambda **_: sdk  # type: ignore[attr-defined]
    client = ClaudeClient(
        "key",
        "default-m",
        client=sdk,  # type: ignore[arg-type]
        on_usage=seen.append if seen is not None else None,
        **models,
    )
    return client, messages


def parsed(value: Any) -> SimpleNamespace:
    return SimpleNamespace(
        parsed_output=value,
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("meeting_link", "fast-m"),
        ("classification", "fast-m"),
        ("brief", "fast-m"),
        ("capture", "default-m"),
        ("draft", "default-m"),
        ("school_read", "default-m"),
        ("weekly_review", "deep-m"),
        ("report", "deep-m"),
    ],
)
def test_each_purpose_has_its_model(purpose: str, expected: str) -> None:
    client, _ = client_with(fast_model="fast-m", deep_model="deep-m")
    assert client.model_for(purpose) == expected


def test_without_other_models_everything_uses_the_default() -> None:
    client, _ = client_with()
    assert {client.model_for(p) for p in ("brief", "capture", "weekly_review")} == {"default-m"}


def test_a_capture_uses_the_default_model_and_no_thinking() -> None:
    client, messages = client_with(parsed(EMPTY), fast_model="fast-m", deep_model="deep-m")
    client.extract_capture(request())
    (call,) = messages.calls
    assert call["model"] == "default-m"
    assert "thinking" not in call and "output_config" not in call


def test_a_brief_uses_the_fast_model() -> None:
    client, messages = client_with(
        parsed(BriefLine(summary="x")), fast_model="fast-m", deep_model="deep-m"
    )
    client.summarize_brief(BriefRequest(meeting="kafe", today=date(2026, 10, 8), people=()))
    assert messages.calls[0]["model"] == "fast-m"


def test_the_review_and_reports_use_the_deep_model_with_adaptive_thinking() -> None:
    from tests.test_review import empty_output

    client, messages = client_with(parsed(empty_output()), deep_model="deep-m", deep_effort="high")
    day = date(2026, 10, 4)
    answer = client.weekly_review(
        ReviewRequest(
            today=day,
            review_week=(day, day),
            plan_week=(day, day),
            last_week_json="{}",
            next_week_json="{}",
        )
    )
    (call,) = messages.calls
    assert call["model"] == "deep-m" and answer.model == "deep-m"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"] == {"effort": "high"}
    assert isinstance(answer.output, ReviewOutput)
    assert ReportOutput and MeetingPeople  # the other deep and fast outputs exist


def test_the_deep_model_can_answer_without_thinking() -> None:
    from tests.test_review import empty_output

    client, messages = client_with(parsed(empty_output()), deep_model="deep-m")
    day = date(2026, 10, 4)
    client.weekly_review(
        ReviewRequest(
            today=day,
            review_week=(day, day),
            plan_week=(day, day),
            last_week_json="{}",
            next_week_json="{}",
        )
    )
    assert "thinking" not in messages.calls[0] and "output_config" not in messages.calls[0]


def test_usage_names_the_model_that_answered() -> None:
    seen: list[Usage] = []
    client, _ = client_with(parsed(BriefLine(summary="x")), seen=seen, fast_model="fast-m")
    client.summarize_brief(BriefRequest(meeting="kafe", today=date(2026, 10, 8), people=()))
    assert [(u.purpose, u.model) for u in seen] == [("brief", "fast-m")]


def test_settings_default_to_one_model_and_high_effort(make_settings: Any) -> None:
    settings = make_settings(anthropic_model="m")
    assert settings.anthropic_model_fast is None and settings.anthropic_model_deep is None
    assert settings.anthropic_deep_effort == "high"
    assert make_settings(anthropic_deep_effort="off").anthropic_deep_effort == "off"
