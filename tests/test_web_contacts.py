"""Contact sync review pages."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.deps import get_contact_source
from orbit.domain.contacts import SyncChange
from orbit.domain.enums import SyncState
from orbit.domain.people import Person
from orbit.integrations.carddav import CardDavError, RawCard
from orbit.services import people

FIXTURE = Path(__file__).parent / "fixtures" / "vcard" / "apple_contacts.vcf"


class FakeSource:
    def __init__(self, text: str | None = None, error: bool = False) -> None:
        self.text = text if text is not None else FIXTURE.read_text(encoding="utf-8")
        self.error = error

    def fetch_cards(self) -> list[RawCard]:
        if self.error:
            raise CardDavError(
                "The contacts server rejected the username or app-specific password."
            )
        return [
            RawCard(chunk + "END:VCARD\n", href=f"/card/{i}.vcf", etag=f'"e{i}"')
            for i, chunk in enumerate(self.text.split("END:VCARD\n"))
            if chunk.strip()
        ]


def sync_now(web: TestClient) -> str:
    response = web.post("/contacts/sync", follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def change_ids(page: str) -> list[int]:
    return [int(m) for m in re.findall(r'name="seen" value="(\d+)"', page)]


def form(seen: list[int], ticked: list[int] = ()) -> dict[str, list[str] | str]:
    data: dict[str, list[str] | str] = {"seen": [str(i) for i in seen]}
    data.update({f"c{i}": "apply" for i in ticked})
    return data


def test_requires_login(client: TestClient) -> None:
    for method, path in (("get", "/contacts/sync"), ("post", "/contacts/sync")):
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_sync_without_credentials_or_with_a_server_error(app: FastAPI, web: TestClient) -> None:
    assert "iCloud isn" in web.post("/contacts/sync").text
    app.dependency_overrides[get_contact_source] = lambda: FakeSource(error=True)
    assert "iCloud sync failed" in web.post("/contacts/sync").text


def test_review_then_apply_new_contacts(app: FastAPI, web: TestClient, db_session) -> None:
    app.dependency_overrides[get_contact_source] = lambda: FakeSource()
    url = sync_now(web)
    page = web.get(url)
    assert "New contacts" in page.text and "Patrik Veselý" in page.text
    assert "Nothing is changed until you press Apply." in page.text
    ids = change_ids(page.text)
    assert len(ids) == 7
    with db_session() as db:
        assert db.query(Person).count() == 0  # a review writes no people

    # Take the first two, leave the rest unticked.
    done = web.post(f"{url}/apply", data=form(ids, ids[:2]))
    assert "Sync: 2 applied, 5 skipped." in done.text
    with db_session() as db:
        assert db.query(Person).count() == 2

    # The five I skipped are remembered; a second sync has nothing new to say.
    again = web.get(sync_now(web))
    assert "Everything is in sync." in again.text


def test_undo_from_the_done_list(app: FastAPI, web: TestClient, db_session) -> None:
    app.dependency_overrides[get_contact_source] = lambda: FakeSource()
    url = sync_now(web)
    first = change_ids(web.get(url).text)[0]
    page = web.post(f"{url}/apply", data=form([first], [first]))
    assert "Undo" in page.text
    undone = web.post(
        f"/contacts/sync/changes/{first}/undo", data={"run_id": url.rsplit("/", 1)[1]}
    )
    assert "Undone:" in undone.text
    with db_session() as db:
        assert db.query(Person).count() == 0
        assert db.get(SyncChange, first).state is SyncState.UNDONE


def test_filter_shows_only_matching_people_and_keeps_the_rest_open(
    app: FastAPI, web: TestClient, db_session
) -> None:
    app.dependency_overrides[get_contact_source] = lambda: FakeSource()
    url = sync_now(web)
    filtered = web.get(f"{url}?q=matus+nic")
    assert "Nothing matches that filter." in filtered.text
    narrowed = web.get(f"{url}?q=patrik")
    shown = change_ids(narrowed.text)
    assert len(shown) == 1
    web.post(f"{url}/apply", data=form(shown, shown))
    with db_session() as db:
        assert db.query(Person).count() == 1
    # The run is still open for the six I did not look at.
    assert len(change_ids(web.get(url).text)) == 6


def test_vcf_upload_is_reviewed_like_a_sync(web: TestClient) -> None:
    files = {"file": ("contacts.vcf", FIXTURE.read_bytes(), "text/vcard")}
    response = web.post("/contacts/sync/vcf", files=files, follow_redirects=False)
    assert response.status_code == 303
    page = web.get(response.headers["location"])
    assert "File" in page.text and len(change_ids(page.text)) == 7
    bad = {"file": ("x.vcf", "FN:Kováč".encode("cp1250"), "text/vcard")}
    assert "not UTF-8" in web.post("/contacts/sync/vcf", files=bad).text


def test_forget_skipped_changes(app: FastAPI, web: TestClient) -> None:
    app.dependency_overrides[get_contact_source] = lambda: FakeSource()
    url = sync_now(web)
    ids = change_ids(web.get(url).text)
    web.post(f"{url}/apply", data=form(ids))  # skip everything
    assert "Everything is in sync." in web.get(sync_now(web)).text
    assert "Forgot 7" in web.post("/contacts/sync/forget").text
    assert len(change_ids(web.get(sync_now(web)).text)) == 7


def test_applying_twice_is_refused(app: FastAPI, web: TestClient) -> None:
    app.dependency_overrides[get_contact_source] = lambda: FakeSource()
    url = sync_now(web)
    ids = change_ids(web.get(url).text)
    web.post(f"{url}/apply", data=form(ids))
    assert "already applied" in web.post(f"{url}/apply", data=form(ids)).text


@pytest.fixture
def db_session(app: FastAPI):
    return app.state.sessionmaker


def test_missing_run_is_a_404(web: TestClient) -> None:
    assert web.get("/contacts/sync/999").status_code == 404


def test_write_back_preview_apply_and_restore(app: FastAPI, web: TestClient, db_session) -> None:
    from tests.test_contact_writeback import CARD, FakeServer

    server = FakeServer(u1=CARD)
    app.dependency_overrides[get_contact_source] = lambda: server
    with db_session() as db:
        person = people.create_person(
            db,
            people.PersonCreate(
                display_name="Patrik Veselý",
                first_name="Patrik",
                last_name="Veselý",
                birthday_month=3,
                birthday_day=17,
            ),
        )
        person.vcard_uid = "u1"
        db.commit()

    url = sync_now(web)
    page = web.get(url)
    assert "Into Apple contacts" in page.text and "written to the contact in iCloud" in page.text
    assert server.puts == []  # the preview wrote nothing
    ids = change_ids(page.text)
    assert "checked" in page.text  # the fill of an empty Apple field is pre-ticked

    done = web.post(f"{url}/apply", data=form(ids, ids))
    assert "Sync: 1 applied" in done.text and "Restore original" in done.text
    assert "BDAY" in server.cards["/card/u1.vcf"]

    restored = web.post("/contacts/sync/backups/1/restore", data={"run_id": url.rsplit("/", 1)[1]})
    assert "Restored Patrik Veselý" in restored.text
    assert server.cards["/card/u1.vcf"] == CARD
