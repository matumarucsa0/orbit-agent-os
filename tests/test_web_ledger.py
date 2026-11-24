from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tests.test_web_people import add


def post(web: TestClient, url: str, **data: str) -> str:
    """POST a form, follow the redirect and return the page it lands on."""
    response = web.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return web.get(response.headers["location"]).text


def text(page: str) -> str:
    """Visible text, roughly: tags stripped, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def debt_ids(page: str, action: str) -> list[int]:
    # Money debts have two settle forms (full and part): dedupe, keep the order.
    found = re.findall(rf'action="/debts/(\d+)/{action}"', page)
    return [int(i) for i in dict.fromkeys(found)]


def test_ledger_pages_require_login(client: TestClient) -> None:
    response = client.get("/ledger", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    response = client.post("/debts/1/settle", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_done_when_scenario(web: TestClient) -> None:
    """ROADMAP Phase 3: log a lent charger, a favor I owe, a 12 € pizza; settle 5 € of it."""
    pato, terka = add(web, "Pato"), add(web, "Terka")

    post(web, f"/people/{pato}/debts", direction="they_owe_me", type="item", description="charger")
    post(web, f"/people/{terka}/debts", direction="i_owe_them", type="favor", description="moving")
    page = post(
        web,
        f"/people/{pato}/debts",
        direction="they_owe_me",
        type="money",
        amount="12",
        currency="EUR",
        description="pizza",
    )
    assert "Added 12.00 € (D-0001)." in text(page)
    assert "Owes you 12.00 €" in text(page)
    assert "Has your charger" in text(page)

    pizza = debt_ids(page, "settle")[-1]  # open debts oldest first; the pizza came last
    page = post(web, f"/debts/{pizza}/settle", amount="5", next=f"/people/{pato}#ledger")
    shown = text(page)
    assert "Partly settled; 7.00 € still open (D-0002)." in shown
    assert "Owes you 7.00 €" in shown
    assert "rest of D-0001" in shown
    assert "paid 5.00 €, the rest moved to D-0002" in shown

    terka_page = text(web.get(f"/people/{terka}").text)
    assert "You owe a favor: moving" in terka_page

    ledger = text(web.get("/ledger").text)
    assert "Owed to you 7.00 €" in ledger
    assert "You owe 0.00 €" not in ledger  # zero lines are left out
    assert "Who owes me 1 Pato 7.00 €" in ledger
    assert "Lent out 1 Pato · charger" in ledger
    assert "I owe 1 Terka · moving" in ledger


def test_add_money_validation_messages(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="")
    assert "Enter an amount" in text(page)
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="abc")
    assert "isn't an amount" in text(page)
    page = post(
        web,
        f"/people/{pato}/debts",
        direction="they_owe_me",
        type="money",
        amount="5",
        currency="EURO",
    )
    assert "Check the amount and currency" in text(page)
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="item")
    assert "Describe the item" in text(page)
    assert "Nothing open between you." in text(page)


def test_add_item_ignores_money_fields(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(
        web,
        f"/people/{pato}/debts",
        direction="they_owe_me",
        type="item",
        amount="5",
        currency="EUR",
        description="book",
        reminder_days="30",
    )
    assert "Added item: book." in text(page)


def test_czk_and_default_currency(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(
        web,
        f"/people/{pato}/debts",
        direction="i_owe_them",
        type="money",
        amount="250",
        currency="czk",
    )
    assert "You owe 250.00 Kč" in text(page)
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="3,50")
    assert "Owes you 3.50 €" in text(page)


def test_settle_full_undo_and_delete(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="10")
    (debt,) = debt_ids(page, "settle")
    page = post(web, f"/debts/{debt}/settle")
    assert "Settled." in text(page)
    assert "Nothing open between you." in text(page)
    assert debt_ids(page, "unsettle") == [debt]

    page = post(web, f"/debts/{debt}/delete")
    assert "can't be deleted" in text(page)

    page = post(web, f"/debts/{debt}/unsettle")
    assert "Settlement undone." in text(page)
    assert "Owes you 10.00 €" in text(page)
    page = post(web, f"/debts/{debt}/delete")
    assert "Deleted." in text(page)
    assert "Nothing open between you." in text(page)


def test_partial_settle_errors(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="10")
    (debt,) = debt_ids(page, "settle")
    assert "more than is owed" in text(post(web, f"/debts/{debt}/settle", amount="11"))
    assert "isn't an amount" in text(post(web, f"/debts/{debt}/settle", amount="x"))
    page = post(
        web, f"/people/{pato}/debts", direction="they_owe_me", type="item", description="pen"
    )
    item = debt_ids(page, "settle")[-1]
    assert "only be settled in full" in text(post(web, f"/debts/{item}/settle", amount="1"))


def test_settle_redirects_to_next_but_never_off_site(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(
        web, f"/people/{pato}/debts", direction="they_owe_me", type="item", description="pen"
    )
    (item,) = debt_ids(page, "settle")
    response = web.post(
        f"/debts/{item}/settle", data={"next": "//evil.example"}, follow_redirects=False
    )
    assert response.headers["location"] == "/"
    web.post(f"/debts/{item}/unsettle")
    response = web.post(f"/debts/{item}/settle", data={"next": "/ledger"}, follow_redirects=False)
    assert response.headers["location"] == "/ledger"


def test_promises_on_person_page_and_ledger(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/promises", direction="i_promised", text="send the photos")
    due = datetime.now(ZoneInfo("Europe/Prague")).date() + timedelta(days=7)
    assert f"Promise added (due {due.day} {due:%b})." in text(page)
    assert "You promised: send the photos" in text(page)

    page = post(
        web,
        f"/people/{pato}/promises",
        direction="they_promised",
        text="return book",
        due="2020-01-01",
    )
    assert "due 1 Jan 2020 · overdue" in text(page)

    ledger = text(web.get("/ledger").text)
    assert "Pato · You promised: send the photos" in ledger
    assert "Pato · They promised: return book" in ledger

    # Open promises are sorted by due date: the overdue one comes first.
    overdue_id = re.findall(r'action="/promises/(\d+)/status"', page)[0]
    page = post(web, f"/promises/{overdue_id}/status", status="done")
    assert "Closed (1)" in text(page)
    page = post(web, f"/promises/{overdue_id}/status", status="open")
    assert "Closed (1)" not in text(page)
    page = post(web, f"/promises/{overdue_id}/status", status="dropped")
    page = post(web, f"/promises/{overdue_id}/delete")
    assert "Promise deleted." in text(page)
    assert "return book" not in text(page)


def test_promise_needs_text(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/promises", direction="i_promised", text="  ")
    assert "A promise needs some text." in text(page)


def test_delete_person_with_open_debt_is_refused(web: TestClient) -> None:
    pato = add(web, "Pato")
    post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="1")
    page = post(web, f"/people/{pato}/delete")
    assert "still has open debts or promises" in text(page)
    assert web.get(f"/people/{pato}").status_code == 200


def test_empty_ledger_page(web: TestClient) -> None:
    page = text(web.get("/ledger").text)
    assert "No open money." in page
    assert "Nothing lent out." in page
    assert "No open promises." in page
