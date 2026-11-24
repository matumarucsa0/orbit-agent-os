"""OpenTelemetry GenAI spans around model calls and worker jobs (W4)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit import tracing
from orbit.domain.usage import ClaudeCall
from orbit.integrations.claude import CaptureExtraction, ClaudeClient, ClaudeError
from orbit.integrations.pricing import cost_micro_eur
from orbit.services import claude_jobs
from tests.test_claude import EMPTY, FakeMessages, request
from tests.test_usage import with_usage


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tracing.use_provider(tracing.in_memory_provider(exporter))
    yield exporter
    tracing.use_provider(None)


def client_for(*outcomes: object) -> ClaudeClient:
    sdk = SimpleNamespace(messages=FakeMessages(*outcomes), close=lambda: None)
    return ClaudeClient(
        "key",
        "claude-sonnet-x",
        client=sdk,  # type: ignore[arg-type]
        pricer=lambda model, i, o: cost_micro_eur(model, i, o, 1.0),
    )


def test_a_model_call_makes_one_genai_span(spans: InMemorySpanExporter) -> None:
    client_for(with_usage(EMPTY, 1_000, 200)).extract_capture(request())
    (span,) = spans.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert span.name == "chat claude-sonnet-x"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-x"
    assert attrs["gen_ai.usage.input_tokens"] == 1_000
    assert attrs["gen_ai.usage.output_tokens"] == 200
    assert attrs["orbit.purpose"] == "capture"
    assert attrs["orbit.prompt_version"] == "capture@1"
    assert attrs["orbit.ok"] is True
    assert attrs["orbit.cost_eur"] == pytest.approx((1_000 * 3 + 200 * 15) / 1e6)
    assert isinstance(attrs["orbit.latency_ms"], int)
    assert span.status.status_code is StatusCode.UNSET


def test_a_retry_after_invalid_output_makes_a_span_per_request(
    spans: InMemorySpanExporter,
) -> None:
    try:
        CaptureExtraction.model_validate({})
    except ValidationError as exc:
        invalid = exc
    client_for(invalid, with_usage(EMPTY, 10, 5)).extract_capture(request())
    first, second = spans.get_finished_spans()
    assert first.attributes["orbit.ok"] is False  # type: ignore[index]
    assert first.status.status_code is StatusCode.ERROR
    assert second.attributes["orbit.ok"] is True  # type: ignore[index]


def test_an_api_error_marks_the_span_failed(spans: InMemorySpanExporter) -> None:
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(ClaudeError):
        client_for(error).extract_capture(request())
    (span,) = spans.get_finished_spans()
    assert span.attributes["orbit.ok"] is False  # type: ignore[index]
    assert span.status.status_code is StatusCode.ERROR


def test_spans_never_hold_prompt_or_answer_text(spans: InMemorySpanExporter) -> None:
    client_for(with_usage(EMPTY, 1, 1)).extract_capture(request("a very private sentence"))
    text = repr([dict(s.attributes or {}) for s in spans.get_finished_spans()])
    assert "private" not in text


def test_no_exporter_is_a_no_op() -> None:
    tracing.use_provider(None)
    assert tracing.configure("none") is None
    assert client_for(with_usage(EMPTY, 1, 1)).extract_capture(request()) is EMPTY


def test_console_exporter_is_opt_in() -> None:
    provider = tracing.configure("console")
    try:
        assert provider is not None
    finally:
        tracing.use_provider(None)
    with pytest.raises(ValueError, match="unknown"):
        tracing.configure("zipkin")


def test_worker_job_records_a_span_and_a_versioned_call(
    spans: InMemorySpanExporter, db: Session
) -> None:
    from datetime import timedelta

    from orbit.services.claude_jobs import QueueLimits
    from tests.test_claude_jobs import LIMITS, T0, spec

    assert isinstance(LIMITS, QueueLimits)
    job = claude_jobs.enqueue(db, spec(), limits=LIMITS, now=T0)
    claude_jobs.claim(db, T0)
    moment = T0 + timedelta(seconds=3)
    claude_jobs.submit(
        db,
        job.id,
        {"text": "hi"},
        model="m",
        input_tokens=7,
        output_tokens=3,
        duration_ms=2_500,
        now=moment,
    )
    (span,) = spans.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert span.name == "worker draft"
    assert attrs["orbit.backend"] == "worker" and attrs["orbit.prompt_version"] == "draft@1"
    assert attrs["gen_ai.usage.input_tokens"] == 7 and attrs["orbit.cost_eur"] == 0.0
    assert span.end_time - span.start_time == 2_500_000_000  # type: ignore[operator]
    call = db.scalars(select(ClaudeCall)).one()
    assert (call.latency_ms, call.prompt_version, call.cost_micro_eur) == (2_500, "draft@1", 0)


def test_worker_failure_span_is_an_error(spans: InMemorySpanExporter) -> None:
    tracing.record_worker_job("brief", "claude-code", 0, 0, None, False, "brief@1", 9_000_000_000)
    (span,) = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
