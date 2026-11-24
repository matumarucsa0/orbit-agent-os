from __future__ import annotations

from sqlalchemy.orm import Session

from orbit.domain.enums import Classification, TransactionDirection
from orbit.services import transactions
from orbit.services.transactions import search_transactions
from tests.test_split import payment


def test_search_is_diacritics_insensitive_across_merchant_and_message(db: Session) -> None:
    lokal = payment(db, 4800, "Lokál")
    lokal.counterparty_norm = "lokal"
    transfer = payment(db, 1200, "PATRIK VESELÝ", direction=TransactionDirection.IN)
    transfer.counterparty_norm = "patrik vesely"
    transfer.message, transfer.message_norm = "pizza + kino", "pizza + kino"
    db.flush()

    assert search_transactions(db, "LOKÁL") == [lokal]
    assert search_transactions(db, "veselý pizza") == [transfer]  # every word must match
    assert search_transactions(db, "vesely lokal") == []
    assert search_transactions(db, "12") == [transfer]  # an amount
    assert search_transactions(db, "48,00") == [lokal]
    assert search_transactions(db, "%") == []  # LIKE wildcards are escaped
    assert set(search_transactions(db)) == {lokal, transfer}


def test_filters_and_paging(db: Session) -> None:
    shared = payment(db, 4800, "Lokál")
    shared.classification = Classification.LIKELY_SHARED
    incoming = payment(db, 1200, "Pato", direction=TransactionDirection.IN)
    unclassified = payment(db, 300, "Kiosk")
    db.flush()
    assert search_transactions(db, show="shared") == [shared]
    assert search_transactions(db, show="received") == [incoming]
    assert unclassified in search_transactions(db, show="unclassified")
    assert shared not in search_transactions(db, show="unclassified")
    first, second = search_transactions(db, limit=2), search_transactions(db, limit=2, offset=2)
    assert len(first) == 2 and len(second) == 1 and not set(first) & set(second)


def test_count_by_show(db: Session) -> None:
    assert transactions.count_by_show(db) == {
        "shared": 0,
        "received": 0,
        "unclassified": 0,
        "all": 0,
    }
    shared = payment(db, 4800, "Lokál")
    shared.classification = Classification.LIKELY_SHARED
    payment(db, 1200, "Pato", direction=TransactionDirection.IN)
    payment(db, 300, "Kaufland")
    db.flush()
    assert transactions.count_by_show(db) == {
        "shared": 1,
        "received": 1,
        "unclassified": 2,
        "all": 3,
    }
    assert transactions.count_by_show(db, import_id=shared.import_id)["all"] == 1  # one per import
    assert transactions.count_by_show(db, import_id=-1)["all"] == 0
