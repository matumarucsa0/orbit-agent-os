from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from orbit.domain.bank import Transaction
from orbit.domain.enums import Bank, Classification, TransactionDirection, TransactionKind
from orbit.domain.normalize import normalize_text
from orbit.integrations.claude import (
    ClassifiedTransaction,
    ClaudeError,
    TransactionClassification,
    TransactionRow,
)
from orbit.services.classify import ClassifyContext, classify

CONTEXT = ClassifyContext("EUR", 1500, date(2026, 9, 24), ZoneInfo("Europe/Prague"))
OUT, IN = TransactionDirection.OUT, TransactionDirection.IN
SHARED, PERSONAL = Classification.LIKELY_SHARED, Classification.PERSONAL
CARD, TRANSFER = TransactionKind.CARD, TransactionKind.TRANSFER


def tx(
    counterparty: str | None,
    amount_minor: int = 2000,
    *,
    currency: str = "EUR",
    direction: TransactionDirection = OUT,
    kind: TransactionKind = CARD,
    id: int = 1,
    message: str | None = None,
) -> Transaction:
    return Transaction(
        id=id,
        bank=Bank.TATRA,
        booking_date=date(2026, 9, 8),
        occurred_at=datetime(2026, 9, 8, 18, 29, tzinfo=UTC),
        amount_minor=amount_minor,
        currency=currency,
        direction=direction,
        kind=kind,
        counterparty=counterparty,
        counterparty_norm=normalize_text(counterparty) if counterparty else None,
        message=message,
        hash=str(id),
        raw_json={},
    )


class FakeRates:
    def __init__(self, rates: dict[str, Decimal] | None) -> None:
        self.rates = rates
        self.calls = 0

    def eur_rates(self, today: date) -> dict[str, Decimal] | None:
        self.calls += 1
        return self.rates


class FakeClassifier:
    def __init__(self, *answers: list[tuple[int, Classification]] | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[TransactionRow]] = []

    def classify_transactions(
        self, rows: Sequence[TransactionRow], shared_min: str
    ) -> TransactionClassification:
        self.calls.append(list(rows))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return TransactionClassification(
            transactions=[
                ClassifiedTransaction(ref=ref, classification=c, confidence=0.8)
                for ref, c in answer
            ]
        )


def rules(transaction: Transaction, rates: FakeRates | None = None) -> Classification | None:
    classify([transaction], None, rates, CONTEXT)
    return transaction.classification


@pytest.mark.parametrize(
    ("merchant", "amount", "expected"),
    [
        ("Bar Restaurant Example", 3910, SHARED),
        ("NORTHWOOD CAFE", 1933, SHARED),
        ("Potraviny Sever dakuje 197", 2254, SHARED),  # groceries over SHARED_MIN_AMOUNT
        ("Potraviny Sever dakuje 197", 1500, SHARED),  # the threshold itself counts
        ("Potraviny Sever dakuje 197", 1499, PERSONAL),
        ("BILLA 120", 75, PERSONAL),
        ("McDonald's", 1250, PERSONAL),
        ("Bolt.eu/r/2609", 1850, SHARED),
        ("KRAL - Turkish Doner K", 1600, SHARED),
        ("KINO ALFA,PL,CENTRUM", 950, SHARED),  # tickets: any amount
        ("PAYPAL *AIRBNB EX1234X", 60090, SHARED),
        ("Kaviareň Pražiareň", 2100, SHARED),  # diacritics don't matter
        ("BARBORA", 2000, None),  # "bar" only as a whole word
        ("ANTHROPIC* CLAUDE SUB", 2214, None),  # Claude decides
        ("od nasich", 1390, None),
    ],
)
def test_rules_for_card_payments(merchant: str, amount: int, expected: Classification) -> None:
    assert rules(tx(merchant, amount)) is expected


@pytest.mark.parametrize(
    ("kind", "direction", "expected"),
    [
        (TransactionKind.TOPUP, IN, Classification.IGNORE),
        (TransactionKind.EXCHANGE, OUT, Classification.IGNORE),
        (TransactionKind.ATM, OUT, Classification.IGNORE),
        (TransactionKind.FEE, OUT, Classification.IGNORE),
        (TRANSFER, IN, Classification.INCOMING_FROM_PERSON),
        (TRANSFER, OUT, PERSONAL),
        (CARD, IN, Classification.IGNORE),  # a refund
        (TransactionKind.OTHER, IN, None),
    ],
)
def test_rules_by_kind(
    kind: TransactionKind, direction: TransactionDirection, expected: Classification | None
) -> None:
    assert rules(tx("Lokál", 4800, kind=kind, direction=direction)) is expected


def test_foreign_currency_is_converted_for_the_threshold_only() -> None:
    rates = FakeRates({"EUR": Decimal(1), "CZK": Decimal("25")})
    dinner = tx("Restaurace U Fleků", 40000, currency="CZK")  # 16 €
    coffee = tx("Kavárna Místo", 30000, currency="CZK", id=2)  # 12 €
    classify([dinner, coffee], None, rates, CONTEXT)
    assert (dinner.classification, coffee.classification) == (SHARED, PERSONAL)
    assert rates.calls == 1  # fetched once per import
    assert dinner.amount_minor == 40000 and dinner.currency == "CZK"  # nothing stored converted


def test_without_a_rate_the_amount_rule_is_skipped() -> None:
    for rates in (None, FakeRates(None), FakeRates({"EUR": Decimal(1)})):
        assert rules(tx("Restaurace U Fleků", 40000, currency="CZK"), rates) is None
    # Same-currency rows never need a rate.
    no_rates = FakeRates(None)
    assert rules(tx("Lokál Restaurant", 4800), no_rates) is SHARED
    assert no_rates.calls == 0


def test_claude_classifies_what_the_rules_leave() -> None:
    rows = [
        tx("od nasich", 1390, id=1),
        tx("Potraviny Sever dakuje 197", 2254, id=2),
        tx("XIAO YING S.R.O.", 4600, id=3),
    ]
    claude = FakeClassifier([(0, PERSONAL), (1, SHARED)])
    outcome = classify(rows, claude, None, CONTEXT)
    ((first, second),) = claude.calls
    assert (first.description, second.description) == ("od nasich", "XIAO YING S.R.O.")
    assert first.when == "2026-09-08 20:29"  # local time
    assert first.amount == "13.90 EUR" and first.direction == "out" and first.kind == "card"
    assert [r.classification for r in rows] == [PERSONAL, SHARED, SHARED]
    assert (outcome.by_rules, outcome.by_claude, outcome.unclassified) == (1, 2, 0)
    assert outcome.confidence == {1: 0.8, 3: 0.8}


def test_claude_answers_are_checked() -> None:
    rows = [tx("A", id=1), tx("B", id=2), tx("C", id=3)]
    # an out-of-range ref, a ref listed twice, and one row left out
    claude = FakeClassifier([(7, SHARED), (0, PERSONAL), (0, SHARED), (1, SHARED)])
    outcome = classify(rows, claude, None, CONTEXT)
    assert [r.classification for r in rows] == [PERSONAL, SHARED, None]
    assert outcome.unclassified == 1


def test_claude_is_asked_in_batches_and_a_failed_batch_stays_unclassified() -> None:
    rows = [tx(f"Shop {i}", id=i + 1) for i in range(150)]
    claude = FakeClassifier(ClaudeError("overloaded"), [(i, PERSONAL) for i in range(50)])
    outcome = classify(rows, claude, None, CONTEXT)
    assert [len(call) for call in claude.calls] == [100, 50]
    assert outcome.unclassified == 100 and outcome.by_claude == 50
    assert outcome.note == "Claude couldn't classify some rows."


def test_without_claude_undecided_rows_stay_unclassified() -> None:
    outcome = classify([tx("od nasich")], None, None, CONTEXT)
    assert outcome.unclassified == 1
    assert outcome.note == "Claude isn't configured, so some rows weren't classified."
