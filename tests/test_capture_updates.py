"""Capture can settle debts and close promises, and offers recent people (SPEC §2.27)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    InteractionKind,
    MeetingLinkSource,
    PromiseDirection,
    PromiseStatus,
)
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.people import Person
from orbit.integrations.claude import (
    CandidateDebt,
    CandidatePerson,
    CandidatePromise,
    CaptureRequest,
    ExtractedPromiseUpdate,
    ExtractedSettle,
    build_capture_prompt,
)
from orbit.services import capture, interactions, ledger, people, promises
from orbit.services.capture import CapturePayload, apply_capture
from tests.test_capture import TODAY, FakeExtractor, extraction, person

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


def money(db: Session, who: Person, amount: int, *, mine: bool = True, **fields: Any) -> Debt:
    debt = ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=who.id,
            direction=DebtDirection.THEY_OWE_ME if mine else DebtDirection.I_OWE_THEM,
            type=DebtType.MONEY,
            amount_minor=amount,
            currency="EUR",
            description=fields.pop("description", "pizza"),
        ),
        "EUR",
    )
    for key, value in fields.items():
        setattr(debt, key, value)
    db.flush()
    return debt


def settle(debt: Debt, mine: bool = True, amount: str | None = None) -> ExtractedSettle:
    return ExtractedSettle(
        debt_ref=f"debt:{debt.id}",
        direction=DebtDirection.THEY_OWE_ME if mine else DebtDirection.I_OWE_THEM,
        amount=amount,
    )


def run(db: Session, who: Person, answer: Any, text: str = "Paťo mi vrátil 12 €") -> CapturePayload:
    result = capture.capture(db, FakeExtractor(answer), text, today=TODAY, default_currency="EUR")
    (proposal, *_) = result.proposals
    return CapturePayload.model_validate(proposal.payload_json)


def apply(db: Session, payload: CapturePayload) -> capture.AppliedCapture:
    return apply_capture(db, payload, today=TODAY, default_currency="EUR")


# --- Settles ---


def test_a_full_settle_is_proposed_and_applied_only_on_confirm(db: Session) -> None:
    pato = person(db, "Paťo")
    debt = money(db, pato, 1200)
    payload = run(db, pato, extraction(settles=[settle(debt)]))
    (draft,) = payload.settles
    assert (draft.debt_id, draft.amount_minor, draft.person) == (debt.id, None, str(pato.id))
    assert "Paťo owes you 12.00 € (pizza)" in capture.describe(db, payload)[0]
    assert "in full" in capture.describe(db, payload)[0]
    assert debt.is_open  # a proposal only (rule 1)
    result = apply(db, payload)
    assert result.settled == 1 and str(result) == "1 debt settled"
    assert not debt.is_open


def test_a_part_settles_the_debt_and_leaves_a_remainder(db: Session) -> None:
    pato = person(db, "Paťo")
    debt = money(db, pato, 1200)
    payload = run(db, pato, extraction(settles=[settle(debt, amount="5")]))
    assert payload.settles[0].amount_minor == 500
    assert "rest stays open" in capture.describe(db, payload)[0]
    apply(db, payload)
    open_now = ledger.list_debts(db, person_id=pato.id)
    assert [d.amount_minor for d in open_now] == [700]


def test_an_amount_matching_no_debt_makes_no_settle_but_keeps_the_text(db: Session) -> None:
    pato = person(db, "Paťo")
    debt = money(db, pato, 1200)
    payload = run(db, pato, extraction(settles=[settle(debt, amount="20")]), "Paťo mi vrátil 20 €")
    assert payload.settles == []
    assert payload.warnings == ["No open debt of 20.00 € for Paťo."]
    assert [n.text for n in payload.notes] == ["Paťo mi vrátil 20 €"]


def test_unlisted_or_wrong_direction_settles_are_dropped(db: Session) -> None:
    pato = person(db, "Paťo")
    terka = person(db, "Terka")
    listed = money(db, pato, 1200)
    other = money(db, terka, 900)
    wrong_way = settle(listed, mine=False)  # the text says "I paid", the debt is theirs
    unknown = ExtractedSettle(debt_ref="debt:99999", direction=DebtDirection.THEY_OWE_ME)
    not_shown = settle(other)  # Terka is not in the text and not recent
    payload = run(db, pato, extraction(settles=[wrong_way, unknown, not_shown]), "Paťo platil")
    assert payload.settles == []


def test_of_identical_debts_the_oldest_is_settled(db: Session) -> None:
    pato = person(db, "Paťo")
    older = money(db, pato, 1200, created_at=datetime(2026, 8, 1, tzinfo=UTC))
    newer = money(db, pato, 1200, created_at=datetime(2026, 9, 1, tzinfo=UTC))
    payload = run(db, pato, extraction(settles=[settle(newer)]))
    assert [s.debt_id for s in payload.settles] == [older.id]


def test_one_debt_is_settled_once_per_dictation(db: Session) -> None:
    pato = person(db, "Paťo")
    debt = money(db, pato, 1200, description="pizza")
    payload = run(db, pato, extraction(settles=[settle(debt), settle(debt)]))
    assert len(payload.settles) == 1


def test_items_and_favors_settle_too(db: Session) -> None:
    terka = person(db, "Terka")
    item = ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=terka.id,
            direction=DebtDirection.THEY_OWE_ME,
            type=DebtType.ITEM,
            description="powerbanka",
        ),
        "EUR",
    )
    payload = run(db, terka, extraction(settles=[settle(item)]), "Terka mi vrátila powerbanku")
    assert capture.describe(db, payload)[0].startswith("Settle: Terka has your powerbanka")
    apply(db, payload)
    assert not item.is_open


def test_confirm_skips_a_debt_settled_in_the_meantime(db: Session) -> None:
    pato = person(db, "Paťo")
    debt = money(db, pato, 1200)
    terka = person(db, "Terka")
    payload = run(db, pato, extraction(settles=[settle(debt)]))
    payload.promises.append(
        capture.PromiseDraft(
            person=str(terka.id), direction=PromiseDirection.I_PROMISED, text="napísať"
        )
    )
    ledger.settle(db, debt)  # by hand, or by a bank payment match
    result = apply(db, payload)
    assert result.settled == 0 and result.promises == 1
    assert "already settled" in str(result)


# --- Promises ---


def open_promise(db: Session, who: Person, text: str) -> Promise:
    return promises.create_promise(
        db,
        promises.PromiseCreate(person_id=who.id, direction=PromiseDirection.I_PROMISED, text=text),
        TODAY,
    )


def test_a_kept_promise_is_closed_on_confirm(db: Session) -> None:
    terka = person(db, "Terka")
    promise = open_promise(db, terka, "poslať rozvrh")
    update = ExtractedPromiseUpdate(promise_ref=f"promise:{promise.id}", status="done")
    payload = run(db, terka, extraction(promise_updates=[update]), "poslal som Terke rozvrh")
    assert capture.describe(db, payload)[0] == "Promise done: You promised Terka: poslať rozvrh"
    assert promise.status is PromiseStatus.OPEN
    result = apply(db, payload)
    assert result.closed == 1 and promise.status is PromiseStatus.DONE


def test_dropped_status_and_unlisted_promises(db: Session) -> None:
    terka = person(db, "Terka")
    promise = open_promise(db, terka, "poslať rozvrh")
    ghost = ExtractedPromiseUpdate(promise_ref="promise:424242", status="done")
    drop = ExtractedPromiseUpdate(promise_ref=f"promise:{promise.id}", status="dropped")
    payload = run(db, terka, extraction(promise_updates=[ghost, drop]), "Terke to nepošlem")
    assert [(u.promise_id, u.status) for u in payload.promise_updates] == [(promise.id, "dropped")]
    apply(db, payload)
    assert promise.status is PromiseStatus.DROPPED


# --- What Claude is told ---


def test_the_prompt_lists_open_debts_and_promises_under_each_person(db: Session) -> None:
    pato = person(db, "Paťo")
    debt = money(db, pato, 1200)
    promise = open_promise(db, pato, "poslať rozvrh")
    fake = FakeExtractor(extraction())
    capture.capture(db, fake, "Paťo mi vrátil 12 €", today=TODAY, default_currency="EUR")
    prompt = build_capture_prompt(fake.requests[0])
    assert f'debt:{debt.id}: they_owe_me, money, 12.00 €, for "pizza"' in prompt
    assert f'promise:{promise.id}: i_promised, "poslať rozvrh"' in prompt


def test_open_items_are_capped_per_person(db: Session) -> None:
    pato = person(db, "Paťo")
    for n in range(15):
        money(db, pato, 100 + n)
    shown = capture.open_items(db, [pato])
    assert len(shown.by_person[pato.id][0]) == capture.MAX_OPEN_PER_PERSON
    assert len(shown.debts) == capture.MAX_OPEN_PER_PERSON


def test_the_prompt_text_for_one_debt_masks_phone_numbers() -> None:
    request = CaptureRequest(
        text="x",
        today=TODAY,
        candidates=(
            CandidatePerson(
                1,
                "Paťo",
                debts=(
                    CandidateDebt(
                        3, "they_owe_me", "item", "kniha +421 905 123 456", None, None, TODAY
                    ),
                ),
                promises=(CandidatePromise(4, "i_promised", "call 0905 123 456", None),),
            ),
        ),
        default_currency="EUR",
    )
    prompt = build_capture_prompt(request)
    assert "123 456" not in prompt and "••• 456" in prompt


# --- Recall (C2) ---


def test_recall_offers_close_recent_and_meeting_people(db: Session) -> None:
    close = person(db, "Blízky")
    people.add_tag(db, close, "close")
    talked = person(db, "Hovorený")
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=TODAY - timedelta(days=3),
            kind=InteractionKind.CALL,
            person_ids=[talked.id],
        ),
    )
    stale = person(db, "Dávny")
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=TODAY - timedelta(days=60), person_ids=[stale.id]
        ),
    )
    met = person(db, "Dnešný")
    meeting = Meeting(
        calendar_uid="u1",
        calendar_name="Personal",
        title="káva",
        start_utc=NOW + timedelta(hours=3),
        end_utc=NOW + timedelta(hours=4),
    )
    db.add(meeting)
    db.flush()
    db.add(
        MeetingPerson(meeting_id=meeting.id, person_id=met.id, link_source=MeetingLinkSource.MANUAL)
    )
    db.flush()
    found = capture.recall_people(db, NOW, capture.DEFAULT_TZ, exclude=set())
    names = {p.display_name for p in found}
    assert names == {"Blízky", "Hovorený", "Dnešný"}
    excluded = capture.recall_people(db, NOW, capture.DEFAULT_TZ, exclude={close.id})
    assert "Blízky" not in {p.display_name for p in excluded}


def test_a_text_naming_nobody_still_offers_recent_people_and_their_debts(db: Session) -> None:
    pato = person(db, "Paťo")
    people.add_tag(db, pato, "close")
    debt = money(db, pato, 1200)
    fake = FakeExtractor(extraction())
    capture.capture(db, fake, "vrátil mi 12 eur", today=TODAY, default_currency="EUR", now=NOW)
    (request,) = fake.requests
    assert [c.id for c in request.candidates] == [pato.id]
    assert request.candidates[0].debts[0].id == debt.id


def test_the_note_fallback_ignores_recalled_people(db: Session) -> None:
    pato = person(db, "Paťo")
    people.add_tag(db, pato, "close")
    result = capture.capture(db, None, "niečo si myslím", today=TODAY, default_currency="EUR")
    payload = CapturePayload.model_validate(result.proposals[0].payload_json)
    assert payload.notes[0].person is None  # one recalled person is not "the one named"
