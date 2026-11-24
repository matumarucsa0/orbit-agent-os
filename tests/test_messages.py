from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy.orm import Session

from orbit.domain.bank import Transaction
from orbit.domain.enums import DebtDirection, DebtType
from orbit.domain.ledger import Debt
from orbit.domain.people import Person
from orbit.services import ledger, messages, payments, people
from orbit.services.errors import InvalidInputError

IBAN = "SK3112000000198742637541"


def person(db: Session, name: str = "Patrik Veselý", **fields: Any) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name, **fields))


def debt(db: Session, who: Person, **fields: Any) -> Debt:
    values: dict[str, Any] = {"person_id": who.id, "direction": DebtDirection.THEY_OWE_ME}
    values.update(fields)
    return ledger.create_debt(db, ledger.DebtCreate(**values), "EUR")


def test_slovak_request_lists_debts_total_iban_and_codes(db: Session) -> None:
    pato = person(db, first_name="Paťo")
    pizza = debt(db, pato, amount_minor=1200, description="pizza")
    kino = debt(db, pato, amount_minor=125050, description="kino")
    (request,) = messages.payment_requests(
        pato, ledger.list_debts(db, person_id=pato.id), iban=IBAN, holder="Alex N."
    )
    assert request.currency == "EUR"
    assert request.total_minor == 126250
    assert request.text == "\n".join(
        [
            "Ahoj Paťo, posielam prehľad, čo mi ešte dlžíš:",
            f"• pizza: 12,00 € ({pizza.reference_code})",
            f"• kino: 1\N{NO-BREAK SPACE}250,50 € ({kino.reference_code})",
            "Spolu: 1\N{NO-BREAK SPACE}262,50 €",
            "IBAN: SK31 1200 0000 1987 4263 7541",
            "Príjemca: Alex N.",
            f"Do poznámky pre príjemcu prosím napíš: {pizza.reference_code} {kino.reference_code}",
            "Vďaka!",
        ]
    )


def test_codes_in_the_text_match_the_payment_back(db: Session) -> None:
    pato = person(db)
    first = debt(db, pato, amount_minor=1200)
    second = debt(db, pato, amount_minor=800)
    (request,) = messages.payment_requests(pato, [first, second])
    codes_line = request.text.splitlines()[-2]
    paid = Transaction(message=codes_line.split(": ", 1)[1])
    assert payments.reference_codes(paid) == [first.reference_code, second.reference_code]


@pytest.mark.parametrize(
    ("country", "intro", "amount"),
    [
        ("CZ", "Ahoj Patrik, posílám přehled, co mi ještě dlužíš:", "• 12,00 € ("),
        (None, "Hi Patrik, here's what's still open between us:", "• 12.00 € ("),
    ],
)
def test_language_follows_the_name_day_calendar(
    db: Session, country: str | None, intro: str, amount: str
) -> None:
    who = person(db, nameday_country=country)
    debt(db, who, amount_minor=1200)
    (request,) = messages.payment_requests(who, ledger.list_debts(db, person_id=who.id))
    lines = request.text.splitlines()
    assert lines[0] == intro
    assert lines[1].startswith(amount)
    assert not any(line.startswith(("Celkem", "Total", "IBAN")) for line in lines)  # one debt


def test_one_request_per_currency_only_for_what_they_owe(db: Session) -> None:
    pato = person(db)
    debt(db, pato, amount_minor=1000, currency="CZK")
    debt(db, pato, amount_minor=500)
    debt(db, pato, amount_minor=700, direction=DebtDirection.I_OWE_THEM)
    debt(db, pato, type=DebtType.ITEM, description="charger")
    ledger.settle(db, debt(db, pato, amount_minor=300))
    requests = messages.payment_requests(
        pato, ledger.list_debts(db, person_id=pato.id, is_open=None)
    )
    assert [(r.currency, r.total_minor) for r in requests] == [("CZK", 1000), ("EUR", 500)]


def test_nothing_owed_no_request(db: Session) -> None:
    pato = person(db)
    debt(db, pato, amount_minor=700, direction=DebtDirection.I_OWE_THEM)
    assert messages.payment_requests(pato, ledger.list_debts(db, person_id=pato.id)) == []


def test_ask_back_an_item(db: Session) -> None:
    pato = person(db, first_name="Paťo")
    charger = debt(db, pato, type=DebtType.ITEM, description="nabíjačka")
    assert messages.ask_back(pato, charger, date(2026, 10, 3)) == (
        "Ahoj Paťo, len pripomínam, požičal som ti: nabíjačka (3. 10.). "
        "Vieš mi to vrátiť, keď sa uvidíme? Vďaka!"
    )
    english = person(db, "Anna", nameday_country=None)
    book = debt(db, english, type=DebtType.ITEM, description="book")
    assert "I lent you my book (3 Oct)" in messages.ask_back(english, book, date(2026, 10, 3))


def test_ask_back_only_items_i_lent(db: Session) -> None:
    pato = person(db)
    borrowed = debt(
        db, pato, type=DebtType.ITEM, description="drill", direction=DebtDirection.I_OWE_THEM
    )
    with pytest.raises(InvalidInputError):
        messages.ask_back(pato, borrowed, date(2026, 10, 3))


def test_address_name_falls_back_to_the_display_name(db: Session) -> None:
    assert messages.address_name(person(db, "@pato_ig")) == "@pato_ig"
    assert messages.address_name(person(db, "Terka Nováková")) == "Terka"
    assert messages.address_name(person(db, "X", first_name="Mária Anna")) == "Mária"


# --- Web ---


def test_person_page_offers_the_texts(web, make_settings, app) -> None:  # type: ignore[no-untyped-def]
    app.state.settings = make_settings(payment_iban="SK31 1200 0000 1987 4263 7541")
    with app.state.sessionmaker() as session:
        pato = person(session, first_name="Paťo")
        debt(session, pato, amount_minor=1200, description="pizza")
        debt(session, pato, type=DebtType.ITEM, description="nabíjačka")
        session.commit()
        pato_id = pato.id
    page = web.get(f"/people/{pato_id}").text
    assert "Ask to pay 12.00 €" in page
    assert "IBAN: SK31 1200 0000 1987 4263 7541" in page
    assert "Ask for it back" in page
    assert "požičal som ti: nabíjačka" in page
