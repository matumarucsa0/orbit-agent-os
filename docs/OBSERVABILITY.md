# Tracing and cost

## Spans

Orbit creates one OpenTelemetry span per request to the model (`chat <model>`) and one per job
the Claude Code worker finished (`worker <kind>`). Attributes:

| Attribute | Meaning |
|---|---|
| `gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model` | GenAI semantic conventions |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | tokens reported by the API |
| `orbit.purpose` | capture, classification, weekly_review, ... |
| `orbit.prompt_version` | for example `capture@1` (docs/PROMPTS.md) |
| `orbit.latency_ms`, `orbit.ok`, `orbit.cost_eur` | wall time, success, estimated price |
| `orbit.backend` | `worker` for worker jobs |

Prompt and answer text never goes on a span. A failed request has span status ERROR.

`ORBIT_OTEL_EXPORTER`: `none` (default: no-op, nothing is created), `console` (finished spans are
printed to stdout) or `otlp` (OTLP over HTTP; install with `uv sync --extra otlp` and set the
standard `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS`, for example to a Langfuse or
Jaeger collector). The same numbers are in the `claude_call` table and on `/ops/claude`.

## Cost

`cost_micro_eur` on each `claude_call` is an estimate: tokens times a list price per model family,
converted from dollars with `USD_EUR_RATE` (a fixed number you maintain, default 0.92). Set exact
euro prices per model name with `CLAUDE_PRICE_OVERRIDES`, for example
`{"my-model": {"input_eur_per_mtok": 0.9, "output_eur_per_mtok": 4.6}}`. Cache reads and writes are
not tracked. `/ops/claude` shows cost per month and purpose.

`CLAUDE_MONTHLY_BUDGET_EUR` (0 = off): at 80 percent of the month's estimate you get one push, at 100
percent another, and API requests stop (callers fall back as without a key: a capture is saved as a
note draft, bank rows stay unsorted). The worker and the offline fake are never stopped.
