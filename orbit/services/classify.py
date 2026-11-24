"""Classifying new bank rows: rules first, then Claude for the rest, in batches per import.

The amount rule compares against `SHARED_MIN_AMOUNT` (in `DEFAULT_CURRENCY`); rows in another
currency are converted with ECB rates for that check only. With no rate, the rule is skipped and
Claude decides. Rows neither the rules nor Claude could place stay unclassified.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from orbit.domain.bank import Transaction
from orbit.domain.enums import Classification, TransactionDirection, TransactionKind
from orbit.domain.money import format_amount, format_plain
from orbit.integrations.claude import ClaudeError, TransactionClassifier, TransactionRow
from orbit.integrations.ecb import RateSource, convert_minor
from orbit.settings import Settings

log = logging.getLogger(__name__)

CLAUDE_BATCH_SIZE = 100


def _patterns(*words: str) -> re.Pattern[str]:
    # Matched against diacritics-free lowercase text, at the start of a word.
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + ")")


# Shared only above the amount threshold: food and drink, groceries, taxis.
THRESHOLD_PATTERNS = _patterns(
    r"restaur", r"restor", r"bistro", r"pizz", r"kebab", r"kebap", r"burger", r"mcdonald",
    r"kfc\b", r"grill", r"trattori", r"pub\b", r"bar\b", r"lounge\b", r"cafe\b", r"caffe",
    r"kaviaren", r"kavarn", r"coffee", r"starbucks", r"sushi", r"ramen", r"pivnica", r"pivovar",
    r"hostinec", r"krcma", r"wolt\b", r"foodora", r"doner", r"obcerst", r"bufet",
    r"lidl", r"billa", r"kaufland", r"tesco", r"albert\b", r"penny\b", r"coop\b", r"jednota",
    r"spar\b", r"hofer", r"yeme", r"rohlik", r"potraviny", r"(?:mini|super)?market\b",
    r"bolt", r"uber", r"liftago", r"hopin", r"taxi",
)  # fmt: skip
# Shared regardless of the amount: event tickets and accommodation.
ALWAYS_PATTERNS = _patterns(
    r"ticketportal", r"ticketmaster", r"goout", r"eventim", r"vstupenk", r"predpredaj",
    r"cinema city", r"cinemax", r"kino\b", r"airbnb",
)  # fmt: skip
_OWN_MONEY_KINDS = frozenset(
    {TransactionKind.TOPUP, TransactionKind.EXCHANGE, TransactionKind.ATM, TransactionKind.FEE}
)


@dataclass(frozen=True)
class ClassifyContext:
    default_currency: str
    shared_min_minor: int  # in default_currency
    today: date
    tz: ZoneInfo

    @classmethod
    def from_settings(cls, settings: Settings, today: date) -> ClassifyContext:
        return cls(
            settings.default_currency, settings.shared_min_amount_minor, today, settings.timezone
        )


@dataclass
class ClassifyOutcome:
    by_rules: int = 0
    by_claude: int = 0
    unclassified: int = 0
    # Claude's confidence per transaction id, for the proposals it leads to.
    confidence: dict[int, float] = field(default_factory=dict)
    note: str | None = None  # why some rows stayed unclassified


class _Threshold:
    """`amount >= SHARED_MIN_AMOUNT`, converting other currencies with (lazily fetched) rates."""

    def __init__(self, context: ClassifyContext, rates: RateSource | None) -> None:
        self._context = context
        self._source = rates
        self._rates: dict[str, Decimal] | None = None
        self._fetched = False

    def reached(self, tx: Transaction) -> bool | None:
        amount: int | None = tx.amount_minor
        if tx.currency != self._context.default_currency:
            if not self._fetched:
                self._fetched = True
                self._rates = self._source.eur_rates(self._context.today) if self._source else None
            amount = (
                convert_minor(
                    tx.amount_minor, tx.currency, self._context.default_currency, self._rates
                )
                if self._rates is not None
                else None
            )
        return None if amount is None else amount >= self._context.shared_min_minor


def rule_classification(tx: Transaction, threshold: _Threshold) -> Classification | None:
    """The rules' verdict, or None when Claude has to decide."""
    if tx.kind in _OWN_MONEY_KINDS:
        return Classification.IGNORE
    if tx.direction is TransactionDirection.IN:
        if tx.kind is TransactionKind.TRANSFER:
            return Classification.INCOMING_FROM_PERSON
        if tx.kind is TransactionKind.CARD:
            return Classification.IGNORE  # a refund
        return None
    if tx.kind is TransactionKind.TRANSFER:
        return Classification.PERSONAL
    text = tx.counterparty_norm or ""
    if ALWAYS_PATTERNS.search(text):
        return Classification.LIKELY_SHARED
    if THRESHOLD_PATTERNS.search(text):
        reached = threshold.reached(tx)
        if reached is None:
            return None
        return Classification.LIKELY_SHARED if reached else Classification.PERSONAL
    return None


# The calendar meetings going on when a payment was made: "kafe s Paťom (Patrik Veselý)".
MeetingLookup = Callable[[Transaction], str | None]


def _row(ref: int, tx: Transaction, tz: ZoneInfo, meeting: str | None = None) -> TransactionRow:
    when = (
        tx.occurred_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        if tx.occurred_at
        else tx.booking_date.isoformat()
    )
    description = " · ".join(p for p in (tx.counterparty, tx.message) if p) or tx.kind.value
    return TransactionRow(
        ref=ref,
        when=when,
        direction=tx.direction.value,
        amount=f"{format_plain(tx.amount_minor, tx.currency)} {tx.currency}",
        kind=tx.kind.value,
        description=description,
        meeting=meeting,
    )


def _ask_claude(
    rows: Sequence[Transaction],
    classifier: TransactionClassifier,
    context: ClassifyContext,
    outcome: ClassifyOutcome,
    meetings: MeetingLookup | None,
) -> None:
    shared_min = format_amount(context.shared_min_minor, context.default_currency)
    for start in range(0, len(rows), CLAUDE_BATCH_SIZE):
        batch = rows[start : start + CLAUDE_BATCH_SIZE]
        try:
            answer = classifier.classify_transactions(
                [
                    _row(i, tx, context.tz, meetings(tx) if meetings else None)
                    for i, tx in enumerate(batch)
                ],
                shared_min,
            )
        except ClaudeError as exc:
            log.warning("transaction classification failed: %s", exc)
            outcome.note = "Claude couldn't classify some rows."
            continue
        for item in answer.transactions:
            if not 0 <= item.ref < len(batch):
                continue
            tx = batch[item.ref]
            if tx.classification is not None:
                continue  # a ref Claude listed twice
            tx.classification = item.classification
            outcome.confidence[tx.id] = min(max(item.confidence, 0.0), 1.0)
            outcome.by_claude += 1


def classify(
    transactions: Sequence[Transaction],
    classifier: TransactionClassifier | None,
    rates: RateSource | None,
    context: ClassifyContext,
    meetings: MeetingLookup | None = None,
) -> ClassifyOutcome:
    """Set `classification` on each new transaction (flushed ids required for confidence).
    `meetings` tells Claude which calendar meetings overlapped each row it's asked about."""
    outcome = ClassifyOutcome()
    threshold = _Threshold(context, rates)
    undecided: list[Transaction] = []
    for tx in transactions:
        tx.classification = rule_classification(tx, threshold)
        if tx.classification is None:
            undecided.append(tx)
        else:
            outcome.by_rules += 1
    if undecided:
        if classifier is None:
            outcome.note = "Claude isn't configured, so some rows weren't classified."
        else:
            _ask_claude(undecided, classifier, context, outcome, meetings)
    outcome.unclassified = sum(1 for tx in transactions if tx.classification is None)
    return outcome
