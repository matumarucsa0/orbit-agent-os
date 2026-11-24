"""Randomized convergence check: applying every proposed change leaves nothing to propose."""

from __future__ import annotations

import random
from typing import Any

from orbit.domain.enums import SyncDirection
from orbit.services import contact_diff as cd

KEYS = [
    "name",
    "first_name",
    "city",
    "birthday",
    "phone:+1",
    "phone:+2",
    "email:a@x",
    "social:instagram:p",
    "social:other:o",
    "alias:pato",
    "note:abc",
]
SCALAR_VALUES = {
    "name": ["Pato", "Patrik"],
    "first_name": ["Pato", "Patrik"],
    "city": ["Košice", "Brno"],
    "birthday": [[3, 17, None], [3, 18, None], [3, 17, 1994]],
}


def value(key: str, rng: random.Random) -> Any:
    if key in SCALAR_VALUES:
        return rng.choice(SCALAR_VALUES[key])
    return {"value": key, "label": None}


def snapshot(rng: random.Random, universe: list[str]) -> dict[str, Any]:
    snap = {k: value(k, rng) for k in universe if rng.random() < 0.5}
    snap.setdefault("name", "Pato")  # both sides always have a display name
    return snap


def apply_all(orbit: dict[str, Any], apple: dict[str, Any], changes: list[cd.FieldChange]) -> None:
    for ch in changes:
        # A conflict is resolved for Apple; a push lands on Apple; a pull lands in Orbit.
        target = apple if ch.direction is SyncDirection.PUSH else orbit
        if ch.new is None:
            target.pop(ch.key, None)
        else:
            target[ch.key] = ch.new


def test_applying_everything_converges_and_never_loses_orbit_only_data() -> None:
    rng = random.Random(20261002)
    for case in range(5000):
        base = snapshot(rng, KEYS)
        orbit, apple = snapshot(rng, KEYS), snapshot(rng, KEYS)
        if rng.random() < 0.4:  # a plausible history: one side still equals the base
            orbit = dict(base) if rng.random() < 0.5 else orbit
            apple = dict(base) if orbit is not base and rng.random() < 0.5 else apple
        before_orbit = dict(orbit)
        changes = cd.diff(base, orbit, apple)
        apply_all(orbit, apple, changes)
        new_base = cd.settle_equal(base, orbit, apple)
        for ch in changes:  # an applied change is agreed from now on
            if ch.new is None:
                new_base.pop(ch.key, None)
            else:
                new_base[ch.key] = ch.new
        # Both sides must now agree, except where the design deliberately leaves them apart.
        residual = [
            k
            for k in {*orbit, *apple}
            if not cd.same(k, orbit.get(k), apple.get(k)) and not _left_apart(k, base, orbit, apple)
        ]
        assert residual == [], (case, base, before_orbit, apple, changes, residual)
        # Nothing is proposed twice.
        assert cd.diff(new_base, orbit, apple) == [] or all(
            _left_apart(c.key, base, orbit, apple) for c in cd.diff(new_base, orbit, apple)
        )
        # Orbit-only data is never deleted by a pull.
        for key in before_orbit:
            if cd.family(key) in ("alias", "note", "city", "first_name", "last_name"):
                assert key in orbit, (case, key)


def _left_apart(key: str, base: dict, orbit: dict, apple: dict) -> bool:
    """Differences the design leaves alone: Orbit-only fields and handle types Apple can't hold."""
    in_orbit, in_apple = key in orbit, key in apple
    never_cleared = cd.family(key) in ("alias", "note", "city", "first_name", "last_name")
    if in_orbit and not in_apple:
        return not cd.is_pushable(key)  # Orbit has it, Apple has no place for it
    if in_apple and not in_orbit and key in base:
        return not cd.is_pushable(key)  # I removed it in Orbit; that can't be written to Apple
    if (
        in_orbit
        and in_apple
        and not cd.is_pushable(key)
        and cd.same(key, apple[key], base.get(key))
    ):
        return True  # I edited it in Orbit; a non-pushable field never reaches Apple
    return never_cleared and not in_apple
