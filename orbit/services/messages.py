"""Ready-to-send texts: asking someone to pay me back, or to return a lent item.

Plain templates (no Claude), in the person's language: Slovak for name-day country SK, Czech for
CZ, English otherwise. I copy or share them and edit before sending; nothing is sent from here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from orbit.domain.enums import DebtDirection, DebtType
from orbit.domain.ledger import Debt
from orbit.domain.money import format_amount
from orbit.domain.people import Person
from orbit.integrations.claude import Language
from orbit.services.errors import InvalidInputError

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_MONEY = {
    "sk": {
        "intro": "Ahoj {name}, posielam prehľad, čo mi ešte dlžíš:",
        "total": "Spolu: {amount}",
        "iban": "IBAN: {iban}",
        "holder": "Príjemca: {holder}",
        "codes": "Do poznámky pre príjemcu prosím napíš: {codes}",
        "thanks": "Vďaka!",
    },
    "cs": {
        "intro": "Ahoj {name}, posílám přehled, co mi ještě dlužíš:",
        "total": "Celkem: {amount}",
        "iban": "IBAN: {iban}",
        "holder": "Příjemce: {holder}",
        "codes": "Do zprávy pro příjemce prosím napiš: {codes}",
        "thanks": "Díky!",
    },
    "en": {
        "intro": "Hi {name}, here's what's still open between us:",
        "total": "Total: {amount}",
        "iban": "IBAN: {iban}",
        "holder": "Account holder: {holder}",
        "codes": "Please put this in the payment note: {codes}",
        "thanks": "Thanks!",
    },
}

_ITEM = {
    "sk": "Ahoj {name}, len pripomínam, požičal som ti: {item} ({since}). "
    "Vieš mi to vrátiť, keď sa uvidíme? Vďaka!",
    "cs": "Ahoj {name}, jen připomínám, půjčil jsem ti: {item} ({since}). "
    "Můžeš mi to vrátit, až se uvidíme? Díky!",
    "en": "Hi {name}, just a reminder: I lent you my {item} ({since}). "
    "Could you give it back next time we meet? Thanks!",
}


def language(person: Person) -> Language:
    """The language to write to someone in, from their name-day calendar."""
    if person.nameday_country == "SK":
        return "sk"
    if person.nameday_country == "CZ":
        return "cs"
    return "en"


def address_name(person: Person) -> str:
    """How to greet them: the first name, else the first word of the display name."""
    if person.first_name and person.first_name.strip():
        return person.first_name.strip().split()[0]
    words = person.display_name.split()
    return words[0] if words else person.display_name


def format_money(amount_minor: int, currency: str, lang: Language) -> str:
    """Slovak and Czech write "1 250,00 €"; English "1,250.00 €"."""
    text = format_amount(amount_minor, currency)
    if lang == "en":
        return text
    return text.replace(",", "\N{NO-BREAK SPACE}").replace(".", ",")


def format_day(day: date, lang: Language) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}" if lang == "en" else f"{day.day}. {day.month}."


def group_iban(iban: str) -> str:
    compact = "".join(iban.split()).upper()
    return " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))


@dataclass(frozen=True)
class PaymentRequest:
    currency: str
    total_minor: int
    text: str


def payment_requests(
    person: Person,
    debts: Sequence[Debt],
    *,
    iban: str | None = None,
    holder: str | None = None,
) -> list[PaymentRequest]:
    """One text per currency listing the open money debts `person` owes me, with their reference
    codes (so the payment matches exactly) and my IBAN when it's configured."""
    owed = [
        d
        for d in debts
        if d.person_id == person.id
        and d.is_open
        and d.type is DebtType.MONEY
        and d.direction is DebtDirection.THEY_OWE_ME
    ]
    lang = language(person)
    words = _MONEY[lang]
    requests: list[PaymentRequest] = []
    for currency in sorted({d.currency for d in owed if d.currency}):
        mine = sorted(
            (d for d in owed if d.currency == currency), key=lambda d: (d.created_at, d.id)
        )
        total = sum(d.amount_minor or 0 for d in mine)
        lines = [words["intro"].format(name=address_name(person))]
        for d in mine:
            what = d.description or ""
            amount = format_money(d.amount_minor or 0, currency, lang)
            code = f" ({d.reference_code})" if d.reference_code else ""
            lines.append(f"• {what}: {amount}{code}" if what else f"• {amount}{code}")
        if len(mine) > 1:
            lines.append(words["total"].format(amount=format_money(total, currency, lang)))
        if iban:
            lines.append(words["iban"].format(iban=group_iban(iban)))
            if holder:
                lines.append(words["holder"].format(holder=holder))
        codes = [d.reference_code for d in mine if d.reference_code]
        if codes:
            lines.append(words["codes"].format(codes=" ".join(codes)))
        lines.append(words["thanks"])
        requests.append(PaymentRequest(currency, total, "\n".join(lines)))
    return requests


def ask_back(person: Person, debt: Debt, lent_on: date) -> str:
    """A friendly reminder to return an item I lent them."""
    if debt.type is not DebtType.ITEM or debt.direction is not DebtDirection.THEY_OWE_ME:
        raise InvalidInputError("Only items you lent can be asked back.")
    lang = language(person)
    return _ITEM[lang].format(
        name=address_name(person), item=debt.description, since=format_day(lent_on, lang)
    )
