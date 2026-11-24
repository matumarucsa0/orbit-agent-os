from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.bank import BankImport, Transaction
from orbit.domain.enums import Bank, Classification, ProposalKind, TransactionDirection
from orbit.domain.inbox import Notification
from orbit.services import notifications
from orbit.services.bank_import import ImportReport, import_statement, notify_splits
from orbit.services.classify import ClassifyContext
from orbit.services.errors import InvalidInputError
from tests.test_bank_parsers import revolut_bytes, tatra_bytes
from tests.test_classify import FakeClassifier
from tests.test_notifications import FakeSender

PRAGUE = ZoneInfo("Europe/Prague")
CONTEXT = ClassifyContext("EUR", 1500, date(2026, 9, 24), PRAGUE)
QUIET = notifications.QuietHours(time(22, 0), time(8, 0), PRAGUE)
NOON = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
# Flagged by the rules alone in tests/fixtures/bank/tatra.csv.
RULE_FLAGGED = [
    "Potraviny Sever dakuje 197",  # 22.54 € of groceries
    "NORTHWOOD CAFE",
    "KINO ALFA,PL,CENTRUM",
    "EXAMPLE LOUNGE",
    "Bar Restaurant Example",
    "PAYPAL *AIRBNB EX1234X",
]


def run(
    db: Session,
    raw: bytes,
    bank: Bank | None = None,
    classifier: FakeClassifier | None = None,
    filename: str = "statement.csv",
) -> ImportReport:
    return import_statement(
        db, raw, filename=filename, bank=bank, context=CONTEXT, classifier=classifier, rates=None
    )


def tatra_lines() -> tuple[str, list[str]]:
    header, *rows = tatra_bytes().decode("cp1250").split("\r\n")
    return header, [r for r in rows if r]


def tatra_file(rows: list[str]) -> bytes:
    header, _ = tatra_lines()
    return "\r\n".join([header, *rows, ""]).encode("cp1250")


def flagged_names(report: ImportReport) -> list[str]:
    by_id = {tx.id: tx for tx in report.new}
    return [by_id[int(p.source_id or 0)].label for p in report.flagged]


def test_imports_a_statement(db: Session) -> None:
    report = run(db, tatra_bytes(), filename="download-12.09.2026.csv")
    record = report.bank_import
    assert (record.bank, record.filename) == (Bank.TATRA, "download-12.09.2026.csv")
    assert (record.rows_total, record.rows_new, record.rows_duplicate, record.rows_skipped) == (
        31,
        31,
        0,
        0,
    )
    assert db.scalar(select(func.count(Transaction.id))) == 31
    rent = db.scalars(select(Transaction).where(Transaction.message == "Najom 12345")).one()
    assert rent.counterparty_iban == "SK3211000000001000002222"
    assert rent.iban_masked == "SK** **** 2222"
    assert rent.label == "SK** **** 2222"
    assert rent.classification is Classification.INCOMING_FROM_PERSON
    assert rent.raw_json["Variabilný symbol"] == "12345"
    tempo = db.scalars(select(Transaction).where(Transaction.amount_minor == 146)).one()
    assert tempo.counterparty_norm == "potraviny plzen tempo"


def test_flags_shared_payments_with_the_rules_alone(db: Session) -> None:
    report = run(db, tatra_bytes())
    assert flagged_names(report) == RULE_FLAGGED
    assert all(p.kind is ProposalKind.SPLIT_TRANSACTION for p in report.flagged)
    assert report.flagged[0].summary == "Potraviny Sever dakuje 197, 22.54 € — split?"
    assert report.flagged[0].confidence is None  # a rule, not Claude
    assert report.unclassified == 9
    assert report.summary == (
        "31 new, 6 flagged for splitting, 6 payments received, 9 unclassified. "
        "Claude isn't configured, so some rows weren't classified."
    )


def test_claude_classifies_the_rest_and_can_flag_more(db: Session) -> None:
    # The rules leave 9 card payments; Claude calls the 3rd (Sportmax) shared.
    answers = [(ref, Classification.PERSONAL) for ref in range(9) if ref != 2]
    claude = FakeClassifier([*answers, (2, Classification.LIKELY_SHARED)])
    report = run(db, tatra_bytes(), classifier=claude)
    (rows,) = claude.calls
    assert [r.description for r in rows][:3] == [
        "KIOSK NAROZI 12",
        "APPLE.COM/BILL",
        "Sportmax,BN Centrum",
    ]
    prompt_text = " ".join(f"{r.description} {r.amount} {r.when}" for r in rows)
    assert "400000" not in prompt_text and "NOVÁK" not in prompt_text  # no card number or name
    assert flagged_names(report)[0] == "Sportmax,BN Centrum"
    assert len(report.flagged) == 7
    assert report.flagged[0].confidence == 0.8
    assert report.unclassified == 0
    assert report.summary == "31 new, 7 flagged for splitting, 6 payments received"


def test_reimporting_the_same_file_adds_nothing(db: Session) -> None:
    run(db, tatra_bytes())
    again = run(db, tatra_bytes())
    record = again.bank_import
    assert (record.rows_new, record.rows_duplicate) == (0, 31)
    assert again.flagged == [] and again.new == []
    assert again.summary == "0 new, 31 already imported, 0 flagged for splitting"
    assert db.scalar(select(func.count(Transaction.id))) == 31
    assert db.scalar(select(func.count(BankImport.id))) == 2


def test_an_overlapping_file_only_adds_the_new_rows(db: Session) -> None:
    """ROADMAP Phase 6: re-importing an overlapping file yields 0 new rows for the overlap."""
    _, rows = tatra_lines()
    older, newer = rows[10:], rows[:20]  # the bank lists newest first
    first = run(db, tatra_file(older))
    assert first.bank_import.rows_new == 21
    second = run(db, tatra_file(newer))
    assert (second.bank_import.rows_new, second.bank_import.rows_duplicate) == (10, 10)
    assert run(db, tatra_bytes()).bank_import.rows_new == 0
    assert db.scalar(select(func.count(Transaction.id))) == 31


def test_identical_rows_in_one_file_are_both_kept(db: Session) -> None:
    """Two same-day coffees at the same place: the occurrence number tells them apart."""
    _, rows = tatra_lines()
    doubled = [rows[0], *rows]
    assert run(db, tatra_file(doubled)).bank_import.rows_new == 32
    assert run(db, tatra_file(doubled)).bank_import.rows_new == 0
    assert run(db, tatra_file(rows)).bank_import.rows_new == 0


def test_the_direction_is_part_of_the_hash(db: Session) -> None:
    """The fixture has 5.00 € in and 5.00 € out, same day, same account: two rows."""
    run(db, tatra_bytes())
    pair = db.scalars(
        select(Transaction).where(
            Transaction.amount_minor == 500, Transaction.booking_date == date(2026, 7, 7)
        )
    ).all()
    assert {t.direction for t in pair} == {TransactionDirection.IN, TransactionDirection.OUT}


def test_banks_are_detected_or_checked(db: Session) -> None:
    report = run(db, revolut_bytes())
    assert report.bank_import.bank is Bank.REVOLUT
    assert report.bank_import.rows_new == 5
    assert report.flagged == [] and report.unclassified == 0  # top-ups and exchanges
    with pytest.raises(InvalidInputError, match="not revolut"):
        run(db, tatra_bytes(), bank=Bank.REVOLUT)
    with pytest.raises(InvalidInputError, match="unrecognized header"):
        run(db, b"not,a,statement\n")


# --- Push ---


def test_one_push_per_import(db: Session) -> None:
    sender = FakeSender()
    report = run(db, tatra_bytes())
    notification = notify_splits(
        db, sender, report, base_url="https://orbit.example", quiet=QUIET, now=NOON
    )
    assert notification is not None and notification.kind == "split_transaction"
    (push,) = sender.sent
    assert push.title == "🧾 6 payments to split?"
    assert push.body == (
        "Potraviny Sever dakuje 197 22.54 €, NORTHWOOD CAFE 19.33 €, "
        "KINO ALFA,PL,CENTRUM 19.50 € and 3 more"
    )
    assert push.click == "https://orbit.example/inbox"
    assert all(a.kind == "view" for a in push.actions)  # no one-tap confirm for splits
    assert notify_splits(db, sender, report, base_url="https://x", quiet=QUIET, now=NOON) is None
    assert len(sender.sent) == 1


def test_a_single_flagged_payment_links_to_its_split(db: Session) -> None:
    _, rows = tatra_lines()
    (airbnb,) = [r for r in rows if "AIRBNB" in r]
    report = run(db, tatra_file([airbnb]))
    sender = FakeSender()
    notify_splits(db, sender, report, base_url="https://orbit.example", quiet=QUIET, now=NOON)
    (push,) = sender.sent
    assert push.title == "🧾 Split?"
    assert push.body == "PAYPAL *AIRBNB EX1234X, 600.90 € — split?"
    assert push.click == f"https://orbit.example/inbox/{report.flagged[0].id}"


def test_no_push_without_flagged_payments(db: Session) -> None:
    sender = FakeSender()
    report = run(db, revolut_bytes())
    assert notify_splits(db, sender, report, base_url="https://x", quiet=QUIET, now=NOON) is None
    assert sender.sent == [] and db.scalar(select(func.count(Notification.id))) == 0
