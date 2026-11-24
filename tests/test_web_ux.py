"""Web UI conveniences: edits in place, anchors after a change, Undo instead of "Are you sure?",
tappable handles, filter counts, the split preview and adding items to a capture by hand."""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.integrations.claude import ExtractedNote
from orbit.web.templating import full_name
from tests.test_bank_parsers import tatra_bytes
from tests.test_capture import FakeExtractor, extraction
from tests.test_web_bank import split_form_url, upload
from tests.test_web_inbox import proposal_ids
from tests.test_web_ledger import debt_ids, post, text
from tests.test_web_people import add


def location(web: TestClient, url: str, **data: str) -> str:
    response = web.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return str(response.headers["location"])


def test_full_name_left_out_when_it_repeats_the_display_name() -> None:
    assert full_name("Ema", "Kráľová", "Ema Kráľová") == ""
    assert full_name("ema", "kráľová", "Ema Kráľová") == ""
    assert full_name("Patrik", "Veselý", "Paťo") == "Patrik Veselý"
    assert full_name(None, None, "Paťo") == ""


def test_changes_return_to_their_section(web: TestClient) -> None:
    pato = add(web, "Pato")
    assert location(web, f"/people/{pato}/notes", text="hi") == f"/people/{pato}#timeline"
    assert location(web, f"/people/{pato}/tags", name="gym") == f"/people/{pato}#tags"
    assert location(web, f"/people/{pato}/aliases", value="Paťo") == f"/people/{pato}#tags"
    assert (
        location(web, f"/people/{pato}/handles", type="email", value="p@example.com")
        == f"/people/{pato}#handles"
    )
    assert (
        location(web, f"/people/{pato}/interactions", kind="meet", occurred_on="2026-09-01")
        == f"/people/{pato}#timeline"
    )


def test_edit_a_note_and_an_interaction(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/notes", text="Has a dgo.")
    note = re.search(rf'action="/people/{pato}/notes/(\d+)/edit"', page)
    assert note
    assert location(web, note.group(0).split('"')[1], text="Has a dog.") == (
        f"/people/{pato}#note-{note.group(1)}"
    )
    page = web.get(f"/people/{pato}").text
    assert "Has a dog." in text(page) and "dgo" not in page

    page = post(web, f"/people/{pato}/interactions", kind="meet", occurred_on="2026-09-01")
    found = re.search(rf'action="/people/{pato}/interactions/(\d+)/edit"', page)
    assert found
    post(
        web,
        found.group(0).split('"')[1],
        kind="call",
        occurred_on="2026-09-02",
        summary="caught up",
    )
    shown = text(web.get(f"/people/{pato}").text)
    assert "2 Sep 2026 · call" in shown and "caught up" in shown


def test_edit_a_debt_keeps_its_reference(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(
        web,
        f"/people/{pato}/debts",
        direction="they_owe_me",
        type="money",
        amount="12",
        description="piza",
    )
    (debt,) = debt_ids(page, "edit")
    page = post(web, f"/debts/{debt}/edit", amount="12.50", currency="EUR", description="pizza")
    assert "Saved D-0001." in text(page)
    assert "Owes you 12.50 € · pizza" in text(page)
    page = post(web, f"/debts/{debt}/edit", amount="abc", currency="EUR", description="pizza")
    assert "isn't an amount" in text(page)


def test_edit_a_promise(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/promises", direction="i_promised", text="send fotos")
    promise = re.search(r'action="/promises/(\d+)/edit"', page)
    assert promise
    page = post(
        web,
        f"/promises/{promise.group(1)}/edit",
        direction="i_promised",
        text="send photos",
        due="2026-10-10",
    )
    assert "You promised: send photos" in text(page) and "due 10 Oct 2026" in text(page)
    page = post(web, f"/promises/{promise.group(1)}/edit", direction="i_promised", text=" ")
    assert "A promise needs some text." in text(page)


def test_settle_and_promise_status_offer_undo(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="12")
    (debt,) = debt_ids(page, "settle")
    assert "confirm('Mark as settled?')" not in page
    page = post(web, f"/debts/{debt}/settle", next=f"/people/{pato}#ledger")
    assert "Settled." in text(page)
    assert f'action="/debts/{debt}/unsettle"' in page
    page = post(web, f"/debts/{debt}/unsettle", next=f"/people/{pato}#ledger")
    assert "Owes you 12.00 €" in text(page)

    page = post(web, f"/people/{pato}/promises", direction="i_promised", text="call mum")
    promise = re.search(r'action="/promises/(\d+)/status"', page)
    assert promise
    page = post(web, f"/promises/{promise.group(1)}/status", status="done")
    assert "Marked done." in text(page)
    assert 'name="status" value="open"' in page  # the Undo button puts it back
    page = post(web, f"/promises/{promise.group(1)}/status", status="open")
    assert "Reopened." in text(page)


def test_removing_a_handle_alias_or_tag_can_be_undone(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(
        web, f"/people/{pato}/handles", type="phone", value="+421 905 123 456", label="work"
    )
    assert 'href="tel:+421905123456"' in page and 'href="sms:+421905123456"' in page
    handle = re.search(rf'action="/people/{pato}/handles/(\d+)/delete"', page)
    assert handle
    page = post(web, handle.group(0).split('"')[1])
    assert "Removed +421 905 123 456." in text(page)
    assert 'name="label" value="work"' in page  # Undo re-adds it as it was
    page = post(
        web, f"/people/{pato}/handles", type="phone", value="+421 905 123 456", label="work"
    )
    assert 'href="tel:+421905123456"' in page

    page = post(web, f"/people/{pato}/tags", name="gym")
    tag = re.search(rf'action="/people/{pato}/tags/(\d+)/delete"', page)
    assert tag
    page = post(web, tag.group(0).split('"')[1])
    assert "Removed #gym." in text(page) and 'name="name" value="gym"' in page


def test_person_header_shows_how_we_met_and_photo(web: TestClient) -> None:
    pato = add(web, "Patrik Veselý")
    page = post(
        web,
        f"/people/{pato}/edit",
        display_name="Patrik Veselý",
        first_name="Patrik",
        last_name="Veselý",
        how_met="climbing gym",
        photo_url="https://example.com/p.jpg",
        country="SK",
        city="Bratislava",
    )
    shown = text(page)
    assert "How we met: climbing gym" in shown
    assert 'class="avatar" src="https://example.com/p.jpg"' in page
    assert "Bratislava, SK" in shown
    assert "Patrik Veselý · Bratislava" not in shown  # the full name isn't repeated


def test_home_has_capture_and_message_shortcuts(web: TestClient) -> None:
    home = web.get("/").text
    assert 'action="/inbox/capture"' in home
    assert 'href="/people?add=1#add"' in home
    people_page = web.get("/people?add=1").text
    assert '<details class="card" id="add" open' in people_page


def test_import_page_counts_and_own_money(web: TestClient) -> None:
    page = upload(web, tatra_bytes())
    chips = dict(re.findall(r'show=(\w+)"[^>]*>[^<]*<span class="count">(\d+)</span>', page))
    assert set(chips) == {"shared", "received", "unclassified", "all"}
    assert int(chips["all"]) >= int(chips["shared"]) > 0
    all_rows = web.get("/import/1?show=all").text
    for row in re.findall(r'<li class="list-row tx-row">.*?</li>', all_rows, re.S):
        if ">own money<" in row:
            assert "Split" not in row and "Match" not in row


def test_split_preview_and_confirm_needs_people(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = upload(web, tatra_bytes())
    url = split_form_url(page)
    form = web.get(url).text
    tx = re.search(r'hx-post="/split/(\d+)/preview"', form)
    assert tx and "needs-people" in form
    preview = web.post(
        f"/split/{tx.group(1)}/preview",
        data={"person": str(pato), "mode": "even", "include_me": "1"},
    ).text
    assert "Pato" in text(preview) and "owes you" in text(preview)
    bad = web.post(
        f"/split/{tx.group(1)}/preview",
        data={"person": str(pato), "mode": "custom", f"amount-{pato}": "abc"},
    ).text
    assert "isn't an amount" in text(bad)
    # Nothing was saved by the previews.
    assert "Pato" not in text(web.get(url).text).split("Who was in?")[1].split("How?")[0]


def test_add_a_missed_item_to_a_capture(web: TestClient, app: FastAPI) -> None:
    pato = add(web, "Pato")
    app.state.claude = FakeExtractor(
        extraction(notes=[ExtractedNote(person_ref=str(pato), text="new job")])
    )
    page = post(web, "/inbox/capture", text="Pato has a new job, owes me 5")
    (proposal,) = proposal_ids(page)
    form = web.get(f"/inbox/{proposal}").text
    assert 'name="add" value="debt"' in form
    response = web.post(
        f"/inbox/{proposal}/save",
        data={
            "full": "1",
            "note-0-include": "1",
            "note-0-person": str(pato),
            "note-0-text": "new job",
            "add": "debt",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/inbox/{proposal}#debt-new"
    form = web.get(f"/inbox/{proposal}").text
    assert 'name="debt-0-amount"' in form and 'id="debt-new"' in form
    page = post(
        web,
        f"/inbox/{proposal}/confirm",
        full="1",
        **{
            "note-0-include": "1",
            "note-0-person": str(pato),
            "note-0-text": "new job",
            "debt-0-include": "1",
            "debt-0-person": str(pato),
            "debt-0-direction": "they_owe_me",
            "debt-0-type": "money",
            "debt-0-amount": "5",
            "debt-0-currency": "EUR",
            "debt-0-description": "lunch",
        },
    )
    assert "1 debt" in text(page) and "1 note" in text(page)
    assert "Owes you 5.00 €" in text(web.get(f"/people/{pato}").text)


def test_error_toasts_are_alerts_and_nav_is_a_tab_bar(web: TestClient) -> None:
    pato = add(web, "Pato")
    page = post(web, f"/people/{pato}/debts", direction="they_owe_me", type="money", amount="abc")
    assert 'class="flash flash-error" role="alert"' in page
    assert 'class="tabbar"' in page and 'href="/import"' in page
    # A flash linking to the page it's shown on drops the link.
    page = post(web, "/inbox/capture", text="hello")
    flash = re.search(r'<div class="flash .*?</div>', page, re.S)
    assert flash and 'href="/inbox"' not in flash.group(0)


def test_match_by_hand_stores_nothing_until_saved(web: TestClient) -> None:
    upload(web, tatra_bytes())
    tx = re.search(r'href="/transactions/(\d+)/match', web.get("/import/1?show=received").text)
    if tx is None:  # every received row already has a pending match: reject one first
        first = proposal_ids(web.get("/inbox").text)
        for pid in first:
            web.post(f"/inbox/{pid}/reject")
        tx = re.search(r'href="/transactions/(\d+)/match', web.get("/import/1?show=received").text)
    assert tx
    pending = len(proposal_ids(web.get("/inbox").text))
    form = web.get(f"/transactions/{tx.group(1)}/match").text
    assert "Who sent it?" in text(form) and "Cancel" in text(form)
    assert f'action="/transactions/{tx.group(1)}/match/confirm"' in form
    assert len(proposal_ids(web.get("/inbox").text)) == pending
    response = web.post(
        f"/transactions/{tx.group(1)}/match/save", data={"full": "1"}, follow_redirects=False
    )
    assert response.headers["location"].startswith("/inbox/")
    assert len(proposal_ids(web.get("/inbox").text)) == pending + 1


def test_double_capture_from_the_web_makes_one_item(web: TestClient) -> None:
    post(web, "/inbox/capture", text="Pato has a new job")
    page = post(web, "/inbox/capture", text="Pato has a new job")
    assert "Already in Inbox" in text(page)
    assert len(proposal_ids(page)) == 1
