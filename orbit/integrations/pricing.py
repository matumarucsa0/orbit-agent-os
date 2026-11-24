"""What a Claude call costs, as an estimate in euros.

Built-in prices are list prices in US dollars per million tokens, by model family, converted with
the fixed `USD_EUR_RATE` setting (there is no live exchange rate). They will drift from the real
price list: check them against Anthropic's pricing page, or set exact euro prices per model name in
`CLAUDE_PRICE_OVERRIDES`. A model that matches no family is priced like the middle one ("sonnet"),
so the budget errs on the high side. The fake backend and the Claude Code worker (a subscription)
cost 0. Cache reads and writes are not tracked, so a cached call is priced as plain input.

Money is kept as integer micro-euros (millionths of a euro) to avoid float drift when summing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MICRO = 1_000_000
FREE_MODELS = frozenset({"fake", "claude-code"})


@dataclass(frozen=True)
class PriceUsd:
    input_per_mtok: float
    output_per_mtok: float


# (substring of the model name, price); first match wins. List prices at the time of writing.
FAMILY_PRICES_USD: tuple[tuple[str, PriceUsd], ...] = (
    ("haiku", PriceUsd(1.0, 5.0)),
    ("sonnet", PriceUsd(3.0, 15.0)),
    ("opus", PriceUsd(5.0, 25.0)),
)
DEFAULT_PRICE_USD = PriceUsd(3.0, 15.0)


def price_eur_per_mtok(
    model: str,
    usd_eur_rate: float,
    overrides: Mapping[str, tuple[float, float]] | None = None,
) -> tuple[float, float]:
    """(input, output) in euros per million tokens."""
    if model in FREE_MODELS:
        return 0.0, 0.0
    if overrides and model in overrides:
        return overrides[model]
    name = model.lower()
    price = next((p for key, p in FAMILY_PRICES_USD if key in name), DEFAULT_PRICE_USD)
    return price.input_per_mtok * usd_eur_rate, price.output_per_mtok * usd_eur_rate


def cost_micro_eur(
    model: str,
    input_tokens: int,
    output_tokens: int,
    usd_eur_rate: float,
    overrides: Mapping[str, tuple[float, float]] | None = None,
) -> int:
    price_in, price_out = price_eur_per_mtok(model, usd_eur_rate, overrides)
    # euros per million tokens == micro-euros per token
    return round(input_tokens * price_in + output_tokens * price_out)


def format_eur(micro: int) -> str:
    return f"€{micro / MICRO:.2f}" if micro >= 10_000 else f"€{micro / MICRO:.4f}"
