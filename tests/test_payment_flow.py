"""Phase 7 end to end: a friend pays me back -> a push -> one tap settles the right debt."""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from orbit.domain.inbox import Notification
from orbit.domain.ledger import Debt
from tests.test_api_bank_import import upload as api_upload
from tests.test_bank_parsers import tatra_bytes
from tests.test_web_bank import upload as web_upload
from tests.test_web_inbox import proposal_ids
from tests.test_web_ledger import post, text
from tests.test_web_people import add

# In tests/fixtures/bank/tatra.csv the account ending 2222 sends 1,000 € ("Najom 12345"),
# 15 € ("Zaloha na jedlo") and 20 € ("Vieden vecera"); three other accounts send one each.
PATO_IBAN = "SK32 1100 0000 0010 0000 2222"


def pato_with_debts(api: TestClient) -> tuple[int, dict[str, int]]:
    pato = api.post("/api/v1/people", json={"display_name": "Patrik Veselý"}).json()["id"]
    handle = api.post(f"/api/v1/people/{pato}/handles", json={"type": "iban", "value": PATO_IBAN})
    assert handle.status_code == 201, handle.text
    debts = {}
    for description, amount in (("jedlo", 1500), ("Viedeň", 2000)):
        body = {
            "person_id": pato,
            "direction": "they_owe_me",
            "amount_minor": amount,
            "description": description,
        }
        debts[description] = api.post("/api/v1/debts", json=body).json()["id"]
    return pato, debts


def pushes(app: FastAPI) -> dict[str, Notification]:
    with app.state.sessionmaker() as session:
        return {n.title: n for n in session.scalars(select(Notification))}


def action(push: Notification, label: str) -> str:
    (found,) = [a["url"] for a in push.payload_json["actions"] if a["label"] == label]
    return str(found).removeprefix("http://testserver")


def test_payment_back_push_and_one_tap_settle(api: TestClient, app: FastAPI) -> None:
    _, debts = pato_with_debts(api)
    body = api_upload(api, tatra_bytes()).json()
    assert body["payments"] == 6  # 3 from him (his rent too), 3 from unknown senders
    assert body["summary"].startswith("31 new, 6 flagged for splitting, 6 payments received")

    # Nothing is ever settled without a tap.
    open_ids = {d["id"] for d in api.get("/api/v1/debts").json()}
    assert open_ids == set(debts.values())

    found = pushes(app)
    assert set(found) == {
        "🧾 6 payments to split?",
        "💶 Received 1,000.00 € from Patrik Veselý",
        "💶 Received 15.00 € from Patrik Veselý",
        "💶 Received 20.00 € from Patrik Veselý",
        "💶 3 payments from unknown senders",
    }
    food = found["💶 Received 15.00 € from Patrik Veselý"]
    assert food.body == "“Zaloha na jedlo” · Matches: jedlo 15.00 €"
    assert [a["label"] for a in food.payload_json["actions"]] == ["Settle", "Choose…", "Not a debt"]
    rent = found["💶 Received 1,000.00 € from Patrik Veselý"]
    assert rent.body == "“Najom 12345” · No open EUR debts match."
    assert [a["label"] for a in rent.payload_json["actions"]] == ["Choose…", "Not a debt"]
    assert "1000002222" not in str([n.payload_json for n in found.values()])

    # One tap on Settle, without the bearer token.
    phone = TestClient(app)
    response = phone.post(action(food, "Settle"))
    assert (response.status_code, response.text) == (
        200,
        "Settled 1 debt from Patrik Veselý: jedlo 15.00 €.",
    )
    settled = api.get(f"/api/v1/debts/{debts['jedlo']}").json()
    assert settled["settled_at"] is not None and settled["settled_by_transaction_id"] is not None
    assert api.get(f"/api/v1/debts/{debts['Viedeň']}").json()["settled_at"] is None
    again = phone.post(action(food, "Settle"))
    assert (again.status_code, again.text) == (409, "Already confirmed.")

    response = phone.post(action(rent, "Not a debt"))
    assert (response.status_code, response.text) == (200, "Rejected.")
    assert api.get("/api/v1/balances").json()["people"][0]["amount_minor"] == 2000


def test_settle_that_no_longer_fits_points_to_the_inbox(api: TestClient, app: FastAPI) -> None:
    _, debts = pato_with_debts(api)
    api_upload(api, tatra_bytes())
    api.post(f"/api/v1/debts/{debts['Viedeň']}/settle", json={})  # settled by hand meanwhile
    vienna = pushes(app)["💶 Received 20.00 € from Patrik Veselý"]
    response = TestClient(app).post(action(vienna, "Settle"))
    assert response.status_code == 422
    assert "was already settled. Open the Inbox to finish it." in response.text


def test_reimport_proposes_nothing_new(api: TestClient) -> None:
    pato_with_debts(api)
    api_upload(api, tatra_bytes())
    again = api_upload(api, tatra_bytes()).json()
    assert (again["payments"], again["payment_proposal_ids"]) == (0, [])


# --- Web ---


def payment_ids(page: str) -> list[int]:
    return [int(i) for i in re.findall(r'href="/inbox/(\d+)">Choose…', page)]


def test_inbox_card_and_choose_page(web: TestClient, app: FastAPI) -> None:
    pato_id = add(web, "Patrik Veselý", handle_type="iban", handle_value=PATO_IBAN)
    pato_url = f"/people/{pato_id}"
    for description, amount in (("jedlo", "10"), ("kino", "5"), ("Viedeň", "20")):
        post(web, f"{pato_url}/debts", direction="they_owe_me", type="money", amount=amount,
             currency="EUR", description=description)  # fmt: skip
    web_upload(web, tatra_bytes())

    inbox = web.get("/inbox").text
    assert "Patrik Veselý · recognized by their IBAN" in text(inbox)
    assert "jedlo 10.00 € kino 5.00 €" in text(inbox)  # 15 € = jedlo + kino
    assert "Who sent it?" in text(inbox)  # the unknown senders

    with app.state.sessionmaker() as session:
        food_id = session.scalar(select(Debt.id).where(Debt.description == "jedlo"))
    url = next(u for u in payment_ids(inbox) if "15.00 €" in text(web.get(f"/inbox/{u}").text))
    page = web.get(f"/inbox/{url}").text
    assert "What does it pay?" in text(page)
    assert "Adds up: jedlo 10.00 € + kino 5.00 €" in text(page)

    # Pick by hand: only 5 € of the food.
    page = post(web, f"/inbox/{url}/save", full="1", person=str(pato_id), option="custom",
                debt=str(food_id), **{f"amount-{food_id}": "5"})  # fmt: skip
    assert "5.00 € of jedlo 10.00 €" in text(page)
    assert "Settles 5.00 € of the 15.00 € received." in text(page)
    page = post(web, f"/inbox/{url}/confirm", full="1", person=str(pato_id), option="custom",
                debt=str(food_id), **{f"amount-{food_id}": "5"})  # fmt: skip
    assert "Settled 1 debt from Patrik Veselý: 5.00 € of jedlo 10.00 €" in text(page)
    person_page = web.get(pato_url).text
    assert "by bank payment" in text(person_page)


def test_unknown_sender_pick_then_settle(web: TestClient, app: FastAPI) -> None:
    terka_id = str(add(web, "Terka"))
    terka_url = f"/people/{terka_id}"
    post(web, f"{terka_url}/debts", direction="they_owe_me", type="money", amount="24.40",
         currency="EUR", description="výlet")  # fmt: skip
    web_upload(web, tatra_bytes())
    inbox = web.get("/inbox").text
    trip = next(
        u for u in payment_ids(inbox) if "Vylet" in web.get(f"/inbox/{u}").text
    )  # 24.40 € from the account ending 5555
    page = web.get(f"/inbox/{trip}").text
    assert "Who sent it?" in text(page) and "Next" in text(page)
    assert "Recognize the account SK** **** 5555 as them next time" in text(page)
    assert "1000005555" not in page

    # Picking a person on Confirm doesn't settle anything yet: their debts are shown first.
    page = post(web, f"/inbox/{trip}/confirm", full="1", person=terka_id, remember="1")
    assert "Now check what the payment settles." in text(page)
    assert "Exact amount: výlet 24.40 €" in text(page)
    with app.state.sessionmaker() as session:
        assert session.scalar(select(Debt.settled_at)) is None
    page = post(web, f"/inbox/{trip}/confirm", full="1", person=terka_id, remember="1", option="0")
    assert "Settled 1 debt from Terka: výlet 24.40 €." in text(page)
    assert "Terka will be recognized by the IBAN from now on." in text(page)


def test_import_page_lists_received_payments_and_matches_by_hand(web: TestClient) -> None:
    web_upload(web, tatra_bytes())
    page = web.get("/import/1?show=received").text
    assert "Najom 12345" in text(page)
    assert text(page).count("Match…") == 6
    assert "payment pending" in text(page)

    # Reject one, then match it by hand again from the Import page.
    first = proposal_ids(web.get("/inbox").text)[0]
    post(web, f"/inbox/{first}/reject")
    page = web.get("/import/1?show=received").text
    tx_id = re.search(r'href="/transactions/(\d+)/match', page)
    assert tx_id and "payment rejected" in text(page)
    response = web.post(f"/transactions/{tx_id.group(1)}/match", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/inbox/")
    # Outgoing payments can't be matched.
    card = re.search(r'href="/transactions/(\d+)/split', web.get("/import/1?show=all").text)
    assert card
    page = post(web, f"/transactions/{card.group(1)}/match")
    assert "Only incoming payments settle debts." in text(page)
