"""Contact sync review: building a run, applying chosen changes, undo, rejection memory."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.contacts import ContactLink, SyncChange
from orbit.domain.enums import HandleType, SyncKind, SyncRunStatus, SyncSource, SyncState
from orbit.domain.people import Person
from orbit.integrations.vcard import Birthday, LabeledValue, VCard
from orbit.services import contact_sync as sync
from orbit.services import people
from orbit.services.errors import InvalidInputError


def card(uid: str = "u1", name: str = "Patrik Veselý", **kwargs) -> sync.CardInput:
    return sync.CardInput(
        VCard(uid=uid, formatted_name=name, **kwargs),
        href=f"/card/{uid}.vcf",
        etag='"e1"',
    )


def run_for(db: Session, *cards: sync.CardInput, source: SyncSource = SyncSource.CARDDAV):
    return sync.start_run(db, list(cards), source)


def changes_of(run, kind: SyncKind | None = None) -> list[SyncChange]:
    return [c for c in run.changes if kind is None or c.kind is kind]


def make_person(db: Session, name: str = "Patrik Veselý", **fields) -> Person:
    person = people.create_person(db, people.PersonCreate(display_name=name, **fields))
    return person


def tick_auto(run) -> dict[int, str]:
    return {c.id: "apply" for c in run.changes if sync.default_choice(c)}


def test_unknown_card_is_a_new_contact_and_applying_creates_and_links(db: Session) -> None:
    run = run_for(db, card(phones=[LabeledValue("+421 905 123 456")], birthday=Birthday(3, 17)))
    (change,) = run.changes
    assert change.kind is SyncKind.NEW_CONTACT and change.person_id is None
    assert sync.default_choice(change) is None  # new contacts are never pre-ticked

    report = sync.apply_run(db, run.id, {change.id: "apply"})

    assert (report.applied, report.skipped) == (1, 0)
    person = db.scalars(select(Person)).one()
    assert person.display_name == "Patrik Veselý" and person.birthday_month == 3
    link = db.scalars(select(ContactLink)).one()
    assert (link.person_id, link.carddav_uid, link.href) == (person.id, "u1", "/card/u1.vcf")
    assert run.status is SyncRunStatus.APPLIED
    # Nothing left to do: Apple and Orbit now agree.
    assert (
        run_for(
            db, card(phones=[LabeledValue("+421 905 123 456")], birthday=Birthday(3, 17))
        ).changes
        == []
    )


def test_filling_an_empty_orbit_field_is_auto_and_pre_ticked(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    run = run_for(db, card(birthday=Birthday(3, 17, 1994)))
    (change,) = run.changes
    assert (change.field, change.kind) == ("birthday", SyncKind.AUTO)
    assert sync.default_choice(change) == "apply"

    sync.apply_run(db, run.id, tick_auto(run))

    assert (person.birthday_month, person.birthday_day, person.birthday_year) == (3, 17, 1994)
    assert run_for(db, card(birthday=Birthday(3, 17, 1994))).changes == []


def test_a_changed_value_is_a_proposal_and_stays_unticked(db: Session) -> None:
    person = make_person(db, birthday_month=3, birthday_day=17)
    person.vcard_uid = "u1"
    sync.apply_run(db, run_for(db, card(birthday=Birthday(3, 17))).id, {})  # first link, in sync
    run = run_for(db, card(birthday=Birthday(3, 18)))
    (change,) = run.changes
    assert (change.kind, change.old_json, change.new_json) == (
        SyncKind.PROPOSAL,
        [3, 17, None],
        [3, 18, None],
    )
    assert sync.default_choice(change) is None


def test_unticked_changes_are_remembered_until_forgotten(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    first = run_for(db, card(birthday=Birthday(3, 17)))
    report = sync.apply_run(db, first.id, {})  # I left it unticked
    assert (report.applied, report.skipped) == (0, 1)
    assert person.birthday_month is None

    assert run_for(db, card(birthday=Birthday(3, 17))).changes == []  # not proposed again
    changed = run_for(db, card(birthday=Birthday(3, 19)))  # a new value is a new change
    assert [c.new_json for c in changed.changes] == [[3, 19, None]]

    assert sync.forget_rejections(db) == 1
    assert len(run_for(db, card(birthday=Birthday(3, 17))).changes) == 1


def test_take_one_field_and_skip_another_for_the_same_person(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    run = run_for(db, card(birthday=Birthday(3, 17), phones=[LabeledValue("+421905123456")]))
    by_field = {c.field: c for c in run.changes}
    sync.apply_run(db, run.id, {by_field["birthday"].id: "apply"})
    assert person.birthday_month == 3 and person.handles == []
    assert by_field["phone:+421905123456"].state is SyncState.REJECTED


def test_new_phone_on_a_person_who_has_one_is_a_proposal(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    people.add_handle(db, person, HandleType.PHONE, "+421900000000")
    sync.apply_run(db, run_for(db, card(phones=[LabeledValue("+421900000000")])).id, {})
    run = run_for(db, card(phones=[LabeledValue("+421900000000"), LabeledValue("+421905123456")]))
    (change,) = run.changes
    assert change.kind is SyncKind.PROPOSAL
    sync.apply_run(db, run.id, {change.id: "apply"})
    assert {h.value_norm for h in person.handles} == {"+421900000000", "+421905123456"}


def test_phone_removed_on_apple_removes_it_in_orbit_when_ticked(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    both = card(phones=[LabeledValue("+421905123456")])
    first = run_for(db, both)
    sync.apply_run(db, first.id, tick_auto(first))
    assert len(person.handles) == 1
    run = run_for(db, card())
    (change,) = run.changes
    assert change.new_json is None
    sync.apply_run(db, run.id, {change.id: "apply"})
    assert person.handles == []


def test_conflict_shows_both_values_and_apple_choice_wins(db: Session) -> None:
    person = make_person(db, birthday_month=3, birthday_day=20)
    person.vcard_uid = "u1"
    run = run_for(db, card(birthday=Birthday(3, 18)))
    (change,) = run.changes
    assert change.kind is SyncKind.CONFLICT
    assert (change.old_json, change.new_json) == ([3, 20, None], [3, 18, None])
    sync.apply_run(db, run.id, {change.id: "apple"})
    assert person.birthday_day == 18


def test_conflict_with_orbit_choice_needs_write_back(db: Session) -> None:
    person = make_person(db, birthday_month=3, birthday_day=20)
    person.vcard_uid = "u1"
    run = run_for(db, card(birthday=Birthday(3, 18)))
    (change,) = run.changes
    report = sync.apply_run(db, run.id, {change.id: "orbit"})
    assert report.failed and change.state is SyncState.FAILED
    assert person.birthday_day == 20


def test_name_change_from_apple_is_a_proposal_never_auto(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    sync.apply_run(db, run_for(db, card()).id, {})
    run = run_for(db, card(name="Patrik Novotný"))
    (change,) = [c for c in run.changes if c.field == "name"]
    assert change.kind is SyncKind.PROPOSAL


def test_undo_restores_value_and_base(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    run = run_for(db, card(birthday=Birthday(3, 17)))
    sync.apply_run(db, run.id, tick_auto(run))
    (change,) = run.changes

    sync.undo_change(db, change.id)

    assert person.birthday_month is None and change.state is SyncState.UNDONE
    assert "birthday" not in db.scalars(select(ContactLink)).one().base_json
    with pytest.raises(InvalidInputError):
        sync.undo_change(db, change.id)  # only an applied change


def test_undo_refuses_when_orbit_changed_since(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    run = run_for(db, card(birthday=Birthday(3, 17)))
    sync.apply_run(db, run.id, tick_auto(run))
    people.update_person(db, person, people.PersonUpdate(birthday_month=5, birthday_day=5))
    with pytest.raises(InvalidInputError, match="changed since"):
        sync.undo_change(db, run.changes[0].id)
    assert person.birthday_month == 5


def test_undo_of_a_new_contact_deletes_it_again(db: Session) -> None:
    run = run_for(db, card())
    sync.apply_run(db, run.id, {run.changes[0].id: "apply"})
    sync.undo_change(db, run.changes[0].id)
    assert db.scalars(select(Person)).all() == [] and db.scalars(select(ContactLink)).all() == []


def test_contact_gone_upstream_can_only_be_archived(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    sync.apply_run(db, run_for(db, card()).id, {})
    run = run_for(db, card(uid="u2", name="Someone Else"))  # u1 vanished from the server
    gone = changes_of(run, SyncKind.REMOVED_UPSTREAM)
    assert [c.person_id for c in gone] == [person.id]
    sync.apply_run(db, run.id, {gone[0].id: "apply"})
    assert person.archived is True
    sync.undo_change(db, gone[0].id)
    assert person.archived is False


def test_vcf_source_never_proposes_archiving(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    sync.apply_run(db, run_for(db, card()).id, {})
    run = run_for(db, card(uid="u2", name="Someone Else"), source=SyncSource.VCF)
    assert changes_of(run, SyncKind.REMOVED_UPSTREAM) == []


def test_similar_name_is_a_possible_match_to_link_or_create(db: Session) -> None:
    person = make_person(db, "Patrick Vesely")
    run = run_for(db, card(name="Patrik Veselý"))
    (change,) = run.changes
    assert change.kind is SyncKind.POSSIBLE_MATCH and change.person_id == person.id
    sync.apply_run(db, run.id, {change.id: "link"})
    assert person.vcard_uid == "u1" and db.scalars(select(ContactLink)).one().person_id == person.id
    assert len(db.scalars(select(Person)).all()) == 1


def test_possible_match_can_be_created_as_new(db: Session) -> None:
    make_person(db, "Patrick Vesely")
    run = run_for(db, card(name="Patrik Veselý"))
    sync.apply_run(db, run.id, {run.changes[0].id: "new"})
    assert len(db.scalars(select(Person)).all()) == 2


def test_a_review_run_is_replaced_by_the_next_sync(db: Session) -> None:
    run_for(db, card())
    second = run_for(db, card())
    assert db.scalars(select(type(second))).all() == [second]
    assert db.scalars(select(SyncChange)).all() == second.changes
    assert second.status is SyncRunStatus.REVIEW


def test_an_applied_run_cannot_be_applied_again(db: Session) -> None:
    run = run_for(db, card())
    sync.apply_run(db, run.id, {})
    with pytest.raises(InvalidInputError, match="already applied"):
        sync.apply_run(db, run.id, {})


def test_cards_without_a_name_are_skipped_and_counted(db: Session) -> None:
    run = run_for(db, sync.CardInput(VCard(uid="x")))
    assert run.changes == [] and run.summary_json["skipped"] == 1


def test_a_failing_change_does_not_stop_the_others(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    other = make_person(db, "Other Person")
    people.add_handle(db, other, HandleType.PHONE, "+421905123456")
    run = run_for(db, card(birthday=Birthday(3, 17), phones=[LabeledValue("+421905123456")]))
    # The phone belongs to someone else: adding it fails, the birthday still goes through.
    report = sync.apply_run(db, run.id, {c.id: "apply" for c in run.changes})
    assert person.birthday_month == 3
    assert report.applied >= 1


def test_a_half_done_review_keeps_what_was_decided_when_the_next_sync_starts(db: Session) -> None:
    person = make_person(db)
    person.vcard_uid = "u1"
    other = card(uid="u2", name="Someone Else")
    first = run_for(db, card(birthday=Birthday(3, 17)), other)
    mine = [c for c in first.changes if c.person_id == person.id]
    sync.apply_run(db, first.id, {mine[0].id: "apply"}, seen={mine[0].id})  # never looked at u2
    assert first.status is SyncRunStatus.REVIEW

    second = run_for(db, card(birthday=Birthday(3, 17)), other)

    assert [c.kind for c in second.changes] == [SyncKind.NEW_CONTACT]  # u2 is proposed again
    assert first.status is SyncRunStatus.APPLIED  # closed, not deleted
    sync.undo_change(db, mine[0].id)  # and still undoable
    assert person.birthday_month is None


def test_a_review_with_nothing_decided_is_dropped_by_the_next_sync(db: Session) -> None:
    first = run_for(db, card())
    first_id = first.id
    second = run_for(db, card())
    assert [r.id for r in db.scalars(select(type(second))).all()] == [second.id]
    assert second.id == first_id or db.get(type(second), first_id) is None
