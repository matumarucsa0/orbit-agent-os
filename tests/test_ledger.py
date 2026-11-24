from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbit.domain.enums import DebtDirection, DebtType
from orbit.domain.ledger import Debt
from orbit.domain.people import Person
from orbit.services import ledger, people
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.ledger import DebtCreate, DebtUpdate

OWES_ME = DebtDirection.THEY_OWE_ME
I_OWE = DebtDirection.I_OWE_THEM
PRAGUE = ZoneInfo("Europe/Prague")


def make(db: Session, name: str) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name))


def money(
    db: Session,
    person: Person,
    amount_minor: int,
    direction: DebtDirection = OWES_ME,
    currency: str | None = None,
    description: str | None = None,
) -> Debt:
    return ledger.create_debt(
        db,
        DebtCreate(
            person_id=person.id,
            direction=direction,
            amount_minor=amount_minor,
            currency=currency,
            description=description,
        ),
        default_currency="EUR",
    )


def thing(
    db: Session,
    person: Person,
    debt_type: DebtType,
    description: str,
    direction: DebtDirection = OWES_ME,
    **extra: object,
) -> Debt:
    return ledger.create_debt(
        db,
        DebtCreate(
            person_id=person.id,
            direction=direction,
            type=debt_type,
            description=description,
            **extra,  # type: ignore[arg-type]
        ),
        default_currency="EUR",
    )


def balance_map(db: Session, person_id: int | None = None) -> dict[tuple[str, str], int]:
    return {
        (b.person.display_name, b.currency): b.amount_minor for b in ledger.balances(db, person_id)
    }


# --- Create ---


def test_create_money_debt_defaults_currency_and_gets_code(db: Session) -> None:
    pato = make(db, "Pato")
    debt = money(db, pato, 1200, description="pizza")
    assert debt.type is DebtType.MONEY
    assert debt.currency == "EUR"
    assert debt.reference_code == "D-0001"
    assert debt.is_open
    assert pato.debts == [debt]


def test_create_money_debt_explicit_currency_is_uppercased(db: Session) -> None:
    debt = money(db, make(db, "Terka"), 25000, currency="czk")
    assert debt.currency == "CZK"


def test_reference_codes_are_sequential_and_money_only(db: Session) -> None:
    pato = make(db, "Pato")
    first = money(db, pato, 100)
    item = thing(db, pato, DebtType.ITEM, "charger")
    second = money(db, pato, 200)
    assert (first.reference_code, item.reference_code, second.reference_code) == (
        "D-0001",
        None,
        "D-0002",
    )


def test_reference_code_keeps_growing_past_four_digits(db: Session) -> None:
    pato = make(db, "Pato")
    debt = money(db, pato, 100)
    debt.reference_code = "D-9999"
    db.flush()
    assert ledger.next_reference_code(db) == "D-10000"
    money(db, pato, 100).reference_code = "D-10000"
    db.flush()
    assert ledger.next_reference_code(db) == "D-10001"  # not fooled by string order


def test_reference_code_is_unique_in_the_database(db: Session) -> None:
    pato = make(db, "Pato")
    money(db, pato, 100)
    money(db, pato, 100).reference_code = "D-0001"
    with pytest.raises(IntegrityError):
        db.flush()


def test_create_item_and_favor(db: Session) -> None:
    pato, terka = make(db, "Pato"), make(db, "Terka")
    charger = thing(db, pato, DebtType.ITEM, "charger")
    favor = thing(db, terka, DebtType.FAVOR, "helped me move", I_OWE)
    assert (charger.amount_minor, charger.currency) == (None, None)
    assert favor.direction is I_OWE
    assert favor.description == "helped me move"


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"type": DebtType.MONEY}, "amount"),
        ({"type": DebtType.ITEM}, "Describe"),
        ({"type": DebtType.FAVOR, "description": "   "}, "Describe"),
        ({"type": DebtType.ITEM, "description": "x", "amount_minor": 5}, "no amount"),
        ({"type": DebtType.FAVOR, "description": "x", "currency": "EUR"}, "no amount"),
        ({"type": DebtType.MONEY, "amount_minor": 5, "reminder_days": 3}, "reminder"),
    ],
)
def test_create_rejects_invalid_combinations(
    db: Session, fields: dict[str, object], message: str
) -> None:
    pato = make(db, "Pato")
    with pytest.raises(InvalidInputError, match=message):
        ledger.create_debt(
            db,
            DebtCreate(person_id=pato.id, direction=OWES_ME, **fields),  # type: ignore[arg-type]
            default_currency="EUR",
        )


def test_create_rejects_non_positive_amount_and_bad_currency() -> None:
    with pytest.raises(ValueError):
        DebtCreate(person_id=1, direction=OWES_ME, amount_minor=0)
    with pytest.raises(ValueError):
        DebtCreate(person_id=1, direction=OWES_ME, amount_minor=5, currency="EURO")


def test_create_for_missing_person(db: Session) -> None:
    with pytest.raises(NotFoundError):
        ledger.create_debt(db, DebtCreate(person_id=999, direction=OWES_ME, amount_minor=5), "EUR")


def test_database_rejects_money_without_amount(db: Session) -> None:
    pato = make(db, "Pato")
    db.add(Debt(person_id=pato.id, direction=OWES_ME, type=DebtType.MONEY, currency="EUR"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_database_rejects_item_without_description(db: Session) -> None:
    pato = make(db, "Pato")
    db.add(Debt(person_id=pato.id, direction=OWES_ME, type=DebtType.ITEM))
    with pytest.raises(IntegrityError):
        db.flush()


# --- Get / update / delete ---


def test_get_debt(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 100)
    assert ledger.get_debt(db, debt.id) is debt
    with pytest.raises(NotFoundError):
        ledger.get_debt(db, 999)


def test_update_open_debt(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200, description="pizza")
    ledger.update_debt(
        db,
        debt,
        DebtUpdate(amount_minor=1500, description="pizza + beer", due=date(2026, 10, 1)),
    )
    assert (debt.amount_minor, debt.description, debt.due) == (
        1500,
        "pizza + beer",
        date(2026, 10, 1),
    )
    ledger.update_debt(db, debt, DebtUpdate(direction=I_OWE))
    assert debt.direction is I_OWE


def test_update_only_touches_given_fields(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200, description="pizza")
    ledger.update_debt(db, debt, DebtUpdate(due=date(2026, 10, 1)))
    assert (debt.amount_minor, debt.description) == (1200, "pizza")


def test_update_cannot_break_invariants(db: Session) -> None:
    pato = make(db, "Pato")
    debt = money(db, pato, 1200)
    with pytest.raises(InvalidInputError):
        ledger.update_debt(db, debt, DebtUpdate(currency=None))
    item = thing(db, pato, DebtType.ITEM, "charger")
    with pytest.raises(InvalidInputError):
        ledger.update_debt(db, item, DebtUpdate(description=""))
    with pytest.raises(InvalidInputError):
        ledger.update_debt(db, item, DebtUpdate(amount_minor=100))
    with pytest.raises(InvalidInputError):
        ledger.update_debt(db, debt, DebtUpdate(direction=None))
    assert (debt.currency, item.description) == ("EUR", "charger")


def test_update_settled_debt_refused(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    ledger.settle(db, debt)
    with pytest.raises(InvalidInputError, match="settled"):
        ledger.update_debt(db, debt, DebtUpdate(amount_minor=5))


def test_delete_open_debt(db: Session) -> None:
    pato = make(db, "Pato")
    debt = money(db, pato, 1200)
    ledger.delete_debt(db, debt)
    assert pato.debts == []
    assert db.scalar(select(func.count(Debt.id))) == 0


def test_delete_settled_debt_refused(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    ledger.settle(db, debt)
    with pytest.raises(InvalidInputError):
        ledger.delete_debt(db, debt)


def test_delete_remainder_refused(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    rest = ledger.settle(db, debt, 500).remainder
    assert rest is not None
    with pytest.raises(InvalidInputError, match="partial"):
        ledger.delete_debt(db, rest)


# --- Settle ---


def test_full_settle(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    when = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
    result = ledger.settle(db, debt, now=when)
    assert result.settled is debt
    assert result.remainder is None
    assert debt.settled_at == when
    assert ledger.paid_amount(debt) == 1200


def test_settle_with_the_full_amount_is_a_full_settle(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    assert ledger.settle(db, debt, 1200).remainder is None
    assert not debt.is_open


def test_partial_settle_creates_linked_remainder(db: Session) -> None:
    pato = make(db, "Pato")
    debt = money(db, pato, 1200, description="pizza")
    debt.due = date(2026, 10, 1)
    result = ledger.settle(db, debt, 500)
    rest = result.remainder
    assert rest is not None
    assert not debt.is_open
    assert debt.amount_minor == 1200  # the original stays as the historical record
    assert (rest.amount_minor, rest.currency, rest.description, rest.due) == (
        700,
        "EUR",
        "pizza",
        date(2026, 10, 1),
    )
    assert rest.direction is OWES_ME
    assert rest.is_open
    assert rest.parent is debt
    assert debt.remainder is rest
    assert rest.reference_code == "D-0002"
    assert ledger.paid_amount(debt) == 500
    assert ledger.paid_amount(rest) is None  # still open


def test_partial_settle_chain(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    second = ledger.settle(db, debt, 500).remainder
    assert second is not None
    third = ledger.settle(db, second, 200).remainder
    assert third is not None
    assert third.amount_minor == 500
    assert third.parent is second
    assert ledger.settle(db, third).remainder is None
    assert [ledger.paid_amount(d) for d in (debt, second, third)] == [500, 200, 500]


@pytest.mark.parametrize("amount", [0, -5, 1201])
def test_partial_settle_rejects_bad_amounts(db: Session, amount: int) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    with pytest.raises(InvalidInputError):
        ledger.settle(db, debt, amount)
    assert debt.is_open
    assert db.scalar(select(func.count(Debt.id))) == 1


def test_settle_twice_refused(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    ledger.settle(db, debt)
    with pytest.raises(InvalidInputError, match="already"):
        ledger.settle(db, debt)


def test_items_and_favors_settle_in_full_only(db: Session) -> None:
    pato = make(db, "Pato")
    item = thing(db, pato, DebtType.ITEM, "charger")
    with pytest.raises(InvalidInputError, match="in full"):
        ledger.settle(db, item, 100)
    assert ledger.settle(db, item).remainder is None
    assert not item.is_open
    assert ledger.paid_amount(item) is None


# --- Undo a settlement ---


def test_unsettle_full(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    ledger.settle(db, debt)
    ledger.unsettle(db, debt)
    assert debt.is_open


def test_unsettle_partial_removes_the_remainder(db: Session) -> None:
    pato = make(db, "Pato")
    debt = money(db, pato, 1200)
    ledger.settle(db, debt, 500)
    ledger.unsettle(db, debt)
    assert debt.is_open
    assert debt.remainder is None
    assert [d.id for d in pato.debts] == [debt.id]
    assert db.scalar(select(func.count(Debt.id))) == 1
    assert balance_map(db) == {("Pato", "EUR"): 1200}


def test_unsettle_refused_when_the_rest_was_settled_since(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    rest = ledger.settle(db, debt, 500).remainder
    assert rest is not None
    ledger.settle(db, rest)
    with pytest.raises(InvalidInputError, match="D-0002"):
        ledger.unsettle(db, debt)
    ledger.unsettle(db, rest)  # newest first works
    ledger.unsettle(db, debt)
    assert debt.is_open


def test_unsettle_open_debt_refused(db: Session) -> None:
    debt = money(db, make(db, "Pato"), 1200)
    with pytest.raises(InvalidInputError):
        ledger.unsettle(db, debt)


# --- Lists ---


def test_list_debts_filters(db: Session) -> None:
    pato, terka = make(db, "Pato"), make(db, "Terka")
    pizza = money(db, pato, 1200)
    charger = thing(db, pato, DebtType.ITEM, "charger")
    favor = thing(db, terka, DebtType.FAVOR, "moving", I_OWE)
    beer = money(db, terka, 400, I_OWE)
    ledger.settle(db, beer)

    assert ledger.list_debts(db) == [pizza, charger, favor]
    assert ledger.list_debts(db, person_id=pato.id) == [pizza, charger]
    assert ledger.list_debts(db, is_open=False) == [beer]
    assert set(ledger.list_debts(db, is_open=None)) == {pizza, charger, favor, beer}
    assert ledger.list_debts(db, debt_type=DebtType.ITEM) == [charger]
    assert ledger.list_debts(db, direction=I_OWE) == [favor]
    assert ledger.list_debts(db, is_open=None, direction=I_OWE, debt_type=DebtType.MONEY) == [beer]


# --- Balances ---


def test_balances_net_per_person_and_currency(db: Session) -> None:
    pato, terka, jano = make(db, "Pato"), make(db, "Terka"), make(db, "Jano")
    money(db, pato, 1200)
    money(db, pato, 1200)
    money(db, pato, 400, I_OWE)  # nets against what Pato owes me
    money(db, pato, 25000, currency="CZK")  # never mixed with EUR
    money(db, terka, 1000, I_OWE)
    money(db, jano, 500)
    money(db, jano, 500, I_OWE)  # nets to zero: left out
    thing(db, pato, DebtType.ITEM, "charger")  # items never count
    thing(db, terka, DebtType.FAVOR, "moving", I_OWE)

    assert balance_map(db) == {
        ("Pato", "EUR"): 2000,
        ("Pato", "CZK"): 25000,
        ("Terka", "EUR"): -1000,
    }
    assert balance_map(db, pato.id) == {("Pato", "EUR"): 2000, ("Pato", "CZK"): 25000}
    assert balance_map(db, jano.id) == {}


def test_balances_ignore_settled_and_follow_partials(db: Session) -> None:
    pato = make(db, "Pato")
    pizza = money(db, pato, 1200)
    beer = money(db, pato, 300)
    ledger.settle(db, beer)
    ledger.settle(db, pizza, 500)
    assert balance_map(db) == {("Pato", "EUR"): 700}


def test_balances_largest_first(db: Session) -> None:
    small, big, owe = make(db, "Small"), make(db, "Big"), make(db, "Owe")
    money(db, small, 100)
    money(db, big, 9000)
    money(db, owe, 5000, I_OWE)
    assert [b.person.display_name for b in ledger.balances(db)] == ["Big", "Owe", "Small"]


def test_totals_per_currency(db: Session) -> None:
    pato, terka, jano = make(db, "Pato"), make(db, "Terka"), make(db, "Jano")
    money(db, pato, 2000)
    money(db, jano, 500)
    money(db, terka, 1000, I_OWE)
    money(db, terka, 25000, I_OWE, currency="CZK")
    found = {t.currency: t for t in ledger.totals(ledger.balances(db))}
    assert list(found) == ["CZK", "EUR"]
    assert (found["EUR"].owed_to_me, found["EUR"].i_owe, found["EUR"].net) == (2500, 1000, 1500)
    assert (found["CZK"].owed_to_me, found["CZK"].i_owe, found["CZK"].net) == (0, 25000, -25000)


def test_totals_empty(db: Session) -> None:
    assert ledger.totals([]) == []


# --- Item reminders ---


def test_item_reminder_uses_default_or_override_days(db: Session) -> None:
    pato = make(db, "Pato")
    charger = thing(db, pato, DebtType.ITEM, "charger")
    book = thing(db, pato, DebtType.ITEM, "book", reminder_days=60)
    # 23:30 UTC on 2 Oct is already 3 Oct in Prague.
    charger.created_at = book.created_at = datetime(2026, 10, 2, 23, 30, tzinfo=UTC)
    assert ledger.item_reminder_on(charger, 21, PRAGUE) == date(2026, 10, 24)
    assert ledger.item_reminder_on(book, 21, PRAGUE) == date(2026, 12, 2)


def test_item_reminder_only_for_open_items_i_lent(db: Session) -> None:
    pato = make(db, "Pato")
    borrowed = thing(db, pato, DebtType.ITEM, "drill", I_OWE)
    favor = thing(db, pato, DebtType.FAVOR, "moving")
    pizza = money(db, pato, 1200)
    returned = thing(db, pato, DebtType.ITEM, "charger")
    ledger.settle(db, returned)
    for debt in (borrowed, favor, pizza, returned):
        assert ledger.item_reminder_on(debt, 21, PRAGUE) is None
