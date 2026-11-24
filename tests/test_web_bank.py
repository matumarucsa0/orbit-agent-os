from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from orbit.domain.bank import Transaction
from orbit.domain.enums import TransactionDirection
from orbit.domain.ledger import Debt
from tests.test_bank_parsers import tatra_bytes
from tests.test_web_inbox import proposal_ids
from tests.test_web_ledger import post, text
from tests.test_web_people import add

MINUS = "\N{MINUS SIGN}"  # outgoing amounts on the import page


def upload(web: TestClient, raw: bytes, bank: str = "auto") -> str:
    response = web.post(
        "/import",
        files={"file": ("download-12.09.2026.csv", raw, "text/csv")},
        data={"bank": bank},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return web.get(response.headers["location"]).text


def split_form_url(page: str) -> str:
    match = re.search(r'href="(/inbox/\d+)">Split…', page)
    assert match, page
    return match.group(1)


def test_import_pages_require_login(client: TestClient) -> None:
    for method, path in (
        ("get", "/import"),
        ("post", "/import"),
        ("get", "/import/1"),
        ("post", "/transactions/1/split"),
        ("get", "/inbox/1/split-search"),
    ):
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_upload_shows_the_summary_and_the_flagged_payments(web: TestClient) -> None:
    page = web.get("/import").text
    assert "Upload a bank statement CSV" in text(page)
    page = upload(web, tatra_bytes())
    assert "31 new, 6 flagged for splitting" in text(page)
    assert f"Bar Restaurant Example {MINUS}39.10 €" in text(page)
    assert f"PAYPAL *AIRBNB EX1234X {MINUS}600.90 €" in text(page)
    assert "Plzen Tempo" not in text(page)  # the default view lists only "Shared?"
    assert text(page).count("Split…") == 6

    everything = web.get("/import/1?show=all").text
    assert f"Potraviny Plzen Tempo {MINUS}1.46 €" in text(everything)
    assert "SK** **** 2222 +1,000.00 €" in text(everything)  # masked IBAN, never the full one
    assert "1000002222" not in everything

    again = upload(web, tatra_bytes())
    assert "0 new, 31 already imported, 0 flagged for splitting" in text(again)
    listing = web.get("/import").text
    assert "31 new" in text(listing) and "0 new, 31 already imported" in text(listing)


def test_upload_errors_are_shown(web: TestClient) -> None:
    page = upload(web, tatra_bytes(), bank="revolut")
    assert "This looks like a tatra statement, not revolut." in text(page)


def test_split_a_flagged_dinner(web: TestClient, app: FastAPI) -> None:
    """ROADMAP Phase 6 'done when': splitting one creates the right debts."""
    pato = add(web, "Patrik Veselý")
    terka = add(web, "Terka Nová")
    page = upload(web, tatra_bytes())
    restaurant = re.search(r'Bar Restaurant Example.*?href="(/inbox/\d+)"', page, re.S)
    assert restaurant
    url = restaurant.group(1)

    form = web.get(url).text
    assert "Who was in?" in text(form)
    assert "Bar Restaurant Example · 39.10 €" in text(form)

    # Picking people: search, then add a row (HTMX fragments).
    results = web.get(f"{url}/split-search", params={"q": "patrik vesely"}).text
    assert f"/split/row?person_id={pato}" in results
    results = web.get(f"{url}/split-search", params={"q": "vesely", "listed": [pato]}).text
    assert "Nobody matches" in text(results)
    row = web.get(f"{url}/split-row", params={"person_id": terka}).text
    assert f'name="person" value="{terka}" checked' in row

    # Save shows a preview; Confirm creates the debts.
    page = post(web, f"{url}/save", full="1", person=str(pato), mode="even", include_me="1",
                description="večera Vlora")  # fmt: skip
    assert "Patrik Veselý owes you 19.55 €" in text(page)
    response = web.post(
        f"{url}/confirm",
        data={"full": "1", "person": [str(pato), str(terka)], "mode": "even", "include_me": "1",
              "description": "večera Vlora"},
        follow_redirects=False,
    )  # fmt: skip
    assert response.status_code == 303
    page = web.get(response.headers["location"]).text
    assert "Split into 2 debts: Patrik Veselý owes you 13.03 €; Terka Nová owes you 13.03 €." in (
        text(page)
    )
    with app.state.sessionmaker() as session:
        debts = session.scalars(select(Debt).order_by(Debt.id)).all()
    assert [(d.person_id, d.amount_minor, d.currency, d.description) for d in debts] == [
        (pato, 1303, "EUR", "večera Vlora"),
        (terka, 1303, "EUR", "večera Vlora"),
    ]
    assert debts[0].source_transaction_id is not None

    # The import page now says it's split, and offers no second split.
    listing = web.get("/import/1").text
    assert "split confirmed" in text(listing)
    assert text(listing).count("Split…") == 5


def test_custom_split_and_its_errors(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = upload(web, tatra_bytes())
    url = split_form_url(page)  # groceries, 22.54 €
    page = post(web, f"{url}/confirm", full="1", person=str(pato), mode="custom",
                **{f"amount-{pato}": "30"})  # fmt: skip
    assert "The shares add up to more than the payment (22.54 €)." in text(page)
    assert 'value="30.00"' in page  # the edit was kept
    page = post(web, f"{url}/confirm", full="1", person=str(pato), mode="custom",
                **{f"amount-{pato}": "10,50"})  # fmt: skip
    assert "Split into 1 debt: Pato owes you 10.50 €." in text(page)


def test_confirm_without_people_asks_for_them(web: TestClient) -> None:
    page = upload(web, tatra_bytes())
    url = split_form_url(page)
    page = post(web, f"{url}/confirm", full="1", mode="even", include_me="1")
    assert "Pick at least one person to split with." in text(page)


def test_not_shared_rejects_it(web: TestClient) -> None:
    upload(web, tatra_bytes())
    inbox = web.get("/inbox").text
    assert "🧾 Split?" in inbox
    assert "Potraviny Sever dakuje 197 · 22.54 €" in text(inbox)
    first = proposal_ids(inbox)[-1]
    page = post(web, f"/inbox/{first}/reject")
    assert "Rejected." in text(page)
    assert len(proposal_ids(page)) == 5 + 6  # the other splits + 6 payments received


def test_split_an_unflagged_payment_by_hand(web: TestClient, app: FastAPI) -> None:
    upload(web, tatra_bytes())
    page = web.get("/import/1?show=all").text
    match = re.search(r'Sportmax,BN Centrum.*?href="/transactions/(\d+)/split', page, re.S)
    assert match
    tx_id = match.group(1)
    pending = len(proposal_ids(web.get("/inbox").text))
    # The form opens without storing anything: backing out leaves no Inbox item.
    form = web.get(f"/transactions/{tx_id}/split").text
    assert "Sportmax,BN Centrum · 15.95 €" in text(form)
    assert f'action="/transactions/{tx_id}/split/confirm"' in form
    assert len(proposal_ids(web.get("/inbox").text)) == pending
    # Saving it makes it an Inbox item, and the form link then goes there.
    response = web.post(
        f"/transactions/{tx_id}/split/save", data={"full": "1"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].startswith("/inbox/")
    assert len(proposal_ids(web.get("/inbox").text)) == pending + 1
    again = web.get(f"/transactions/{tx_id}/split", follow_redirects=False)
    assert again.headers["location"] == response.headers["location"]
    # An incoming payment can't be split.
    with app.state.sessionmaker() as session:
        rent = session.scalars(
            select(Transaction).where(Transaction.message == "Najom 12345")
        ).one()
    page = post(web, f"/transactions/{rent.id}/split")
    assert "Only outgoing payments can be split." in text(page)


def test_transactions_across_imports(web: TestClient) -> None:
    upload(web, tatra_bytes())
    page = web.get("/transactions").text
    assert f"Potraviny Plzen Tempo {MINUS}1.46 €" in text(page)
    assert "SK** **** 2222 +1,000.00 €" in text(page)
    assert "1000002222" not in page

    found = web.get("/transactions", params={"q": "restaurant"}).text
    assert f"Bar Restaurant Example {MINUS}39.10 €" in text(found) and "Plzen Tempo" not in text(
        found
    )
    by_amount = web.get("/transactions", params={"q": "39,10"}).text
    assert "Bar Restaurant Example" in text(by_amount) and "Plzen Tempo" not in text(by_amount)
    received = web.get("/transactions", params={"show": "received"}).text
    assert "+1,000.00 €" in text(received) and "Plzen Tempo" not in text(received)
    assert "Nothing matches" in text(web.get("/transactions", params={"q": "zzzz"}).text)

    live = web.get(
        "/transactions",
        params={"q": "tempo"},
        headers={"HX-Request": "true", "HX-Target": "results"},
    )
    assert "<h1>" not in live.text and "Plzen Tempo" in live.text


def test_split_from_transactions_goes_back_there_on_error(web: TestClient, app: FastAPI) -> None:
    upload(web, tatra_bytes())
    with app.state.sessionmaker() as session:
        incoming = session.scalars(
            select(Transaction.id).where(Transaction.direction == TransactionDirection.IN)
        ).first()
    assert incoming is not None
    response = web.post(
        f"/transactions/{incoming}/split",
        data={"back": "/transactions?show=received"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/transactions?show=received"
    assert "Only outgoing payments can be split." in text(
        web.get(response.headers["location"]).text
    )
    offsite = web.post(
        f"/transactions/{incoming}/split",
        data={"back": "https://evil.example"},
        follow_redirects=False,
    )
    assert offsite.headers["location"] == "/"
