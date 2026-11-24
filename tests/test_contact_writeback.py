"""Writing to Apple Contacts: preview, ETag guard, backups, undo, restore."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.contacts import ContactBackup, ContactLink, SyncChange
from orbit.domain.enums import HandleType, SyncDirection, SyncKind, SyncSource, SyncState
from orbit.integrations.carddav import CardChangedError, CardDavClient, CardDavError, RawCard
from orbit.integrations.vcard import parse_vcards
from orbit.services import contact_sync as sync
from orbit.services import people
from orbit.services.errors import InvalidInputError

CARD = (
    "BEGIN:VCARD\nVERSION:3.0\nN:Veselý;Patrik;;;\nFN:Patrik Veselý\n"
    "X-CUSTOM-APP;x-foo=bar:keep me\nUID:u1\nEND:VCARD\n"
)


class FakeServer:
    """An address book that versions every write like a real ETag server."""

    def __init__(self, **cards: str) -> None:
        self.cards = {f"/card/{uid}.vcf": text for uid, text in cards.items()}
        self.versions = {href: 1 for href in self.cards}
        self.puts: list[str] = []
        self.fail_hrefs: set[str] = set()

    def etag(self, href: str) -> str:
        return f'"v{self.versions[href]}"'

    def fetch_cards(self) -> list[RawCard]:
        return [RawCard(t, h, self.etag(h)) for h, t in self.cards.items()]

    def get_card(self, href: str) -> RawCard:
        return RawCard(self.cards[href], href, self.etag(href))

    def put_card(self, href: str, text: str, etag: str | None) -> str | None:
        if href in self.fail_hrefs:
            raise CardDavError("PUT failed with HTTP 500.")
        if etag != self.etag(href):
            raise CardChangedError("The contact changed in Apple Contacts since it was read.")
        self.cards[href] = text
        self.versions[href] += 1
        self.puts.append(href)
        return self.etag(href)

    def edit_elsewhere(self, href: str, text: str) -> None:
        self.cards[href] = text
        self.versions[href] += 1


def start(db: Session, server: FakeServer):
    cards = sync.cards_from_raw(server.fetch_cards())
    return sync.start_run(db, cards, SyncSource.CARDDAV, include_push=True)


def linked_person(db: Session, **fields) -> people.Person:
    person = people.create_person(
        db,
        people.PersonCreate(
            display_name="Patrik Veselý", first_name="Patrik", last_name="Veselý", **fields
        ),
    )
    person.vcard_uid = "u1"
    return person


def ticked(run, kinds=None) -> dict[int, str]:
    return {c.id: "apply" for c in run.changes if kinds is None or c.kind in kinds}


def card_of(server: FakeServer, href: str = "/card/u1.vcf"):
    return parse_vcards(server.cards[href])[0]


def test_orbit_birthday_is_a_pre_ticked_push_and_is_written(db: Session) -> None:
    server = FakeServer(u1=CARD)
    person = linked_person(db, birthday_month=3, birthday_day=17, birthday_year=1994)
    run = start(db, server)
    (change,) = run.changes
    assert (change.direction, change.kind) == (SyncDirection.PUSH, SyncKind.AUTO)
    assert sync.default_choice(change) == "apply"
    assert server.puts == []  # the preview wrote nothing

    report = sync.apply_run(db, run.id, {change.id: "apply"}, writer=server)

    assert (report.applied, report.failed) == (1, [])
    assert card_of(server).birthday.year == 1994
    assert "X-CUSTOM-APP;x-foo=bar:keep me" in server.cards["/card/u1.vcf"]
    backup = db.scalars(select(ContactBackup)).one()
    assert backup.vcard_text == CARD and backup.person_id == person.id
    assert db.scalars(select(ContactLink)).one().etag == '"v2"'
    assert start(db, server).changes == []  # nothing left to do


def test_unticked_push_writes_nothing(db: Session) -> None:
    server = FakeServer(u1=CARD)
    linked_person(db, birthday_month=3, birthday_day=17)
    run = start(db, server)
    sync.apply_run(db, run.id, {}, writer=server)
    assert server.puts == [] and db.scalars(select(ContactBackup)).all() == []


def test_changed_in_apple_since_the_preview_is_not_overwritten(db: Session) -> None:
    server = FakeServer(u1=CARD)
    linked_person(db, birthday_month=3, birthday_day=17)
    run = start(db, server)
    server.edit_elsewhere("/card/u1.vcf", CARD.replace("END:VCARD", "NOTE:new\nEND:VCARD"))

    report = sync.apply_run(db, run.id, ticked(run), writer=server)

    assert report.applied == 0 and "changed in Apple Contacts" in report.failed[0]
    assert "NOTE:new" in server.cards["/card/u1.vcf"] and "BDAY" not in server.cards["/card/u1.vcf"]
    assert run.changes[0].state is SyncState.FAILED


def test_server_refusing_the_write_fails_only_that_contact(db: Session) -> None:
    other = (
        CARD.replace("u1", "u2")
        .replace("Patrik Veselý", "Eva Nováková")
        .replace("Veselý;Patrik", "Nováková;Eva")
    )
    server = FakeServer(u1=CARD, u2=other)
    linked_person(db, birthday_month=3, birthday_day=17)
    eva = people.create_person(
        db,
        people.PersonCreate(display_name="Eva Nováková", first_name="Eva", last_name="Nováková"),
    )
    eva.vcard_uid = "u2"
    people.update_person(db, eva, people.PersonUpdate(birthday_month=5, birthday_day=6))
    server.fail_hrefs.add("/card/u1.vcf")
    run = start(db, server)

    report = sync.apply_run(db, run.id, ticked(run), writer=server)

    assert report.applied == 1 and len(report.failed) == 1
    assert card_of(server, "/card/u2.vcf").birthday.month == 5


def test_conflict_keep_orbit_writes_orbit_value(db: Session) -> None:
    server = FakeServer(u1=CARD.replace("UID:u1", "BDAY:1994-03-18\nUID:u1"))
    person = linked_person(db, birthday_month=3, birthday_day=20)
    run = start(db, server)
    (change,) = run.changes
    assert change.kind is SyncKind.CONFLICT

    sync.apply_run(db, run.id, {change.id: "orbit"}, writer=server)

    assert card_of(server).birthday.day == 20 and person.birthday_day == 20
    assert start(db, server).changes == []


def test_name_change_is_an_unticked_proposal_and_pushes_when_chosen(db: Session) -> None:
    server = FakeServer(u1=CARD)
    person = linked_person(db)
    sync.apply_run(db, start(db, server).id, {}, writer=server)  # link, in sync
    people.update_person(db, person, people.PersonUpdate(display_name="Patrik V."))
    run = start(db, server)
    (change,) = [c for c in run.changes if c.field == "name"]
    assert (change.direction, change.kind) == (SyncDirection.PUSH, SyncKind.PROPOSAL)
    assert sync.default_choice(change) is None
    sync.apply_run(db, run.id, {change.id: "apply"}, writer=server)
    assert card_of(server).formatted_name == "Patrik V."


def test_phone_added_and_removed_in_orbit_reach_apple(db: Session) -> None:
    server = FakeServer(u1=CARD)
    person = linked_person(db)
    handle = people.add_handle(db, person, HandleType.PHONE, "+421905123456")
    run = start(db, server)
    sync.apply_run(db, run.id, ticked(run), writer=server)
    assert [p.value for p in card_of(server).phones] == ["+421905123456"]

    people.remove_handle(db, person, handle.id)
    run = start(db, server)
    (change,) = run.changes
    assert change.new_json is None and change.kind is SyncKind.PROPOSAL
    sync.apply_run(db, run.id, {change.id: "apply"}, writer=server)
    assert card_of(server).phones == []


def test_pushing_without_a_connection_fails_cleanly(db: Session) -> None:
    server = FakeServer(u1=CARD)
    linked_person(db, birthday_month=3, birthday_day=17)
    run = start(db, server)
    report = sync.apply_run(db, run.id, ticked(run), writer=None)
    assert report.failed == ["Patrik Veselý: Apple Contacts is not connected."]


def test_undo_push_puts_the_old_value_back(db: Session) -> None:
    server = FakeServer(u1=CARD)
    linked_person(db, birthday_month=3, birthday_day=17)
    run = start(db, server)
    sync.apply_run(db, run.id, ticked(run), writer=server)
    (change,) = run.changes

    sync.undo_change(db, change.id, writer=server)

    assert card_of(server).birthday is None and change.state is SyncState.UNDONE
    # Orbit still has the birthday, so the next sync proposes the push again.
    assert len(start(db, server).changes) == 1


def test_undo_push_refuses_when_apple_changed_since(db: Session) -> None:
    server = FakeServer(u1=CARD)
    linked_person(db, birthday_month=3, birthday_day=17)
    run = start(db, server)
    sync.apply_run(db, run.id, ticked(run), writer=server)
    server.edit_elsewhere("/card/u1.vcf", CARD.replace("END:VCARD", "BDAY:1999-01-02\nEND:VCARD"))
    with pytest.raises(InvalidInputError, match="changed in Apple Contacts since"):
        sync.undo_change(db, run.changes[0].id, writer=server)
    assert run.changes[0].state is SyncState.APPLIED


def test_restore_backup_puts_the_original_card_back(db: Session) -> None:
    server = FakeServer(u1=CARD)
    linked_person(db, birthday_month=3, birthday_day=17)
    run = start(db, server)
    sync.apply_run(db, run.id, ticked(run), writer=server)
    backup = sync.list_backups(db, run.id)[0]

    sync.restore_backup(db, backup.id, server)

    assert server.cards["/card/u1.vcf"] == CARD
    assert backup.restored_at is not None and run.changes[0].state is SyncState.UNDONE
    with pytest.raises(InvalidInputError, match="already restored"):
        sync.restore_backup(db, backup.id, server)


def test_restore_refuses_after_a_later_change(db: Session) -> None:
    server = FakeServer(u1=CARD)
    linked_person(db, birthday_month=3, birthday_day=17)
    run = start(db, server)
    sync.apply_run(db, run.id, ticked(run), writer=server)
    server.edit_elsewhere("/card/u1.vcf", CARD + "")
    with pytest.raises(InvalidInputError, match="not restored"):
        sync.restore_backup(db, sync.list_backups(db, run.id)[0].id, server)


def test_backups_older_than_90_days_are_pruned(db: Session) -> None:
    server = FakeServer(u1=CARD)
    person = linked_person(db, birthday_month=3, birthday_day=17)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    db.add(ContactBackup(person_id=person.id, href="/x", vcard_text="x", created_at=old))
    run = start(db, server)
    sync.apply_run(db, run.id, ticked(run), writer=server)
    assert sync.prune_backups(db, now=old + timedelta(days=91)) == 1
    assert len(db.scalars(select(ContactBackup)).all()) == 1  # the recent one stays
    assert db.scalars(select(SyncChange)).all()


def test_carddav_client_get_and_conditional_put() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=CARD, headers={"ETag": '"v1"'})
        if request.headers.get("If-Match") != '"v1"':
            return httpx.Response(412)
        return httpx.Response(204, headers={"ETag": '"v2"'})

    client = CardDavClient("https://c.example", httpx.Client(transport=httpx.MockTransport(handle)))
    card = client.get_card("https://c.example/card/u1.vcf")
    assert (card.text, card.etag) == (CARD, '"v1"')
    assert client.put_card("https://c.example/card/u1.vcf", CARD, '"v1"') == '"v2"'
    assert seen[-1].headers["Content-Type"].startswith("text/vcard")
    with pytest.raises(CardChangedError):
        client.put_card("https://c.example/card/u1.vcf", CARD, '"old"')
