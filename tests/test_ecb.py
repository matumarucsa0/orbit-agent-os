from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx

from orbit.integrations.ecb import EcbRates, convert_minor, parse_daily_xml

DAILY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <Cube>
    <Cube time='2026-09-23'>
      <Cube currency='USD' rate='1.1650'/>
      <Cube currency='JPY' rate='171.20'/>
      <Cube currency='CZK' rate='24.310'/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""
RATES = parse_daily_xml(DAILY_XML)


def test_parses_the_daily_file() -> None:
    assert {
        "USD": Decimal("1.1650"),
        "JPY": Decimal("171.20"),
        "CZK": Decimal("24.310"),
        "EUR": Decimal(1),
    } == RATES


def test_converts_minor_units_through_eur() -> None:
    assert convert_minor(48620, "CZK", "EUR", RATES) == 2000  # 486.20 Kč
    assert convert_minor(2000, "EUR", "CZK", RATES) == 48620
    assert convert_minor(3424, "JPY", "EUR", RATES) == 2000  # JPY has no minor unit
    assert convert_minor(2330, "USD", "CZK", RATES) == 48620
    assert convert_minor(1234, "EUR", "EUR", {}) == 1234
    assert convert_minor(1000, "HUF", "EUR", RATES) is None


def fetcher(*answers: httpx.Response | Exception) -> tuple[EcbRates, list[httpx.Request]]:
    queue = list(answers)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return EcbRates(httpx.Client(transport=httpx.MockTransport(handle))), seen


def test_fetches_once_per_day() -> None:
    rates, seen = fetcher(httpx.Response(200, text=DAILY_XML), httpx.Response(200, text=DAILY_XML))
    assert rates.eur_rates(date(2026, 9, 24)) == RATES
    assert rates.eur_rates(date(2026, 9, 24)) == RATES
    assert len(seen) == 1
    assert rates.eur_rates(date(2026, 9, 25)) == RATES
    assert len(seen) == 2


def test_failures_give_none_and_are_not_retried_the_same_day() -> None:
    rates, seen = fetcher(httpx.ConnectError("offline"))
    assert rates.eur_rates(date(2026, 9, 24)) is None
    assert rates.eur_rates(date(2026, 9, 24)) is None
    assert len(seen) == 1
    for answer in (httpx.Response(503), httpx.Response(200, text="<html>maintenance</html>")):
        rates, _ = fetcher(answer)
        assert rates.eur_rates(date(2026, 9, 24)) is None
