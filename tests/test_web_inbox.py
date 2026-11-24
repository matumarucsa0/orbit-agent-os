from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from orbit.domain.enums import DebtDirection, DebtType
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt
from orbit.integrations.claude import ExtractedDebt, ExtractedNote, UnknownPerson
from tests.test_capture import FakeExtractor, extraction, slovak_answer
from tests.test_web_ledger import post, text
from tests.test_web_people import add

SLOVAK = "zaplatil som Paťovi pizzu 12€, sľúbil som mu poslať rozvrh"


def proposal_ids(page: str) -> list[int]:
    return [int(i) for i in dict.fromkeys(re.findall(r'id="proposal-(\d+)"', page))]


def test_inbox_pages_require_login(client: TestClient) -> None:
    for method, path in (
        ("get", "/inbox"),
        ("get", "/inbox/1"),
        ("post", "/inbox/capture"),
        ("post", "/inbox/1/confirm"),
        ("post", "/inbox/1/save"),
        ("post", "/inbox/1/reject"),
    ):
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_capture_confirm_from_the_web(web: TestClient, app: FastAPI) -> None:
    """ROADMAP Phase 4 'done when', through the web capture box."""
    pato = add(web, "Patrik Veselý")
    post(web, f"/people/{pato}/aliases", value="Paťo")
    app.state.claude = FakeExtractor(slovak_answer(pato))

    page = post(web, "/inbox/capture", text=SLOVAK)
    assert "Claude is reading it" in text(page)  # the flash: saved at once, read afterwards
    assert "Patrik Veselý owes you 12.00 € (pizza)" in text(page)
    assert "You promised Patrik Veselý: poslať rozvrh" in text(page)
    assert 'class="badge"' in page  # nav count
    (proposal,) = proposal_ids(page)

    page = post(web, f"/inbox/{proposal}/confirm")
    assert "Saved: 1 debt, 1 promise." in text(page)
    assert "All caught up." in text(page)
    assert 'class="badge"' not in page

    person = web.get(f"/people/{pato}").text
    assert "Owes you 12.00 €" in text(person)
    assert "poslať rozvrh" in text(person)


def test_missing_amount_prompt(web: TestClient, app: FastAPI) -> None:
    pato = add(web, "Pato")
    app.state.claude = FakeExtractor(
        extraction(
            debts=[
                ExtractedDebt(
                    person_ref=str(pato),
                    direction=DebtDirection.THEY_OWE_ME,
                    type=DebtType.MONEY,
                    amount=None,
                    currency=None,
                    description="coffee",
                )
            ]
        )
    )
    page = post(web, "/inbox/capture", text="zaplatil som Patovi kávu")
    assert "Amount?" in text(page)
    assert "Pato owes you an amount (missing) (coffee)" in text(page)
    (proposal,) = proposal_ids(page)

    # Confirming without it goes to the edit page with the reason.
    page = post(web, f"/inbox/{proposal}/confirm")
    assert "Amount missing" in text(page)

    page = post(
        web, f"/inbox/{proposal}/confirm", **{"debt-0-amount": "3,20", "debt-0-currency": "eur"}
    )
    assert "Saved: 1 debt." in text(page)
    person = web.get(f"/people/{pato}").text
    assert "Owes you 3.20 €" in text(person)


def test_bad_amount_is_explained(web: TestClient, app: FastAPI) -> None:
    pato = add(web, "Pato")
    answer = slovak_answer(pato)
    answer.debts[0].amount = None
    app.state.claude = FakeExtractor(answer)
    (proposal,) = proposal_ids(post(web, "/inbox/capture", text="kávu"))
    page = post(web, f"/inbox/{proposal}/confirm", **{"debt-0-amount": "twelve"})
    assert "isn't an amount" in text(page)


def test_edit_form(web: TestClient, app: FastAPI) -> None:
    pato, terka = add(web, "Paťo Veselý"), add(web, "Terka")
    app.state.claude = FakeExtractor(slovak_answer(pato))
    (proposal,) = proposal_ids(post(web, "/inbox/capture", text=SLOVAK))

    page = web.get(f"/inbox/{proposal}").text
    assert 'name="debt-0-amount"' in page and 'value="12.00"' in page
    assert 'name="promise-0-text" value="poslať rozvrh"' in page
    assert f'<option value="{terka}"' in page  # everyone is pickable

    # Move the debt to Terka, change it to 15, and untick the promise.
    form = {
        "full": "1",
        "debt-0-include": "1",
        "debt-0-person": str(terka),
        "debt-0-direction": "they_owe_me",
        "debt-0-type": "money",
        "debt-0-amount": "15",
        "debt-0-currency": "EUR",
        "debt-0-description": "pizza",
        "promise-0-person": str(pato),
        "promise-0-direction": "i_promised",
        "promise-0-text": "poslať rozvrh",
        "promise-0-due": "",
    }
    page = post(web, f"/inbox/{proposal}/save", **form)
    assert "Saved your changes." in text(page)
    assert 'value="15.00"' in page

    page = post(web, f"/inbox/{proposal}/confirm", **form)
    assert "Saved: 1 debt." in text(page)
    assert "Owes you 15.00 €" in text(web.get(f"/people/{terka}").text)
    assert "poslať rozvrh" not in web.get(f"/people/{pato}").text


def test_new_people_in_a_capture(web: TestClient, app: FastAPI) -> None:
    marek = add(web, "Marek Horváth")
    app.state.claude = FakeExtractor(
        extraction(
            notes=[ExtractedNote(person_ref="new:0", text="nový kolega")],
            unknown_people=[UnknownPerson(name="Maroš", context="colleague")],
        )
    )
    page = post(web, "/inbox/capture", text="Maroš je nový kolega")
    assert "Note on Maroš: nový kolega" in text(page)
    (proposal,) = proposal_ids(page)
    assert 'name="new-0-action"' in web.get(f"/inbox/{proposal}").text

    page = post(
        web,
        f"/inbox/{proposal}/confirm",
        full="1",
        **{
            "new-0-name": "Maroš",
            "new-0-action": "link",
            "new-0-person_id": str(marek),
            "note-0-include": "1",
            "note-0-person": "new:0",
            "note-0-text": "nový kolega",
        },
    )
    assert "Saved: 1 note." in text(page)
    assert "nový kolega" in web.get(f"/people/{marek}").text


def test_new_person_card(web: TestClient, app: FastAPI) -> None:
    app.state.claude = FakeExtractor(
        extraction(unknown_people=[UnknownPerson(name="Janko Kováč", context="from the gym")])
    )
    page = post(web, "/inbox/capture", text="stretol som Janka z gymu")
    assert "Janko Kováč · from the gym" in text(page)
    assert "Add person" in page
    (proposal,) = proposal_ids(page)
    response = web.post(f"/inbox/{proposal}/confirm", follow_redirects=False)
    page = web.get(response.headers["location"]).text
    assert "Added Janko Kováč." in text(page)
    match = re.search(r'href="(/people/\d+)">Open', page)
    assert match
    assert "from the gym" in web.get(match.group(1)).text


def test_reject(web: TestClient, app: FastAPI) -> None:
    pato = add(web, "Paťo")
    app.state.claude = FakeExtractor(slovak_answer(pato))
    (proposal,) = proposal_ids(post(web, "/inbox/capture", text=SLOVAK))
    page = post(web, f"/inbox/{proposal}/reject")
    assert "Rejected." in text(page)
    assert proposal_ids(page) == []
    page = web.get(f"/inbox/{proposal}").text
    assert "Rejected" in text(page)
    assert "Owes you" not in text(web.get(f"/people/{pato}").text)
    assert "already rejected" in text(post(web, f"/inbox/{proposal}/reject"))


def test_capture_without_claude(web: TestClient) -> None:
    page = post(web, "/inbox/capture", text="dôležitá poznámka")
    assert "Claude isn't configured" in text(page)
    assert "Note on someone (pick a person): dôležitá poznámka" in text(page)


def test_empty_capture(web: TestClient) -> None:
    assert "Nothing to capture" in text(post(web, "/inbox/capture", text="  "))


def test_home_shows_the_inbox_count(web: TestClient) -> None:
    assert "📥 Inbox" not in web.get("/").text
    post(web, "/inbox/capture", text="niečo")
    assert "1 waiting" in text(web.get("/").text)


def test_reopen_a_rejected_proposal(web: TestClient, app: FastAPI) -> None:
    pato = add(web, "Paťo")
    app.state.claude = FakeExtractor(slovak_answer(pato))
    (proposal,) = proposal_ids(post(web, "/inbox/capture", text=SLOVAK))
    page = post(web, f"/inbox/{proposal}/reject")
    assert "Recently rejected" in text(page)
    assert f'action="/inbox/{proposal}/reopen"' in page
    assert f'action="/inbox/{proposal}/reopen"' in web.get(f"/inbox/{proposal}").text

    page = post(web, f"/inbox/{proposal}/reopen")
    assert "Reopened." in text(page)
    inbox_page = web.get("/inbox").text
    assert proposal_ids(inbox_page) == [proposal]
    assert "Recently rejected" not in text(inbox_page)
    assert "can be reopened" in text(post(web, f"/inbox/{proposal}/reopen"))


def test_appointment_card_edit_and_confirm(web: TestClient, app: FastAPI) -> None:
    from datetime import date, timedelta

    from orbit.integrations.claude import ExtractedAppointment

    day = (date.today() + timedelta(days=3)).isoformat()  # inside the Meetings page window
    barber = add(web, "Roman Holič")
    app.state.claude = FakeExtractor(
        extraction(
            appointments=[
                ExtractedAppointment(person_ref=str(barber), title="Haircut", start=f"{day}T15:00")
            ]
        )
    )
    (proposal,) = proposal_ids(post(web, "/inbox/capture", text="holič v pondelok o 15:00"))

    page = web.get(f"/inbox/{proposal}").text
    assert "Appointments" in page
    assert 'name="appointment-0-title" value="Haircut"' in page
    assert f'name="appointment-0-start" value="{day}T15:00"' in page

    form = {
        "full": "1",
        "appointment-0-include": "1",
        "appointment-0-person": str(barber),
        "appointment-0-title": "Haircut + beard",
        "appointment-0-start": f"{day}T16:30",
        "appointment-0-end": "",
        "appointment-0-location": "",
    }
    page = post(web, f"/inbox/{proposal}/confirm", **form)
    assert "Saved: 1 appointment." in text(page)
    listing = web.get("/meetings").text
    assert "Haircut + beard" in listing

    meeting_id = re.search(r"/meetings/(\d+)", listing)
    assert meeting_id is not None
    assert "Cancel appointment" in web.get(f"/meetings/{meeting_id[1]}").text
    page = post(web, f"/meetings/{meeting_id[1]}/cancel")
    assert "Appointment cancelled." in text(page)
    assert "Haircut + beard" not in web.get("/meetings").text


def test_add_a_blank_appointment(web: TestClient, app: FastAPI) -> None:
    app.state.claude = FakeExtractor(extraction(notes=[ExtractedNote(person_ref="", text="x")]))
    (proposal,) = proposal_ids(post(web, "/inbox/capture", text="x"))

    page = post(web, f"/inbox/{proposal}/save", add="appointment", full="1")

    assert 'name="appointment-0-start"' in page


def two_new_people(web: TestClient, app: FastAPI, dictation: str) -> str:
    app.state.claude = FakeExtractor(
        extraction(
            unknown_people=[
                UnknownPerson(name="Janko", context=""),
                UnknownPerson(name="Marta", context=""),
            ]
        )
    )
    return post(web, "/inbox/capture", text=dictation)


def test_new_person_card_shows_what_was_dictated(web: TestClient, app: FastAPI) -> None:
    page = two_new_people(web, app, "stretol som Janka a Martu")
    assert text(page).count("“stretol som Janka a Martu”") == 2
    long_text = "x" * 300
    page = two_new_people(web, app, long_text)
    quotes = re.findall(r'<p class="quote small">“(.*?)”</p>', page)
    assert [q for q in quotes if q.startswith("x")] == ["x" * 200 + "…"] * 2
    first = max(proposal_ids(page))
    assert "x" * 300 in web.get(f"/inbox/{first}").text  # the card page has all of it


def test_reject_all_from_this_capture(web: TestClient, app: FastAPI) -> None:
    page = two_new_people(web, app, "prvý diktát")
    other = two_new_people(web, app, "druhý diktát")
    ids = proposal_ids(other)
    assert len(ids) == 4
    assert page.count("Reject all 2 from this capture") == 2
    newest = ids[0]  # newest first
    after = post(web, f"/inbox/{newest}/reject-group")
    assert "Rejected 2 from this capture." in text(after)
    assert len(proposal_ids(after)) == 2  # the other dictation is untouched
    assert "Reject all" in after  # ...and still has its button
    # Reversible: Reopen puts one back and nothing was applied.
    reopened = post(web, f"/inbox/{newest}/reopen")
    assert newest in proposal_ids(reopened)


def test_no_reject_all_button_for_a_single_card(web: TestClient, app: FastAPI) -> None:
    app.state.claude = FakeExtractor(
        extraction(unknown_people=[UnknownPerson(name="Janko", context="")])
    )
    page = post(web, "/inbox/capture", text="stretol som Janka")
    assert "Reject all" not in page


def test_old_new_person_cards_without_text_still_render(web: TestClient, app: FastAPI) -> None:
    from orbit.domain.enums import ProposalKind, ProposalStatus
    from orbit.domain.inbox import Proposal

    with app.state.sessionmaker() as db:
        db.add(
            Proposal(
                kind=ProposalKind.NEW_PERSON,
                status=ProposalStatus.PENDING,
                summary="New person? Stará",
                payload_json={"name": "Stará", "context": None},
                confidence=None,
                source_type="capture",
            )
        )
        db.commit()
    page = web.get("/inbox").text
    assert "Stará" in page and "quote" not in page and "Reject all" not in page


def test_capture_forms_have_a_waiting_label(web: TestClient) -> None:
    assert 'data-busy="Sending to Claude…"' in web.get("/").text
    assert 'data-busy="Sending to Claude…"' in web.get("/inbox").text


def test_editing_a_capture_card_keeps_its_group(web: TestClient, app: FastAPI) -> None:
    pato = add(web, "Paťo")
    app.state.claude = FakeExtractor(slovak_answer(pato))
    (proposal,) = proposal_ids(post(web, "/inbox/capture", text=SLOVAK))
    with app.state.sessionmaker() as db:
        before = db.get(Proposal, proposal).payload_json["group"]
    post(web, f"/inbox/{proposal}/save", full="1")
    with app.state.sessionmaker() as db:
        assert db.get(Proposal, proposal).payload_json["group"] == before


def test_capture_settles_a_debt_from_the_web(web: TestClient, app: FastAPI) -> None:
    from orbit.integrations.claude import ExtractedSettle

    pato = add(web, "Patrik Veselý")
    post(web, f"/people/{pato}/aliases", value="Paťo")
    post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="12")
    with app.state.sessionmaker() as db:
        debt_id = db.scalars(select(Debt.id)).one()
    app.state.claude = FakeExtractor(
        extraction(
            settles=[
                ExtractedSettle(debt_ref=f"debt:{debt_id}", direction=DebtDirection.THEY_OWE_ME)
            ]
        )
    )
    page = post(web, "/inbox/capture", text="Paťo mi vrátil 12 €")
    assert "Settle: Patrik Veselý owes you 12.00 € · in full" in text(page)
    (proposal,) = proposal_ids(page)
    edit = web.get(f"/inbox/{proposal}").text
    assert 'name="settle-0-amount"' in edit
    post(
        web,
        f"/inbox/{proposal}/save",
        full="1",
        **{"settle-0-include": "1", "settle-0-amount": "5"},
    )
    assert "5.00 € paid, the rest stays open" in text(web.get("/inbox").text)
    page = post(web, f"/inbox/{proposal}/confirm")
    assert "Saved: 1 debt settled." in text(page)
    ledger_page = text(web.get("/ledger").text)
    assert "Owed to you 7.00 €" in ledger_page
