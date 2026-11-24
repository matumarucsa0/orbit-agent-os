"""OpenTelemetry spans for Claude calls (GenAI semantic conventions) and worker jobs.

Off by default: with `ORBIT_OTEL_EXPORTER=none` the spans come from OpenTelemetry's no-op tracer and
cost nothing. `console` prints finished spans to stdout, `otlp` sends them over OTLP/HTTP to the
endpoint named by the standard `OTEL_EXPORTER_OTLP_*` variables (needs the `otlp` extra).

Attributes follow the GenAI conventions where one exists (`gen_ai.system`,
`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) and use the
`orbit.*` prefix for the rest (purpose, prompt_version, latency_ms, ok, cost_eur). Prompt and answer
text is never put on a span.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, Tracer

if TYPE_CHECKING:
    from orbit.integrations.claude import Usage

log = logging.getLogger(__name__)

TRACER_NAME = "orbit"
_provider: TracerProvider | None = None


def configure(exporter: str) -> TracerProvider | None:
    """Set up tracing for the process; "none" leaves the no-op tracer in place."""
    global _provider
    if exporter == "none":
        _provider = None
        return None
    span_exporter: SpanExporter
    if exporter == "console":
        span_exporter = ConsoleSpanExporter()
    elif exporter == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            log.warning("ORBIT_OTEL_EXPORTER=otlp needs the otlp extra (uv sync --extra otlp)")
            return None
        span_exporter = OTLPSpanExporter()
    else:
        raise ValueError(f"unknown exporter {exporter!r}")
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    use_provider(provider)
    return provider


def use_provider(provider: TracerProvider | None) -> None:
    """Use this provider for Orbit's spans (tests pass one with an in-memory exporter)."""
    global _provider
    _provider = provider


def in_memory_provider(exporter: SpanExporter) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def tracer() -> Tracer:
    if _provider is not None:
        return _provider.get_tracer(TRACER_NAME)
    return trace.get_tracer(TRACER_NAME)


def shutdown() -> None:
    if _provider is not None:
        _provider.shutdown()


@contextmanager
def model_call_span(purpose: str, model: str, prompt_version: str | None) -> Iterator[Span]:
    """A span around one request to the model."""
    with tracer().start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("orbit.purpose", purpose)
        if prompt_version:
            span.set_attribute("orbit.prompt_version", prompt_version)
        yield span


def finish_model_span(span: Span, usage: Usage, latency_ms: int, cost_micro_eur: int) -> None:
    span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    span.set_attribute("orbit.latency_ms", latency_ms)
    span.set_attribute("orbit.ok", usage.ok)
    span.set_attribute("orbit.cost_eur", cost_micro_eur / 1_000_000)
    if not usage.ok:
        span.set_status(trace.Status(trace.StatusCode.ERROR))


def record_worker_job(
    kind: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int | None,
    ok: bool,
    prompt_version: str | None,
    end_ns: int,
) -> None:
    """A span for a job the Claude Code worker answered (it ran elsewhere; this records it)."""
    start_ns = end_ns - (duration_ms or 0) * 1_000_000
    span = tracer().start_span(f"worker {kind}", start_time=start_ns)
    span.set_attribute("gen_ai.system", "anthropic")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
    span.set_attribute("orbit.purpose", kind)
    span.set_attribute("orbit.backend", "worker")
    span.set_attribute("orbit.ok", ok)
    span.set_attribute("orbit.cost_eur", 0.0)
    if duration_ms is not None:
        span.set_attribute("orbit.latency_ms", duration_ms)
    if prompt_version:
        span.set_attribute("orbit.prompt_version", prompt_version)
    if not ok:
        span.set_status(trace.Status(trace.StatusCode.ERROR))
    span.end(end_time=end_ns)
