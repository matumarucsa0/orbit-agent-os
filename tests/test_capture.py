from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    HandleType,
    InteractionKind,
    PromiseDirection,
    ProposalKind,
    ProposalStatus,
)
from orbit.domain.ledger import Debt, Promise
from orbit.domain.people import Interaction, Note, Person
from orbit.integrations.claude import (
    CaptureExtraction,
    CaptureRequest,
    ClaudeError,
    ExtractedDebt,
    ExtractedNote,
    ExtractedPromise,
    UnknownPerson,
)
from orbit.services import capture, people
from orbit.services.capture import (
    CapturePayload,
    DebtDraft,
    InteractionDraft,
    NewPersonDraft,
    NewPersonPayload,
    NoteDraft,
    PromiseDraft,
    apply_capture,
    match_people,
    payload_from_extraction,
)
from orbit.services.errors import InvalidInputError

TODAY = date(2026, 9, 23)
SLOVAK = "zaplatil som Paťovi pizzu 12€, sľúbil som mu poslať rozvrh"


def extraction(**items: Any) -> CaptureExtraction:
    values: dict[str, Any] = {
        "notes": [],
        "interactions": [],
        "debts": [],
        "promises": [],
        "unknown_people": [],
        "confidence": 0.9,
    }
    values.update(items)
    return CaptureExtraction(**values)


class FakeExtractor:
    """Stands in for Claude: returns a fixed answer (or raises) and records what it was asked."""

    def __init__(self, result: CaptureExtraction | Exception) -> None:
        self.result = result
        self.requests: list[CaptureRequest] = []

    def extract_capture(self, request: CaptureRequest) -> CaptureExtraction:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def person(db: Session, name: str, *aliases: str, **fields: Any) -> Person:
    created = people.create_person(db, people.PersonCreate(display_name=name, **fields))
    for alias in aliases:
        people.add_alias(db, created, alias)
    return created


# --- Fuzzy pre-match ---


@pytest.fixture
def crowd(db: Session) -> dict[str, Person]:
    return {
        "pato": person(db, "Patrik Veselý", "Paťo"),
        "terka": person(db, "Terka Nová"),
        "peter": person(db, "Peter Malý"),
        "marek": person(db, "Marek Horváth"),
        "zuzana": person(db, "Zuzana Kráľová"),
        "old": person(db, "Paťo Starý", archived=True),
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SLOVAK, "pato"),
        ("kafe s Paťom", "pato"),
        ("pato mi dlzi", "pato"),
        ("požičal som Terke nabíjačku", "terka"),
        ("Terku som videl", "terka"),
        ("stretol som Petra", "peter"),
        ("dal som Petrovi knihu", "peter"),
        ("s Markom na pive", "marek"),
        ("Zuzke som sľúbil", None),  # a diminutive that isn't an alias: left to Claude
        ("volal som so Zuzanou", "zuzana"),
        ("Tobiáš sa ozval", None),
    ],
)
def test_prematch_tolerates_inflections_and_diacritics(
    db: Session, crowd: dict[str, Person], text: str, expected: str | None
) -> None:
    found = match_people(db, text)
    if expected is None:
        assert found == []
    else:
        assert found[0] == crowd[expected]


def test_prematch_skips_archived_people(db: Session, crowd: dict[str, Person]) -> None:
    assert crowd["old"] not in match_people(db, "Paťo Starý")


def test_prematch_finds_handles_and_close_spellings(db: Session, crowd: dict[str, Person]) -> None:
    people.add_handle(db, crowd["marek"], HandleType.INSTAGRAM, "@mhorvath")
    assert match_people(db, "videl som @mhorvath")[0] == crowd["marek"]
    assert crowd["marek"] in match_people(db, "Horvat")  # missing the h


def test_prematch_caps_the_list(db: Session) -> None:
    for i in range(30):
        person(db, f"Jana {i}")
    assert len(match_people(db, "Jana", limit=25)) == 25


# --- Claude's answer -> payload ---


def test_payload_resolves_refs_and_amounts(db: Session, crowd: dict[str, Person]) -> None:
    pato = crowd["pato"]
    payload, standalone = payload_from_extraction(
        extraction(
            debts=[
                ExtractedDebt(
                    person_ref=str(pato.id),
                    direction=DebtDirection.THEY_OWE_ME,
                    type=DebtType.MONEY,
                    amount="12,50",
                    currency="eur",
                    description="pizza",
                ),
                ExtractedDebt(
                    person_ref=str(pato.id),
                    direction=DebtDirection.THEY_OWE_ME,
                    type=DebtType.MONEY,
                    amount=None,
                    currency=None,
                    description="coffee",
                ),
                ExtractedDebt(
                    person_ref="999",  # not a person Claude was shown
                    direction=DebtDirection.I_OWE_THEM,
                    type=DebtType.FAVOR,
                    amount="5",
                    currency="EUR",
                    description="moving",
                ),
            ],
        ),
        text="…",
        candidates=[pato],
        default_currency="EUR",
    )
    pizza, coffee, favor = payload.debts
    assert (pizza.person, pizza.amount_minor, pizza.currency) == (str(pato.id), 1250, "EUR")
    assert coffee.amount_minor is None and coffee.currency == "EUR" and coffee.amount_missing
    assert favor.person is None  # unassigned: I pick the person before confirming
    assert (favor.amount_minor, favor.currency) == (None, None)
    assert payload.candidates == [pato.id]
    assert standalone == []


def test_unknown_people_referenced_stay_in_the_capture_the_rest_stand_alone(db: Session) -> None:
    payload, standalone = payload_from_extraction(
        extraction(
            notes=[ExtractedNote(person_ref="new:1", text="začína nový job")],
            unknown_people=[
                UnknownPerson(name="Janko", context="Pato's brother"),
                UnknownPerson(name="Evka", context="new colleague"),
            ],
        ),
        text="…",
        candidates=[],
        default_currency="EUR",
    )
    assert [p.name for p in payload.new_people] == ["Evka"]
    assert payload.notes[0].person == "new:0"  # renumbered
    assert [(p.name, p.context) for p in standalone] == [("Janko", "Pato's brother")]


# --- capture() ---


def slovak_answer(pato_id: int) -> CaptureExtraction:
    return extraction(
        debts=[
            ExtractedDebt(
                person_ref=str(pato_id),
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.MONEY,
                amount="12",
                currency="EUR",
                description="pizza",
            )
        ],
        promises=[
            ExtractedPromise(
                person_ref=str(pato_id),
                direction=PromiseDirection.I_PROMISED,
                text="poslať rozvrh",
                due=None,
                goal_ref=None,
            )
        ],
    )


def test_slovak_capture_becomes_one_pending_proposal(db: Session, crowd: dict[str, Person]) -> None:
    pato = crowd["pato"]
    extractor = FakeExtractor(slovak_answer(pato.id))
    result = capture.capture(db, extractor, SLOVAK, today=TODAY, default_currency="EUR")

    (request,) = extractor.requests
    assert request.today == TODAY
    assert request.candidates[0].id == pato.id
    assert request.candidates[0].aliases == ("Paťo",)

    (proposal,) = result.proposals
    assert proposal.kind is ProposalKind.CAPTURE
    assert proposal.status is ProposalStatus.PENDING
    assert proposal.source_type == "capture"
    assert proposal.confidence == 0.9
    assert proposal.summary == (
        "Patrik Veselý owes you 12.00 € (pizza) · You promised Patrik Veselý: poslať rozvrh"
    )
    assert result.item_count == 2
    assert result.summary.startswith("2 items waiting in Inbox: Patrik Veselý owes you 12.00 €")
    # Proposed only: nothing touched the ledger or promises.
    assert db.scalars(select(Debt)).all() == []
    assert db.scalars(select(Promise)).all() == []


def test_standalone_unknown_person_becomes_a_new_person_proposal(db: Session) -> None:
    extractor = FakeExtractor(
        extraction(unknown_people=[UnknownPerson(name="Janko Kováč", context="from the gym")])
    )
    result = capture.capture(
        db, extractor, "Stretol som Janka Kováča", today=TODAY, default_currency="EUR"
    )
    (proposal,) = result.proposals
    assert proposal.kind is ProposalKind.NEW_PERSON
    payload = NewPersonPayload.model_validate(proposal.payload_json)
    assert (payload.name, payload.context) == ("Janko Kováč", "from the gym")
    assert payload.source_text == "Stretol som Janka Kováča"
    assert payload.group and len(payload.group) == 32
    assert result.summary == "1 item waiting in Inbox: New person? Janko Kováč"


@pytest.mark.parametrize(
    ("extractor", "note"),
    [
        (None, "Claude isn't configured"),
        (FakeExtractor(ClaudeError("API down")), "Claude couldn't process it"),
        (FakeExtractor(extraction()), "Found nothing specific"),
    ],
)
def test_without_a_usable_answer_the_text_is_kept_as_a_note_draft(
    db: Session, crowd: dict[str, Person], extractor: FakeExtractor | None, note: str
) -> None:
    result = capture.capture(db, extractor, "Pato má psa", today=TODAY, default_currency="EUR")
    (proposal,) = result.proposals
    payload = CapturePayload.model_validate(proposal.payload_json)
    assert [(n.person, n.text) for n in payload.notes] == [(str(crowd["pato"].id), "Pato má psa")]
    assert result.summary.startswith(note)


def test_fallback_leaves_the_person_open_when_ambiguous(db: Session) -> None:
    person(db, "Jana Malá")
    person(db, "Jana Veľká")
    result = capture.capture(db, None, "Jana volala", today=TODAY, default_currency="EUR")
    payload = CapturePayload.model_validate(result.proposals[0].payload_json)
    assert payload.notes[0].person is None
    assert "pick a person" in result.summary


def test_empty_text_is_refused(db: Session) -> None:
    with pytest.raises(InvalidInputError):
        capture.capture(db, None, "   ", today=TODAY, default_currency="EUR")


# --- apply_capture ---


def full_payload(pato: Person) -> CapturePayload:
    return CapturePayload(
        text="…",
        candidates=[pato.id],
        new_people=[NewPersonDraft(name="Janko Kováč", context="Pato's brother")],
        notes=[
            NoteDraft(person=str(pato.id), text="sťahuje sa do Brna"),
            NoteDraft(person="new:0", text="hrá na gitare"),
        ],
        interactions=[
            InteractionDraft(
                people=[str(pato.id), "new:0"], kind=InteractionKind.MEET, summary="pivo"
            )
        ],
        debts=[
            DebtDraft(
                person=str(pato.id),
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.MONEY,
                amount_minor=1200,
                currency="EUR",
                description="pizza",
            ),
            DebtDraft(
                person="new:0",
                direction=DebtDirection.THEY_OWE_ME,
                type=DebtType.ITEM,
                description="charger",
            ),
        ],
        promises=[
            PromiseDraft(
                person=str(pato.id), direction=PromiseDirection.I_PROMISED, text="poslať rozvrh"
            )
        ],
    )


def test_apply_creates_everything(db: Session, crowd: dict[str, Person]) -> None:
    pato = crowd["pato"]
    applied = apply_capture(db, full_payload(pato), today=TODAY, default_currency="EUR")
    assert str(applied) == "1 new person, 2 notes, 1 interaction, 2 debts, 1 promise"

    janko = db.scalars(select(Person).where(Person.display_name == "Janko Kováč")).one()
    assert (janko.first_name, janko.last_name, janko.nameday_country) == ("Janko", "Kováč", "SK")
    assert [n.text for n in janko.notes] == ["hrá na gitare"]
    pizza = db.scalars(select(Debt).where(Debt.description == "pizza")).one()
    assert (pizza.person_id, pizza.amount_minor, pizza.reference_code) == (pato.id, 1200, "D-0001")
    assert db.scalars(select(Debt).where(Debt.type == DebtType.ITEM)).one().person_id == janko.id
    meet = db.scalars(select(Interaction)).one()
    assert {p.id for p in meet.people} == {pato.id, janko.id}
    assert meet.occurred_on == TODAY
    promise = db.scalars(select(Promise)).one()
    assert promise.due == TODAY + timedelta(days=7)


def test_link_uses_the_existing_person(db: Session, crowd: dict[str, Person]) -> None:
    pato, marek = crowd["pato"], crowd["marek"]
    payload = full_payload(pato)
    payload.new_people[0].action = "link"
    payload.new_people[0].person_id = marek.id
    applied = apply_capture(db, payload, today=TODAY, default_currency="EUR")
    assert applied.people == 0
    assert db.scalars(select(Debt).where(Debt.type == DebtType.ITEM)).one().person_id == marek.id
    assert {p.id for p in db.scalars(select(Interaction)).one().people} == {pato.id, marek.id}


def test_skip_drops_that_persons_items(db: Session, crowd: dict[str, Person]) -> None:
    pato = crowd["pato"]
    payload = full_payload(pato)
    payload.new_people[0].action = "skip"
    applied = apply_capture(db, payload, today=TODAY, default_currency="EUR")
    assert str(applied) == "1 note, 1 interaction, 1 debt, 1 promise"
    assert db.scalars(select(Person).where(Person.display_name == "Janko Kováč")).all() == []
    assert [p.id for p in db.scalars(select(Interaction)).one().people] == [pato.id]


def test_unticked_items_are_left_out(db: Session, crowd: dict[str, Person]) -> None:
    payload = full_payload(crowd["pato"])
    for item in payload.items():
        item.include = item in payload.promises
    applied = apply_capture(db, payload, today=TODAY, default_currency="EUR")
    assert str(applied) == "1 promise"
    assert db.scalars(select(Person).where(Person.display_name == "Janko Kováč")).all() == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: setattr(p.debts[0], "amount_minor", None), "Amount missing"),
        (lambda p: setattr(p.notes[0], "person", None), "Pick a person"),
        (lambda p: setattr(p.debts[1], "description", None), "Describe the item"),
        (lambda p: setattr(p.notes[0], "person", "999"), "no longer exists"),
        (lambda p: setattr(p.new_people[0], "action", "link"), "Pick the existing person"),
        (lambda p: setattr(p.interactions[0], "people", []), "Pick who this was with"),
    ],
)
def test_nothing_is_written_when_anything_is_off(
    db: Session, crowd: dict[str, Person], change: Any, message: str
) -> None:
    payload = full_payload(crowd["pato"])
    change(payload)
    people_before = len(db.scalars(select(Person)).all())
    with pytest.raises(InvalidInputError, match=message):
        apply_capture(db, payload, today=TODAY, default_currency="EUR")
    assert len(db.scalars(select(Person)).all()) == people_before
    assert db.scalars(select(Note)).all() == []
    assert db.scalars(select(Debt)).all() == []


def test_the_same_text_again_soon_returns_the_first_capture(
    db: Session, crowd: dict[str, Person]
) -> None:
    extractor = FakeExtractor(slovak_answer(crowd["pato"].id))
    first = capture.capture(db, extractor, SLOVAK, today=TODAY, default_currency="EUR")
    (proposal,) = first.proposals
    later = proposal.created_at + capture.DUPLICATE_WINDOW / 2

    again = capture.capture(
        db, extractor, f"  {SLOVAK} ", today=TODAY, default_currency="EUR", now=later
    )
    assert again.proposals == [proposal]
    assert again.summary.startswith("Already in Inbox:")
    assert len(extractor.requests) == 1  # Claude isn't asked twice

    # Other text, another meeting, much later, or once it's resolved: a new capture.
    other = capture.capture(db, extractor, "iné", today=TODAY, default_currency="EUR", now=later)
    assert other.proposals[0] is not proposal
    much_later = proposal.created_at + capture.DUPLICATE_WINDOW * 2
    fresh = capture.capture(
        db, extractor, SLOVAK, today=TODAY, default_currency="EUR", now=much_later
    )
    assert fresh.proposals[0] is not proposal


def test_one_dictation_shares_a_group_across_its_cards(db: Session) -> None:
    extractor = FakeExtractor(
        extraction(
            notes=[ExtractedNote(person_ref="", text="stretli sme sa")],
            unknown_people=[
                UnknownPerson(name="Janko", context=""),
                UnknownPerson(name="Marta", context=""),
            ],
        )
    )
    result = capture.capture(db, extractor, "Janko a Marta", today=TODAY, default_currency="EUR")
    groups = {p.payload_json["group"] for p in result.proposals}
    assert len(result.proposals) == 3 and len(groups) == 1
    again = capture.capture(db, extractor, "Janko a Marta 2", today=TODAY, default_currency="EUR")
    assert {p.payload_json["group"] for p in again.proposals} != groups
    (group,) = groups
    assert len(capture.pending_in_group(db, group)) == 3
    assert capture.pending_in_group(db, None) == []


def test_a_new_person_payload_without_the_new_fields_still_validates() -> None:
    old = NewPersonPayload.model_validate({"name": "Janko", "context": None})
    assert old.source_text is None and old.group is None
