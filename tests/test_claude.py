from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from orbit.domain.enums import Classification
from orbit.integrations.claude import (
    CandidatePerson,
    CaptureExtraction,
    CaptureRequest,
    ClassifiedTransaction,
    ClaudeClient,
    ClaudeError,
    TransactionClassification,
    TransactionRow,
    build_capture_prompt,
    build_classification_prompt,
    mask_sensitive,
)

EMPTY = CaptureExtraction(
    notes=[], interactions=[], debts=[], promises=[], unknown_people=[], confidence=0.9
)


# --- Masking ---


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("pošli na SK31 1200 0000 1987 4263 7541", "pošli na SK** **** 7541"),
        ("IBAN CZ6508000000192000145399.", "IBAN CZ** **** 5399."),
        ("volaj +421 905 123 456 zajtra", "volaj ••• 456 zajtra"),
        ("číslo 0905/123456", "číslo ••• 456"),
        ("00420 777 123 456", "••• 456"),
    ],
)
def test_masks_ibans_and_phone_numbers(text: str, masked: str) -> None:
    assert mask_sensitive(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "zaplatil som Paťovi pizzu 12€",
        "1 250 € za letenky",
        "stretnutie 23.09.2026 o 18:30",
        "D-0142 a D-0143",
        "SK je super",
    ],
)
def test_leaves_amounts_dates_and_words_alone(text: str) -> None:
    assert mask_sensitive(text) == text


# --- Prompt ---


def request(text: str = "zaplatil som Paťovi pizzu 12€", **kwargs: Any) -> CaptureRequest:
    values: dict[str, Any] = {
        "text": text,
        "today": date(2026, 9, 23),
        "candidates": (CandidatePerson(7, "Patrik Veselý", ("Paťo", "Pato")),),
        "default_currency": "EUR",
    }
    values.update(kwargs)
    return CaptureRequest(**values)


def test_prompt_lists_candidates_and_today() -> None:
    prompt = build_capture_prompt(request())
    assert "Today is 2026-09-23 (Wednesday)." in prompt
    assert "- 7: Patrik Veselý; Paťo, Pato" in prompt
    assert "zaplatil som Paťovi pizzu 12€" in prompt


def test_prompt_is_masked() -> None:
    prompt = build_capture_prompt(
        request("jeho číslo je +421 905 123 456, IBAN SK3112000000198742637541")
    )
    assert "905 123" not in prompt
    assert "198742637541" not in prompt
    assert "••• 456" in prompt and "SK** **** 7541" in prompt


def test_prompt_without_candidates() -> None:
    assert "No known people matched" in build_capture_prompt(request(candidates=()))


# --- Client ---


class FakeMessages:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_client(*outcomes: Any) -> tuple[ClaudeClient, FakeMessages]:
    messages = FakeMessages(*outcomes)
    sdk = SimpleNamespace(messages=messages, close=lambda: None)
    return ClaudeClient("key", "model-from-env", client=sdk), messages  # type: ignore[arg-type]


def answer(parsed: CaptureExtraction | None, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


def test_returns_the_parsed_output_using_the_configured_model() -> None:
    client, messages = fake_client(answer(EMPTY))
    assert client.extract_capture(request()) is EMPTY
    (call,) = messages.calls
    assert call["model"] == "model-from-env"
    assert call["output_format"] is CaptureExtraction
    assert "default EUR" in call["system"]
    assert "Paťovi" in call["messages"][0]["content"]


def test_retries_once_on_invalid_output() -> None:
    try:
        CaptureExtraction.model_validate({})
    except ValidationError as exc:
        invalid = exc
    client, messages = fake_client(invalid, answer(EMPTY))
    assert client.extract_capture(request()) is EMPTY
    assert len(messages.calls) == 2


def test_gives_up_after_repeated_bad_answers() -> None:
    client, _ = fake_client(answer(None, "max_tokens"), answer(None, "max_tokens"))
    with pytest.raises(ClaudeError, match="no usable answer"):
        client.extract_capture(request())


def test_refusal_raises() -> None:
    client, _ = fake_client(answer(None, "refusal"))
    with pytest.raises(ClaudeError, match="declined"):
        client.extract_capture(request())


def test_api_errors_raise_claude_error() -> None:
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client, messages = fake_client(error)
    with pytest.raises(ClaudeError, match="APIConnectionError"):
        client.extract_capture(request())
    assert len(messages.calls) == 1  # the SDK already retried transport errors


# --- Transaction classification ---

ROW = TransactionRow(
    ref=0,
    when="2026-09-08 20:29",
    direction="out",
    amount="48.00 EUR",
    kind="card",
    description="Lokál",
)


def test_classification_prompt_lists_rows_masked() -> None:
    prompt = build_classification_prompt(
        [
            ROW,
            TransactionRow(
                1, "2026-09-09", "in", "20.00 EUR", "transfer", "SK3112000000198742637541"
            ),
        ]
    )
    assert "0 | 2026-09-08 20:29 | out | 48.00 EUR | card | Lokál | -" in prompt
    assert "198742637541" not in prompt and "SK** **** 7541" in prompt


def test_classify_transactions_uses_structured_output() -> None:
    parsed = TransactionClassification(
        transactions=[
            ClassifiedTransaction(
                ref=0, classification=Classification.LIKELY_SHARED, confidence=0.9
            )
        ]
    )
    client, messages = fake_client(answer(parsed))  # type: ignore[arg-type]
    assert client.classify_transactions([ROW], "15.00 €") is parsed
    (call,) = messages.calls
    assert call["model"] == "model-from-env"
    assert call["output_format"] is TransactionClassification
    assert "under about 15.00 €" in call["system"]
    assert "Lokál" in call["messages"][0]["content"]


def test_classify_transactions_errors_raise_claude_error() -> None:
    client, _ = fake_client(answer(None, "refusal"))
    with pytest.raises(ClaudeError, match="declined"):
        client.classify_transactions([ROW], "15.00 €")
