from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbit.domain.enums import GoalHorizon, GoalStatus, PromiseDirection, PromiseStatus
from orbit.domain.goals import Goal
from orbit.domain.ledger import Promise
from orbit.domain.people import Person
from orbit.services import goals, people, promises
from orbit.services.errors import InvalidInputError, NotFoundError

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 5)


def goal(db: Session, title: str, **fields: object) -> Goal:
    return goals.create_goal(db, goals.GoalCreate(title=title, **fields), NOW)  # type: ignore[arg-type]


def ranks(db: Session) -> list[tuple[str, int | None]]:
    return [(g.title, g.rank) for g in goals.active_goals(db)]


def person(db: Session, name: str) -> Person:
    return people.create_person(db, people.PersonCreate(display_name=name))


def promise(db: Session, who: Person, text: str, **fields: object) -> Promise:
    data = promises.PromiseCreate(
        person_id=who.id,
        direction=PromiseDirection.I_PROMISED,
        text=text,
        **fields,  # type: ignore[arg-type]
    )
    return promises.create_promise(db, data, TODAY)


# --- Create, update ---


def test_create_ranks_goals_in_order(db: Session) -> None:
    a, b, c = goal(db, "Run a half marathon"), goal(db, "Save 5000"), goal(db, "Learn Rust")
    assert (a.rank, b.rank, c.rank) == (1, 2, 3)
    assert a.status is GoalStatus.ACTIVE
    assert a.reviewed_at == NOW  # a new goal counts as looked at


def test_create_with_all_fields_and_blank_fields_become_none(db: Session) -> None:
    g = goal(
        db,
        "Run",
        why="  my back hurts ",
        horizon=GoalHorizon.YEAR,
        target="under 2:00",
        target_date=date(2027, 4, 1),
    )
    assert (g.why, g.horizon, g.target, g.target_date) == (
        "my back hurts",
        GoalHorizon.YEAR,
        "under 2:00",
        date(2027, 4, 1),
    )
    blank = goal(db, "Other", why="   ", target="")
    assert (blank.why, blank.target, blank.horizon) == (None, None, None)


def test_title_is_required() -> None:
    with pytest.raises(ValueError):
        goals.GoalCreate(title="   ")


def test_titles_are_unique_among_active_goals_ignoring_diacritics(db: Session) -> None:
    goal(db, "Naučiť sa Rust")
    with pytest.raises(InvalidInputError, match="already have an active goal"):
        goal(db, "nauciť sa  rust")


def test_a_paused_goal_does_not_block_its_title_but_reactivating_checks(db: Session) -> None:
    first = goal(db, "Run")
    goals.set_status(db, first, GoalStatus.PAUSED, NOW)
    second = goal(db, "Run")  # allowed: the first is paused
    with pytest.raises(InvalidInputError, match="already have an active goal"):
        goals.set_status(db, first, GoalStatus.ACTIVE, NOW)
    goals.set_status(db, second, GoalStatus.DONE, NOW)
    goals.set_status(db, first, GoalStatus.ACTIVE, NOW)
    assert ranks(db) == [("Run", 1)]


def test_update_fields_and_title_uniqueness(db: Session) -> None:
    a, b = goal(db, "Run"), goal(db, "Read")
    goals.update_goal(
        db,
        a,
        goals.GoalUpdate(why="energy", horizon=GoalHorizon.MONTH, target_date=date(2026, 12, 1)),
    )
    assert (a.why, a.horizon, a.target_date) == ("energy", GoalHorizon.MONTH, date(2026, 12, 1))
    goals.update_goal(db, a, goals.GoalUpdate(why=None, target_date=None))
    assert (a.why, a.target_date) == (None, None)
    assert a.horizon is GoalHorizon.MONTH  # not mentioned: untouched
    goals.update_goal(db, a, goals.GoalUpdate(title="Run more"))
    assert a.title_norm == "run more"
    with pytest.raises(InvalidInputError, match="already have an active goal"):
        goals.update_goal(db, b, goals.GoalUpdate(title="run more"))
    with pytest.raises(InvalidInputError, match="needs a title"):
        goals.update_goal(db, b, goals.GoalUpdate(title=None))
    goals.update_goal(db, a, goals.GoalUpdate(title="Run more"))  # its own title is fine


def test_get_goal_missing(db: Session) -> None:
    with pytest.raises(NotFoundError):
        goals.get_goal(db, 999)


# --- Status and rank ---


def test_leaving_active_closes_the_gap(db: Session) -> None:
    a, b, c = goal(db, "A"), goal(db, "B"), goal(db, "C")
    goals.set_status(db, b, GoalStatus.PAUSED, NOW)
    assert (b.rank, b.closed_at) == (None, None)
    assert ranks(db) == [("A", 1), ("C", 2)]
    goals.set_status(db, a, GoalStatus.DONE, NOW)
    assert (a.rank, a.closed_at) == (None, NOW)
    assert ranks(db) == [("C", 1)]
    goals.set_status(db, c, GoalStatus.DROPPED, NOW)
    assert c.closed_at == NOW
    assert ranks(db) == []


def test_reactivating_puts_the_goal_last_and_clears_closed(db: Session) -> None:
    a, b = goal(db, "A"), goal(db, "B")
    goals.set_status(db, a, GoalStatus.DONE, NOW)
    goals.set_status(db, a, GoalStatus.ACTIVE, NOW)
    assert ranks(db) == [("B", 1), ("A", 2)]
    assert a.closed_at is None
    assert b.rank == 1


def test_every_status_transition_keeps_the_invariants(db: Session) -> None:
    a = goal(db, "A")
    goal(db, "B")
    for status in (
        GoalStatus.PAUSED,
        GoalStatus.DONE,
        GoalStatus.DROPPED,
        GoalStatus.ACTIVE,
        GoalStatus.DONE,
        GoalStatus.PAUSED,
        GoalStatus.ACTIVE,
    ):
        goals.set_status(db, a, status, NOW)
        assert (a.rank is not None) == (a.status is GoalStatus.ACTIVE)
        assert (a.closed_at is not None) == (a.status in (GoalStatus.DONE, GoalStatus.DROPPED))
        active = [g.rank for g in goals.active_goals(db)]
        assert active == list(range(1, len(active) + 1))  # contiguous, no ties


def test_the_database_refuses_a_rank_on_an_inactive_goal(db: Session) -> None:
    g = goal(db, "A")
    g.status = GoalStatus.PAUSED  # rank still set
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_rank_goals_sets_the_whole_order(db: Session) -> None:
    a, b, c = goal(db, "A"), goal(db, "B"), goal(db, "C")
    later = NOW + timedelta(days=1)
    goals.rank_goals(db, [c.id, a.id, b.id], later)
    assert ranks(db) == [("C", 1), ("A", 2), ("B", 3)]
    assert c.reviewed_at == later  # moved
    assert b.reviewed_at == later  # moved (2 -> 3)
    assert a.reviewed_at == later  # moved (1 -> 2)


def test_rank_goals_leaves_unmoved_goals_alone(db: Session) -> None:
    a, b = goal(db, "A"), goal(db, "B")
    later = NOW + timedelta(days=1)
    goals.rank_goals(db, [a.id, b.id], later)
    assert a.reviewed_at == NOW


@pytest.mark.parametrize("ids", [[], [1], [1, 1, 2], [1, 2, 3], [2, 1, 99]])
def test_rank_goals_needs_every_active_goal_once(db: Session, ids: list[int]) -> None:
    goal(db, "A")
    goal(db, "B")
    paused = goal(db, "P")
    goals.set_status(db, paused, GoalStatus.PAUSED, NOW)
    with pytest.raises(InvalidInputError, match="every active goal exactly once"):
        goals.rank_goals(db, ids)


def test_move(db: Session) -> None:
    a, b, c = goal(db, "A"), goal(db, "B"), goal(db, "C")
    goals.move(db, c, "up", NOW)
    assert ranks(db) == [("A", 1), ("C", 2), ("B", 3)]
    goals.move(db, a, "down", NOW)
    assert ranks(db) == [("C", 1), ("A", 2), ("B", 3)]
    goals.move(db, b, "top", NOW)
    assert ranks(db) == [("B", 1), ("C", 2), ("A", 3)]
    goals.move(db, b, "up", NOW)  # already first
    goals.move(db, a, "down", NOW)  # already last
    assert ranks(db) == [("B", 1), ("C", 2), ("A", 3)]
    assert b.reviewed_at == NOW


def test_moving_counts_as_reviewing(db: Session) -> None:
    a, b = goal(db, "A"), goal(db, "B")
    later = NOW + timedelta(days=9)
    goals.move(db, b, "up", later)
    assert b.reviewed_at == later
    assert a.reviewed_at == later  # it moved too


def test_only_active_goals_move(db: Session) -> None:
    a = goal(db, "A")
    goals.set_status(db, a, GoalStatus.PAUSED, NOW)
    with pytest.raises(InvalidInputError, match="Only active goals"):
        goals.move(db, a, "up")


def test_list_goals_and_inactive_order(db: Session) -> None:
    a, b, c, d = goal(db, "A"), goal(db, "B"), goal(db, "C"), goal(db, "D")
    goals.set_status(db, a, GoalStatus.DROPPED, NOW)
    goals.set_status(db, b, GoalStatus.PAUSED, NOW)
    goals.set_status(db, c, GoalStatus.DONE, NOW)
    assert [g.title for g in goals.list_goals(db)] == ["D"]
    assert [g.title for g in goals.list_goals(db, GoalStatus.DONE)] == ["C"]
    assert [g.title for g in goals.inactive_goals(db)] == ["B", "C", "A"]  # paused, done, dropped
    assert d.rank == 1


# --- Review nudge ---


def test_review_nudge(db: Session) -> None:
    assert goals.review_overdue_days(db, NOW) is None  # no goals
    a = goal(db, "A")
    assert goals.review_overdue_days(db, NOW + timedelta(days=6)) is None
    assert goals.review_overdue_days(db, NOW + timedelta(days=7)) == 7
    assert goals.review_overdue_days(db, NOW + timedelta(days=9)) == 9
    goals.review(db, a, NOW + timedelta(days=8))
    assert goals.review_overdue_days(db, NOW + timedelta(days=9)) is None


def test_the_freshest_review_decides_the_nudge(db: Session) -> None:
    a, b = goal(db, "A"), goal(db, "B")
    goals.review(db, b, NOW + timedelta(days=5))
    assert goals.review_overdue_days(db, NOW + timedelta(days=9)) is None
    assert goals.days_since_review(a, NOW + timedelta(days=9)) == 9
    assert goals.days_since_review(a, NOW - timedelta(days=1)) == 0  # never negative


def test_paused_goals_do_not_count_for_the_nudge(db: Session) -> None:
    a = goal(db, "A")
    goals.set_status(db, a, GoalStatus.PAUSED, NOW)
    assert goals.review_overdue_days(db, NOW + timedelta(days=30)) is None


# --- Links ---


def test_people_links(db: Session) -> None:
    g = goal(db, "Get closer to Peter")
    peter, terka = person(db, "Peter"), person(db, "Terka")
    goals.add_person(db, g, peter.id)
    goals.add_person(db, g, peter.id)  # twice: still once
    goals.add_person(db, g, terka.id)
    assert [p.display_name for p in g.people] == ["Peter", "Terka"]
    assert [x.title for x in goals.goals_of_person(db, peter.id)] == ["Get closer to Peter"]
    assert [p.display_name for p in goals.people_for_picker(db, g)] == []
    goals.remove_person(db, g, terka.id)
    assert [p.display_name for p in g.people] == ["Peter"]
    assert [p.display_name for p in goals.people_for_picker(db, g)] == ["Terka"]
    with pytest.raises(NotFoundError):
        goals.add_person(db, g, 999)


def test_goals_of_person_skips_inactive_goals(db: Session) -> None:
    g = goal(db, "A")
    peter = person(db, "Peter")
    goals.add_person(db, g, peter.id)
    goals.set_status(db, g, GoalStatus.PAUSED, NOW)
    assert goals.goals_of_person(db, peter.id) == []


def test_promise_links(db: Session) -> None:
    g, other = goal(db, "A"), goal(db, "B")
    pato = person(db, "Pato")
    p1, p2 = promise(db, pato, "send the plan"), promise(db, pato, "call him")
    goals.attach_promise(db, g, p1.id)
    goals.attach_promise(db, other, p2.id)
    goals.attach_promise(db, g, p2.id)  # moves it: a promise serves at most one goal
    assert [p.text for p in g.promises] == ["send the plan", "call him"]
    db.refresh(other)
    assert other.promises == []
    assert p1.goal.title == "A"
    goals.detach_promise(db, g, p1.id)
    assert p1.goal_id is None
    with pytest.raises(NotFoundError):
        goals.detach_promise(db, g, p1.id)  # not linked any more
    with pytest.raises(NotFoundError):
        goals.attach_promise(db, g, 999)


def test_open_promises_come_soonest_due_first(db: Session) -> None:
    g = goal(db, "A")
    pato = person(db, "Pato")
    late = promise(db, pato, "late", due=date(2026, 11, 1), goal_id=g.id)
    soon = promise(db, pato, "soon", due=date(2026, 10, 8), goal_id=g.id)
    done = promise(db, pato, "done", goal_id=g.id)
    promises.set_status(done, PromiseStatus.DONE, NOW)
    db.refresh(g)
    assert [p.id for p in goals.open_promises(g)] == [soon.id, late.id]


def test_promise_create_and_update_with_a_goal(db: Session) -> None:
    g = goal(db, "A")
    pato = person(db, "Pato")
    p = promise(db, pato, "x", goal_id=g.id)
    assert p.goal_id == g.id
    promises.update_promise(db, p, promises.PromiseUpdate(text="y"))
    assert p.goal_id == g.id  # not mentioned: untouched
    promises.update_promise(db, p, promises.PromiseUpdate(goal_id=None))
    assert p.goal_id is None
    promises.update_promise(db, p, promises.PromiseUpdate(goal_id=g.id))
    assert p.goal_id == g.id
    with pytest.raises(NotFoundError):
        promises.update_promise(db, p, promises.PromiseUpdate(goal_id=999))
    with pytest.raises(NotFoundError):
        promise(db, pato, "z", goal_id=999)


def test_a_promise_can_serve_a_paused_goal_but_not_a_missing_one(db: Session) -> None:
    g = goal(db, "A")
    goals.set_status(db, g, GoalStatus.PAUSED, NOW)
    p = promise(db, person(db, "Pato"), "x", goal_id=g.id)
    assert p.goal_id == g.id


def test_delete_goal_keeps_promises_and_people_and_closes_the_gap(db: Session) -> None:
    a, b, c = goal(db, "A"), goal(db, "B"), goal(db, "C")
    pato = person(db, "Pato")
    p = promise(db, pato, "x", goal_id=b.id)
    goals.add_person(db, b, pato.id)
    goals.delete_goal(db, b)
    assert ranks(db) == [("A", 1), ("C", 2)]
    db.refresh(p)
    assert p.goal_id is None
    assert db.get(Person, pato.id) is pato
    assert db.scalars(select(Goal).where(Goal.id == b.id)).first() is None
    assert (a.rank, c.rank) == (1, 2)
