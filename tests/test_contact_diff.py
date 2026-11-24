"""Three-way contact diff: every case in SPEC §2.13."""

from __future__ import annotations

from orbit.domain.enums import SyncDirection, SyncKind
from orbit.integrations.vcard import Birthday, LabeledValue, SocialProfile, VCard
from orbit.services import contact_diff as cd

PULL, PUSH = SyncDirection.PULL, SyncDirection.PUSH


def phone(norm: str) -> dict[str, object]:
    return {f"phone:{norm}": {"value": norm, "label": None}}


def kinds(changes: list[cd.FieldChange]) -> list[tuple[str, SyncDirection, SyncKind]]:
    return [(c.key, c.direction, c.kind) for c in changes]


def test_equal_is_silent_and_becomes_the_base() -> None:
    orbit = {"name": "Pato", "birthday": [3, 17, None]}
    apple = {"name": "pato", "birthday": [3, 17, None]}
    assert cd.diff({}, orbit, apple) == []
    assert cd.settle_equal({}, orbit, apple) == orbit


def test_apple_filled_empty_birthday_is_auto_pull() -> None:
    changes = cd.diff({}, {"name": "Pato"}, {"name": "Pato", "birthday": [3, 17, 1994]})
    assert kinds(changes) == [("birthday", PULL, SyncKind.AUTO)]
    assert changes[0].old is None and changes[0].new == [3, 17, 1994]


def test_apple_changed_value_is_a_proposal() -> None:
    base = {"name": "Pato", "birthday": [3, 17, None]}
    changes = cd.diff(base, base, {"name": "Pato", "birthday": [3, 18, None]})
    assert kinds(changes) == [("birthday", PULL, SyncKind.PROPOSAL)]


def test_orbit_changed_is_a_push() -> None:
    base = {"name": "Pato", "birthday": [3, 17, None]}
    changes = cd.diff(base, {**base, "birthday": [3, 18, None]}, base)
    assert kinds(changes) == [("birthday", PUSH, SyncKind.PROPOSAL)]


def test_orbit_filled_empty_apple_field_is_auto_push() -> None:
    changes = cd.diff({}, {"name": "Pato", "birthday": [3, 17, None]}, {"name": "Pato"})
    assert kinds(changes) == [("birthday", PUSH, SyncKind.AUTO)]


def test_both_changed_differently_is_a_conflict() -> None:
    base = {"name": "Pato", "birthday": [3, 17, None]}
    changes = cd.diff(
        base, {**base, "birthday": [3, 20, None]}, {**base, "birthday": [3, 18, None]}
    )
    assert kinds(changes) == [("birthday", PULL, SyncKind.CONFLICT)]
    assert changes[0].old == [3, 20, None] and changes[0].new == [3, 18, None]


def test_first_link_with_different_values_is_a_conflict() -> None:
    changes = cd.diff(
        {}, {"name": "Pato", "birthday": [3, 20, None]}, {"name": "Pato", "birthday": [3, 18, None]}
    )
    assert kinds(changes) == [("birthday", PULL, SyncKind.CONFLICT)]


def test_both_changed_to_the_same_value_is_silent() -> None:
    base = {"name": "Pato", "birthday": [3, 17, None]}
    both = {**base, "birthday": [3, 18, None]}
    assert cd.diff(base, both, both) == []
    assert cd.settle_equal(base, both, both)["birthday"] == [3, 18, None]


def test_name_is_never_auto() -> None:
    changes = cd.diff({"name": "Pato"}, {"name": "Pato"}, {"name": "Patrik"})
    assert kinds(changes) == [("name", PULL, SyncKind.PROPOSAL)]
    changes = cd.diff({"name": "Pato"}, {"name": "Patrik"}, {"name": "Pato"})
    assert kinds(changes) == [("name", PUSH, SyncKind.PROPOSAL)]


def test_new_phone_is_auto_only_when_the_target_has_none() -> None:
    changes = cd.diff({}, {"name": "A"}, {"name": "A", **phone("+421905123456")})
    assert kinds(changes) == [("phone:+421905123456", PULL, SyncKind.AUTO)]
    has_one = {"name": "A", **phone("+421900000000")}
    base = dict(has_one)
    changes = cd.diff(base, has_one, {**has_one, **phone("+421905123456")})
    assert kinds(changes) == [("phone:+421905123456", PULL, SyncKind.PROPOSAL)]


def test_phone_removed_on_apple_is_a_pull_removal() -> None:
    both = {"name": "A", **phone("+421905123456")}
    changes = cd.diff(both, both, {"name": "A"})
    assert kinds(changes) == [("phone:+421905123456", PULL, SyncKind.PROPOSAL)]
    assert changes[0].new is None


def test_phone_removed_in_orbit_is_a_push_removal() -> None:
    both = {"name": "A", **phone("+421905123456")}
    changes = cd.diff(both, {"name": "A"}, both)
    assert kinds(changes) == [("phone:+421905123456", PUSH, SyncKind.PROPOSAL)]
    assert changes[0].new is None


def test_each_set_element_is_diffed_on_its_own() -> None:
    base = {"name": "A", **phone("+421900000001"), **phone("+421900000002")}
    orbit = {"name": "A", **phone("+421900000001"), **phone("+421900000003")}  # swapped 2 -> 3
    apple = {"name": "A", **phone("+421900000002"), **phone("+421900000004")}  # swapped 1 -> 4
    got = {(c.key, c.direction) for c in cd.diff(base, orbit, apple)}
    assert got == {
        ("phone:+421900000001", PULL),  # Apple removed it
        ("phone:+421900000002", PUSH),  # Orbit removed it
        ("phone:+421900000003", PUSH),  # Orbit added it
        ("phone:+421900000004", PULL),  # Apple added it
    }


def test_orbit_only_data_is_never_cleared_or_pushed() -> None:
    base = {"name": "A", "city": "Košice", "alias:pato": {"value": "Pato"}}
    assert cd.diff(base, base, {"name": "A"}) == []  # Apple dropped city and nickname
    assert cd.diff({}, {"name": "A", "city": "Košice"}, {"name": "A"}) == []  # city is not pushed
    assert cd.diff({}, {"name": "A", "alias:pato": {"value": "Pato"}}, {"name": "A"}) == []


def test_iban_and_other_handles_are_not_pushed() -> None:
    other = {"social:other:foo": {"value": "foo", "label": "x"}}
    assert cd.diff({}, {"name": "A", **other}, {"name": "A"}) == []
    assert cd.diff({}, {"name": "A"}, {"name": "A", **other})[0].direction is PULL


def test_card_snapshot_normalizes_handles_and_birthday() -> None:
    card = VCard(
        uid="u",
        formatted_name="Patrik Veselý",
        given_name="Patrik",
        family_name="Veselý",
        phones=[LabeledValue("0905 123 456", "mobile")],
        emails=[LabeledValue("Patrik@Example.com")],
        socials=[SocialProfile("instagram", "@pato"), SocialProfile("mastodon", "p@x")],
        birthday=Birthday(3, 17),
        nicknames=["Paťo"],
        note="Met at Uni",
    )
    snap = cd.snapshot_card(card, "SK")
    assert snap["name"] == "Patrik Veselý"
    assert snap["birthday"] == [3, 17, None]
    assert "phone:+421905123456" in snap
    assert "email:patrik@example.com" in snap
    assert "social:instagram:pato" in snap
    assert "social:other:p@x" in snap
    assert "alias:pato" in snap
    assert any(k.startswith("note:") for k in snap)


def test_card_json_round_trip() -> None:
    card = VCard(
        uid="u",
        formatted_name="X",
        phones=[LabeledValue("+421905123456", "mobile")],
        socials=[SocialProfile("instagram", "x")],
        birthday=Birthday(1, 2, 1990),
    )
    assert cd.card_from_json(cd.card_to_json(card)) == card


def test_reject_hash_changes_with_the_value() -> None:
    a = cd.reject_hash("person:1", "birthday", PULL, [3, 17, None])
    assert a == cd.reject_hash("person:1", "birthday", PULL, [3, 17, None])
    assert a != cd.reject_hash("person:1", "birthday", PULL, [3, 18, None])
    assert a != cd.reject_hash("person:1", "birthday", PUSH, [3, 17, None])
