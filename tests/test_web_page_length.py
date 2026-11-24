"""Home "Needs attention" and the Ledger sections stay short as data grows (SPEC §2.25)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection
from orbit.services import ledger, people, promises
from orbit.web.ledger import LEDGER_PAGE
from orbit.web.pages import HOME_ATTENTION


def seed_overdue_promises(app: FastAPI, count: int) -> None:
    today = datetime.now(UTC).astimezone(app.state.settings.timezone).date()
    with app.state.sessionmaker() as db:
        for n in range(count):
            who = people.create_person(db, people.PersonCreate(display_name=f"Person {n:03}"))
            promises.create_promise(
                db,
                promises.PromiseCreate(
                    person_id=who.id,
                    direction=PromiseDirection.THEY_PROMISED,
                    text=f"send thing {n}",
                    due=today - timedelta(days=1),
                ),
                today - timedelta(days=10),
            )
        db.commit()


def seed_debts(app: FastAPI, count: int, debt_type: DebtType, amount: int | None = None) -> None:
    with app.state.sessionmaker() as db:
        for n in range(count):
            who = people.create_person(db, people.PersonCreate(display_name=f"Debtor {n:03}"))
            ledger.create_debt(
                db,
                ledger.DebtCreate(
                    person_id=who.id,
                    direction=DebtDirection.THEY_OWE_ME,
                    type=debt_type,
                    amount_minor=amount,
                    currency="EUR" if amount else None,
                    description=f"thing {n}",
                ),
                "EUR",
            )
        db.commit()


def test_home_shows_five_attention_rows_and_links_to_the_rest(
    web: TestClient, app: FastAPI
) -> None:
    seed_overdue_promises(app, 12)
    home = web.get("/").text
    assert home.count("send thing") == HOME_ATTENTION
    assert "…and 7 more" in home and 'href="/attention"' in home
    full = web.get("/attention").text
    assert full.count("send thing") == 12


def test_home_shows_no_more_link_when_everything_fits(web: TestClient, app: FastAPI) -> None:
    seed_overdue_promises(app, HOME_ATTENTION)
    home = web.get("/").text
    assert home.count("send thing") == HOME_ATTENTION
    assert "…and" not in home


def test_attention_page_when_empty_and_login(web: TestClient, client: TestClient) -> None:
    assert "Nothing needs you right now." in web.get("/attention").text


def test_attention_needs_login(client: TestClient) -> None:
    assert client.get("/attention", follow_redirects=False).status_code in (303, 401)


def test_home_order_attention_before_goals_and_money(web: TestClient, app: FastAPI) -> None:
    seed_overdue_promises(app, 1)
    home = web.get("/").text
    assert home.index("Needs attention") < home.index("Birthdays &amp; name days")


def test_ledger_caps_each_section_and_show_all_lifts_one(web: TestClient, app: FastAPI) -> None:
    seed_debts(app, 25, DebtType.ITEM)
    seed_debts(app, 25, DebtType.MONEY, amount=1000)
    page = web.get("/ledger").text
    assert len(re.findall(r"\bthing \d", page)) == LEDGER_PAGE  # lent items only
    assert page.count("Debtor ") == 2 * LEDGER_PAGE  # 20 lent rows + 20 owed rows
    assert "Show all 25" in page
    assert page.count('href="/ledger?all=lent#lent"') == 1
    assert page.count('href="/ledger?all=owed_to_me#owed_to_me"') == 1
    lent_all = web.get("/ledger?all=lent").text
    assert len(re.findall(r"\bthing \d", lent_all)) == 25
    assert 'href="/ledger?all=lent#lent"' not in lent_all
    assert 'href="/ledger?all=owed_to_me#owed_to_me"' in lent_all  # other sections stay capped


def test_ledger_totals_cover_every_row_not_just_the_visible_ones(
    web: TestClient, app: FastAPI
) -> None:
    seed_debts(app, 30, DebtType.MONEY, amount=1000)
    page = web.get("/ledger").text
    assert "Owed to you" in page and "300" in page.split("Owed to you", 1)[1][:40]
    assert page.count("Debtor ") == LEDGER_PAGE


def test_ledger_html_stays_small_with_many_rows(web: TestClient, app: FastAPI) -> None:
    seed_debts(app, 150, DebtType.ITEM)
    seed_debts(app, 150, DebtType.MONEY, amount=500)
    seed_overdue_promises(app, 150)
    assert len(web.get("/ledger").content) < 150_000
