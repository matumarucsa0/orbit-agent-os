"""ECB daily euro reference rates, for the `SHARED_MIN_AMOUNT` check on foreign-currency rows.

Conversions are display/classification only and never stored. Rates are fetched at most once per
day per process; if the ECB can't be reached, callers get None and skip the amount rule.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date
from decimal import Decimal
from typing import Protocol

import httpx

from orbit.domain.money import exponent

log = logging.getLogger(__name__)

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
TIMEOUT_SECONDS = 10.0
_CUBE_RE = re.compile(r"<Cube\s+currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([0-9.]+)['\"]")


class RateSource(Protocol):
    def eur_rates(self, today: date) -> dict[str, Decimal] | None:
        """Units of each currency per 1 EUR (EUR itself included), or None if unavailable."""
        ...


def parse_daily_xml(text: str) -> dict[str, Decimal]:
    rates = {code: Decimal(rate) for code, rate in _CUBE_RE.findall(text)}
    rates["EUR"] = Decimal(1)
    return rates


class EcbRates:
    def __init__(self, client: httpx.Client | None = None, url: str = ECB_DAILY_URL) -> None:
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._url = url
        self._lock = threading.Lock()
        self._cache: tuple[date, dict[str, Decimal] | None] | None = None

    def eur_rates(self, today: date) -> dict[str, Decimal] | None:
        with self._lock:
            if self._cache is not None and self._cache[0] == today:
                return self._cache[1]
            rates: dict[str, Decimal] | None
            try:
                response = self._client.get(self._url)
                response.raise_for_status()
                rates = parse_daily_xml(response.text)
            except httpx.HTTPError as exc:
                log.warning("ECB rates unavailable: %s", exc)
                rates = None
            if rates is not None and len(rates) < 2:
                log.warning("ECB rates: no rates in the answer")
                rates = None
            # A failure is cached for the day too: no hammering the ECB on every import.
            self._cache = (today, rates)
            return rates

    def close(self) -> None:
        self._client.close()


def convert_minor(
    amount_minor: int, source: str, target: str, rates: dict[str, Decimal]
) -> int | None:
    """Convert minor units between currencies through EUR; None if a rate is missing."""
    if source == target:
        return amount_minor
    if source not in rates or target not in rates:
        return None
    major = Decimal(amount_minor).scaleb(-exponent(source))
    converted = major / rates[source] * rates[target]
    return int(converted.scaleb(exponent(target)).to_integral_value())
