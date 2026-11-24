from __future__ import annotations

from datetime import UTC, date, datetime, time
from itertools import count
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from orbit.domain.bank import BankImport, Transaction
from orbit.domain.enums import (
    Bank,
    Classification,
    DebtDirection,
    HandleType,
    ProposalKind,
    ProposalStatus,
    TransactionDirection,
    TransactionKind,
)
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt
from orbit.domain.people import Person
from orbit.services import action_tokens, inbox, ledger, notifications, payments, people
from orbit.services.errors import InvalidInputError
from orbit.services.payments import DebtSettle, PayerSource, PaymentMatchPayload
from tests.test_ledger import make, money
from tests.test_notifications import FakeSender

PRAGUE = ZoneInfo("Europe/Prague")
QUIET = notifications.QuietHours(time(22, 0), time(8, 0), PRAGUE)
NOON = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
SECRET = "test-secret-key-" + "y" * 32
BASE = "https://orbit.example.com"
PATO_IBAN = "SK3211000000001000002222"
_hashes = count()


def incoming(
    db: Session,
    amount_minor: int,
    *,
    iban: str | None = None,
    name: str | None = None,
    message: str | None = None,
    currency: str = "EUR",
    direction: TransactionDirection = TransactionDirection.IN,
) -> Transaction:
    tx = Transaction(
        bank_import=BankImport(bank=Bank.TATRA),
        bank=Bank.TATRA,
        booking_date=date(2026, 9, 20),
        amount_minor=amount_minor,
        currency=currency,
        direction=direction,
        kind=TransactionKind.TRANSFER,
        counterparty=name,
        counterparty_norm=name.lower() if name else None,
        counterparty_iban=iban,
        iban_masked=f"{iban[:2]}** **** {iban[-4:]}" if iban else None,
        message=message,
        classification=Classification.INCOMING_FROM_PERSON,
        hash=f"tx-{next(_hashes)}",
        raw_json={},
    )
    db.add(tx)
    db.flush()
    return tx


def person(db: Session, display: str, first: str | None = None, last: str | None = None) -> Person:
    return people.create_person(
        db, people.PersonCreate(display_name=display, first_name=first, last_name=last)
    )


@pytest.fixture
def pato(db: Session) -> Person:
    found = person(db, "Patrik Veselý", "Patrik", "Veselý")
    people.add_alias(db, found, "Paťo")
    people.add_handle(db, found, HandleType.IBAN, PATO_IBAN)
    return found


def payload_of(proposal: Proposal) -> PaymentMatchPayload:
    return PaymentMatchPayload.model_validate(proposal.payload_json)


def settle_ids(settle: list[DebtSettle]) -> list[tuple[int, int | None]]:
    return [(s.debt_id, s.amount_minor) for s in settle]


def propose(db: Session, tx: Transaction) -> Proposal:
    proposal = payments.propose_match(db, tx)
    assert proposal is not None
    return proposal


def confirm(db: Session, proposal: Proposal, **kwargs: object) -> inbox.ConfirmResult:
    return inbox.confirm(
        db, proposal, today=date(2026, 9, 24), default_currency="EUR", now=NOON, **kwargs
    )  # type: ignore[arg-type]


# --- Who paid ---


def test_payer_by_iban_handle(db: Session, pato: Person) -> None:
    tx = incoming(db, 1200, iban=PATO_IBAN)
    assert payments.identify_payer(db, tx) == payments.Payer(pato, PayerSource.IBAN)


def test_payer_by_bank_name_handle(db: Session, pato: Person) -> None:
    people.add_handle(db, pato, HandleType.BANK_NAME, "Transfer from P. VESELY")
    tx = incoming(db, 1200, name="TRANSFER FROM P. VESELÝ")  # case and diacritics don't matter
    assert payments.identify_payer(db, tx) == payments.Payer(pato, PayerSource.BANK_NAME)


@pytest.mark.parametrize(
    "bank_name",
    [
        "VESELY PATRIK",  # surname first, no diacritics
        "Patrik Veselý",
        "P. VESELY",  # initial + surname
        "Transfer from Patrik Vesely",
        "PATO VESELY",  # an alias as the first name
        "Patrick Vesely",  # close spelling
    ],
)
def test_payer_by_fuzzy_name(db: Session, pato: Person, bank_name: str) -> None:
    person(db, "Terka Nováková", "Tereza", "Nováková")
    tx = incoming(db, 1200, name=bank_name)
    assert payments.identify_payer(db, tx) == payments.Payer(pato, PayerSource.NAME)


@pytest.mark.parametrize("bank_name", ["VESELY", "M. VESELY", "PATRIK NOVAK", "Lidl"])
def test_no_payer_from_a_partial_or_different_name(
    db: Session, pato: Person, bank_name: str
) -> None:
    assert payments.identify_payer(db, incoming(db, 1200, name=bank_name)) is None


def test_ambiguous_names_are_not_guessed(db: Session, pato: Person) -> None:
    person(db, "Peter Veselý", "Peter", "Veselý")
    assert payments.identify_payer(db, incoming(db, 1200, name="P. VESELY")) is None
    # The full first name still tells them apart.
    found = payments.identify_payer(db, incoming(db, 1200, name="PETER VESELY"))
    assert found is not None and found.person.display_name == "Peter Veselý"


def test_archived_people_are_not_guessed_but_their_handles_count(db: Session, pato: Person) -> None:
    pato.archived = True
    assert payments.identify_payer(db, incoming(db, 1200, name="PATRIK VESELY")) is None
    found = payments.identify_payer(db, incoming(db, 1200, iban=PATO_IBAN))
    assert found == payments.Payer(pato, PayerSource.IBAN)


def test_payer_by_reference_code(db: Session) -> None:
    terka = make(db, "Terka")
    debt = money(db, terka, 1500, description="kino")
    tx = incoming(
        db, 1500, iban="SK0000000000000000009999", message=f"za kino {debt.reference_code}"
    )
    assert payments.identify_payer(db, tx) == payments.Payer(terka, PayerSource.REFERENCE)


def test_reference_codes_are_read_loosely() -> None:
    tx = Transaction(message="D-0142, d0007 a D 0142", counterparty=None, bank_reference="D-10000")
    assert payments.reference_codes(tx) == ["D-0142", "D-0007", "D-10000"]
    assert payments.reference_codes(Transaction(message="VS12345 d12 dlh 2026")) == []


# --- Options ---


def test_exact_amount(db: Session, pato: Person) -> None:
    money(db, pato, 800, description="kebab")
    pizza = money(db, pato, 1200, description="pizza")
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN, message="pizza"))
    payload = payload_of(proposal)
    assert (payload.person_id, payload.payer_source) == (pato.id, PayerSource.IBAN)
    assert payload.options[0].kind == "exact"
    assert settle_ids(payload.settle) == [(pizza.id, None)]
    assert proposal.summary == "Received 12.00 € from Patrik Veselý — pizza 12.00 €"
    assert proposal.status is ProposalStatus.PENDING
    assert pizza.is_open  # nothing is settled until I confirm


def test_several_debts_adding_up(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200, description="pizza")
    money(db, pato, 500, description="coffee")
    kino = money(db, pato, 1200, description="kino")
    payload = payload_of(propose(db, incoming(db, 2400, iban=PATO_IBAN)))
    assert payload.options[0].kind == "subset"
    assert settle_ids(payload.settle) == [(pizza.id, None), (kino.id, None)]
    assert payments.describe(db, payload) == ["pizza 12.00 €", "kino 12.00 €"]


def test_up_to_four_debts_add_up(db: Session, pato: Person) -> None:
    debts = [money(db, pato, amount) for amount in (100, 200, 300, 400, 5000)]
    payload = payload_of(propose(db, incoming(db, 1000, iban=PATO_IBAN)))
    assert settle_ids(payload.settle) == [(d.id, None) for d in debts[:4]]
    # Five debts are too many to add up: the fallback pays them in order.
    payload = payload_of(propose(db, incoming(db, 6000, iban=PATO_IBAN)))
    assert [o.kind for o in payload.options] == ["all"]


def test_ranked_options(db: Session, pato: Person) -> None:
    a = money(db, pato, 1000, description="a")
    b = money(db, pato, 400, description="b")
    c = money(db, pato, 600, description="c")
    d = money(db, pato, 1000, description="d")
    tx = incoming(db, 1000, iban=PATO_IBAN, message=f"{d.reference_code}")
    options = payload_of(propose(db, tx)).options
    assert [(o.kind, settle_ids(o.settle)) for o in options] == [
        ("reference", [(d.id, None)]),
        ("exact", [(a.id, None)]),
        ("subset", [(b.id, None), (c.id, None)]),
    ]


def test_partial_payment_pays_the_oldest_first(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200, description="pizza")
    kino = money(db, pato, 1000, description="kino")
    proposal = propose(db, incoming(db, 1500, iban=PATO_IBAN))
    payload = payload_of(proposal)
    assert [o.kind for o in payload.options] == ["partial"]
    assert settle_ids(payload.settle) == [(pizza.id, None), (kino.id, 300)]
    assert payments.describe(db, payload) == ["pizza 12.00 €", "3.00 € of kino 10.00 €"]

    result = confirm(db, proposal)
    assert not pizza.is_open and not kino.is_open
    rest = kino.remainder
    assert rest is not None and rest.is_open and rest.amount_minor == 700
    assert result.message == (
        "Settled 2 debts from Patrik Veselý: pizza 12.00 €; "
        f"3.00 € of kino 10.00 € (7.00 € still open as {rest.reference_code})."
    )
    assert [b.amount_minor for b in ledger.balances(db, pato.id)] == [700]


def test_paying_more_than_owed(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200)
    payload = payload_of(propose(db, incoming(db, 1500, iban=PATO_IBAN)))
    assert [(o.kind, settle_ids(o.settle)) for o in payload.options] == [
        ("overpaid", [(pizza.id, None)])
    ]
    # Far more than owed (the rent): not offered as settling the pizza.
    assert payload_of(propose(db, incoming(db, 50000, iban=PATO_IBAN))).options == []


def test_reference_code_of_a_partly_paid_debt_points_to_the_rest(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 2400, description="pizza")
    rest = ledger.settle(db, pizza, 1000).remainder
    assert rest is not None
    tx = incoming(db, 1400, iban=PATO_IBAN, message=f"pizza {pizza.reference_code}")
    payload = payload_of(propose(db, tx))
    assert payload.options[0].kind == "reference"
    assert settle_ids(payload.settle) == [(rest.id, None)]


def test_only_money_they_owe_me_in_that_currency(db: Session, pato: Person) -> None:
    money(db, pato, 1200, direction=DebtDirection.I_OWE_THEM)
    money(db, pato, 1200, currency="CZK")
    ledger.settle(db, money(db, pato, 1200), None)
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    assert payload_of(proposal).options == []


def test_currency_mismatch(db: Session, pato: Person) -> None:
    koruny = money(db, pato, 30000, currency="CZK", description="koncert")
    tx = incoming(db, 1200, iban=PATO_IBAN)
    proposal = propose(db, tx)
    payload = payload_of(proposal)
    assert (payload.person_id, payload.options, payload.settle) == (pato.id, [], [])
    assert proposal.summary == "Received 12.00 € from Patrik Veselý — No open EUR debts match."
    # I can still settle the CZK debt with it, in full only.
    with pytest.raises(InvalidInputError, match="is in CZK: settle it in full"):
        confirm(
            db,
            proposal,
            payload={
                **proposal.payload_json,
                "settle": [{"debt_id": koruny.id, "amount_minor": 100}],
            },
        )
    confirm(db, proposal, payload={**proposal.payload_json, "settle": [{"debt_id": koruny.id}]})
    assert not koruny.is_open and koruny.settled_by_transaction_id == tx.id


def test_known_payer_without_money_owed_gets_no_proposal(db: Session, pato: Person) -> None:
    tx = incoming(db, 100000, iban=PATO_IBAN, message="najom")
    assert payments.propose_match(db, tx) is None
    forced = payments.propose_match(db, tx, force=True)  # from the Import page
    assert forced is not None and payload_of(forced).options == []
    assert payments.propose_match(db, tx) is forced  # an open one is reused


def test_outgoing_payments_settle_nothing(db: Session, pato: Person) -> None:
    tx = incoming(db, 1200, iban=PATO_IBAN, direction=TransactionDirection.OUT)
    with pytest.raises(InvalidInputError, match="Only incoming"):
        payments.propose_match(db, tx)


# --- Unknown payer ---


def test_unknown_payer(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200, description="pizza")
    tx = incoming(db, 1200, iban="SK5711000000001000005555", name="J. NOVAK", message="pizza")
    proposal = propose(db, tx)
    payload = payload_of(proposal)
    assert (payload.person_id, payload.remember_payer, payload.options) == (None, True, [])
    assert proposal.summary == "Who sent 12.00 €? “J. NOVAK”"
    with pytest.raises(InvalidInputError, match="Pick who sent this payment"):
        confirm(db, proposal)

    # I pick Pato in the Inbox: his options appear, and confirming remembers the payer.
    picked = payments.rematch(db, payload, pato.id)
    assert picked.payer_source is PayerSource.MANUAL
    assert settle_ids(picked.settle) == [(pizza.id, None)]
    result = confirm(db, proposal, payload=picked.model_dump(mode="json"))
    assert not pizza.is_open
    assert result.person_id == pato.id
    assert {(h.type, h.value) for h in pato.handles} >= {
        (HandleType.IBAN, "SK5711000000001000005555"),
        (HandleType.BANK_NAME, "J. NOVAK"),
    }
    assert (
        "Patrik Veselý will be recognized by the IBAN and “J. NOVAK” from now on." in result.message
    )
    # Next time the payment is recognized.
    again = incoming(db, 500, iban="SK5711000000001000005555", name="J. NOVAK")
    assert payments.identify_payer(db, again) == payments.Payer(pato, PayerSource.IBAN)


def test_linking_only(db: Session, pato: Person) -> None:
    tx = incoming(db, 100000, iban="SK2411110000001000003333", message="depozit")
    proposal = propose(db, tx)
    picked = payments.rematch(db, payload_of(proposal), pato.id)
    assert picked.settle == [] and picked.remember_payer
    result = confirm(db, proposal, payload=picked.model_dump(mode="json"))
    assert result.message == "Patrik Veselý will be recognized by the IBAN from now on."
    # Without anything to remember or settle, there's nothing to confirm.
    other = propose(db, incoming(db, 500, iban="SK0000000000000000000001"))
    bare = payments.rematch(db, payload_of(other), pato.id).model_copy(
        update={"remember_payer": False}
    )
    with pytest.raises(InvalidInputError, match="Nothing to settle"):
        confirm(db, other, payload=bare.model_dump(mode="json"))


def test_linking_resolves_other_waiting_payments_from_them(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200, description="pizza")
    iban = "SK8111000000001000004444"
    first = propose(db, incoming(db, 500, iban=iban))
    second = propose(db, incoming(db, 1200, iban=iban))
    unrelated = propose(db, incoming(db, 700, iban="SK0000000000000000000002"))
    # The 5 € was something else: I only say who sent it.
    picked = payments.rematch(db, payload_of(first), pato.id).model_copy(update={"settle": []})
    result = confirm(db, first, payload=picked.model_dump(mode="json"))
    assert "1 more payment in the Inbox matched too." in result.message
    assert pizza.is_open  # still waits for my tap
    refreshed = payload_of(second)
    assert (refreshed.person_id, refreshed.payer_source) == (pato.id, PayerSource.IBAN)
    assert settle_ids(refreshed.settle) == [(pizza.id, None)]
    assert second.summary == "Received 12.00 € from Patrik Veselý — pizza 12.00 €"
    assert payload_of(unrelated).person_id is None


def test_a_handle_someone_else_has_is_not_taken(db: Session, pato: Person) -> None:
    terka = make(db, "Terka")
    money(db, terka, 1200)
    tx = incoming(db, 1200, iban=PATO_IBAN)  # Pato paid for Terka
    proposal = payments.propose_match(db, tx, force=True)  # Pato owes nothing: by hand
    assert proposal is not None
    picked = payments.rematch(db, payload_of(proposal), terka.id)
    assert picked.remember_payer is False  # the IBAN is Pato's
    confirm(db, proposal, payload=picked.model_dump(mode="json"))
    assert terka.handles == []


# --- Confirming ---


def test_confirm_settles_and_links_the_payment(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200, description="pizza")
    kino = money(db, pato, 1200, description="kino")
    tx = incoming(db, 2400, iban=PATO_IBAN)
    proposal = propose(db, tx)
    result = confirm(db, proposal)
    assert proposal.status is ProposalStatus.CONFIRMED
    assert result.message == "Settled 2 debts from Patrik Veselý: pizza 12.00 €; kino 12.00 €."
    for debt in (pizza, kino):
        assert (debt.settled_at, debt.settled_by_transaction_id) == (NOON, tx.id)
    ledger.unsettle(db, pizza)  # a mis-tap can be undone
    assert pizza.settled_by_transaction_id is None


def test_confirm_fails_cleanly_when_a_debt_changed_meanwhile(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200, description="pizza")
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    ledger.settle(db, pizza)
    with pytest.raises(InvalidInputError, match="already settled"):
        confirm(db, proposal)
    assert proposal.status is ProposalStatus.PENDING


@pytest.mark.parametrize(
    ("settle", "error"),
    [
        ([{"debt_id": 999}], "no longer exists"),
        ([{"debt_id": "{other}"}], "isn't Patrik Veselý's debt"),
        ([{"debt_id": "{owe}"}], "isn't money Patrik Veselý owes you"),
        ([{"debt_id": "{pizza}"}, {"debt_id": "{pizza}"}], "only once"),
        ([{"debt_id": "{pizza}", "amount_minor": 1300}], "more than"),
    ],
)
def test_confirm_validates_the_choice(
    db: Session, pato: Person, settle: list[dict[str, object]], error: str
) -> None:
    ids = {
        "{pizza}": money(db, pato, 1200).id,
        "{owe}": money(db, pato, 1200, direction=DebtDirection.I_OWE_THEM).id,
        "{other}": money(db, make(db, "Terka"), 1200).id,
    }
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    chosen = [{**s, "debt_id": ids.get(str(s["debt_id"]), s["debt_id"])} for s in settle]
    with pytest.raises(InvalidInputError, match=error):
        confirm(db, proposal, payload={**proposal.payload_json, "settle": chosen})


def test_part_amount_equal_to_the_debt_settles_it_in_full(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200)
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    confirm(
        db,
        proposal,
        payload={**proposal.payload_json, "settle": [{"debt_id": pizza.id, "amount_minor": 1200}]},
    )
    assert not pizza.is_open and pizza.remainder is None


def test_rejecting_settles_nothing(db: Session, pato: Person) -> None:
    pizza = money(db, pato, 1200)
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    inbox.reject(db, proposal)
    assert pizza.is_open


# --- Pushes ---


def test_push_with_settle_choose_and_not_a_debt(db: Session, pato: Person) -> None:
    money(db, pato, 1200, description="pizza")
    money(db, pato, 1200, description="kino")
    proposal = propose(db, incoming(db, 2400, iban=PATO_IBAN, message="pizza + kino"))
    push = payments.payment_push(db, proposal, base_url=BASE, secret=SECRET, now=NOON)
    assert push.title == "💶 Received 24.00 € from Patrik Veselý"
    assert push.body == "“pizza + kino” · Matches: pizza 12.00 € + kino 12.00 €"
    assert push.click == f"{BASE}/inbox/{proposal.id}"
    settle, choose, not_a_debt = push.actions
    assert (settle.label, settle.kind, settle.clear) == ("Settle", "http", True)
    assert (choose.label, choose.kind, choose.url) == ("Choose…", "view", push.click)
    assert (not_a_debt.label, not_a_debt.kind) == ("Not a debt", "http")
    for action, expected in ((settle, "confirm"), (not_a_debt, "reject")):
        token = action_tokens.read_token(SECRET, action.url.removeprefix(f"{BASE}/a/"), now=NOON)
        assert (token.proposal_id, token.action) == (proposal.id, expected)
    assert "Bearer" not in str(push.to_dict()) and PATO_IBAN not in str(push.to_dict())


def test_push_shows_the_bank_name_when_the_match_is_a_guess(db: Session, pato: Person) -> None:
    money(db, pato, 1200, description="pizza")
    proposal = propose(db, incoming(db, 1200, name="VESELY PATRIK"))
    push = payments.payment_push(db, proposal, base_url=BASE, secret=SECRET, now=NOON)
    assert push.body == "from “VESELY PATRIK” · Matches: pizza 12.00 €"


def test_push_without_a_match_has_no_settle_button(db: Session, pato: Person) -> None:
    money(db, pato, 30000, currency="CZK")
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    push = payments.payment_push(db, proposal, base_url=BASE, secret=SECRET, now=NOON)
    assert push.body == "No open EUR debts match."
    assert [a.label for a in push.actions] == ["Choose…", "Not a debt"]


def test_notify_payments(db: Session, pato: Person) -> None:
    money(db, pato, 1200)
    known = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    lone = propose(db, incoming(db, 500, iban="SK0000000000000000000003", name="J. NOVAK"))
    sender = FakeSender()

    def notify(proposals: list[Proposal], import_id: int = 1) -> list[str]:
        sent = payments.notify_payments(
            db,
            sender,
            proposals,
            base_url=BASE,
            secret=SECRET,
            quiet=QUIET,
            import_id=import_id,
            now=NOON,
        )
        return [n.title for n in sent]

    assert notify([known, lone]) == [
        "💶 Received 12.00 € from Patrik Veselý",
        "💶 Who sent 5.00 €?",
    ]
    assert [a.label for a in sender.sent[1].actions] == ["Who is it?", "Not a debt"]
    assert sender.sent[1].body == "“J. NOVAK”"
    assert notify([known, lone]) == []  # never twice

    many = [
        propose(db, incoming(db, 100 * i, iban=f"SK00000000000000000001{i:02d}"))
        for i in range(1, 6)
    ]
    assert notify(many, import_id=2) == ["💶 5 payments from unknown senders"]
    assert sender.sent[-1].body == (
        "SK** **** 0101 1.00 €, SK** **** 0102 2.00 €, SK** **** 0103 3.00 € and 2 more."
        " Who are they?"
    )
    assert [a.label for a in sender.sent[-1].actions] == ["Open Inbox"]


def test_push_is_queued_during_quiet_hours(db: Session, pato: Person) -> None:
    money(db, pato, 1200)
    proposal = propose(db, incoming(db, 1200, iban=PATO_IBAN))
    sender = FakeSender()
    late = datetime(2026, 9, 24, 21, 30, tzinfo=UTC)
    (record,) = payments.notify_payments(
        db, sender, [proposal], base_url=BASE, secret=SECRET, quiet=QUIET, import_id=1, now=late
    )
    assert sender.sent == [] and record.not_before > late


def test_proposal_kind_and_source(db: Session, pato: Person) -> None:
    money(db, pato, 1200)
    tx = incoming(db, 1200, iban=PATO_IBAN)
    proposal = propose(db, tx)
    assert (proposal.kind, proposal.source_type, proposal.source_id) == (
        ProposalKind.PAYMENT_MATCH,
        "bank_transaction",
        str(tx.id),
    )
    assert proposal.confidence is None  # rules, not Claude
    assert db.get(Debt, payload_of(proposal).settle[0].debt_id) is not None
