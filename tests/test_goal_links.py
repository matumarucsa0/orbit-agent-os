"""Goals meeting the rest of Orbit: capture, merge and undo, the digest, the context."""

from __future__ import annotations

from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import GoalStatus, PromiseDirection
from orbit.domain.goals import Goal, goal_person
from orbit.domain.ledger import Promise
from orbit.domain.people import MergeRecord
from orbit.integrations.claude import (
    CandidateGoal,
    CandidatePerson,
    ExtractedPromise,
    build_capture_prompt,
)
from orbit.services import capture, duplicates, goals, inbox, people, reminders
from orbit.services.context import (
    ContextFocus,
    ContextWindow,
    GoalsProvider,
    build_context,
)
from orbit.services.errors import InvalidInputError
from tests.test_capture import FakeExtractor, extraction, person
from tests.test_claude import request
from tests.test_goals import NOW, goal
from tests.test_web_inbox import proposal_ids
from tests.test_web_ledger import post, text
from tests.test_web_people import add

TODAY = date(2026, 10, 5)
PRAGUE = ZoneInfo("Europe/Prague")
WINDOW = ContextWindow(date(2026, 10, 5), date(2026, 10, 11), PRAGUE)


def promise_answer(pato_id: int, goal_ref: str | None) -> FakeExtractor:
    return FakeExtractor(
        extraction(
            promises=[
                ExtractedPromise(
                    person_ref=str(pato_id),
                    direction=PromiseDirection.I_PROMISED,
                    text="send the training plan",
                    due=None,
                    goal_ref=goal_ref,
                )
            ]
        )
    )


def run_capture(db: Session, extractor: FakeExtractor) -> Promise:
    result = capture.capture(
        db, extractor, "poslem Paťovi plán", today=TODAY, default_currency="EUR"
    )
    (proposal,) = result.proposals
    inbox.confirm(db, proposal, today=TODAY, default_currency="EUR")
    return db.scalars(select(Promise)).one()


# --- The prompt ---


def test_prompt_lists_goals_with_rank_and_why() -> None:
    prompt = build_capture_prompt(
        request(
            goals=(CandidateGoal(3, 1, "Get fit", "my back hurts"), CandidateGoal(4, 2, "Save"))
        )
    )
    assert "active goals, most important first" in prompt
    assert "- 3: 1. Get fit; my back hurts" in prompt
    assert "- 4: 2. Save" in prompt


def test_prompt_without_goals_has_no_goal_section() -> None:
    assert "goals" not in build_capture_prompt(request())


def test_prompt_masks_goal_text() -> None:
    prompt = build_capture_prompt(
        request(goals=(CandidateGoal(1, 1, "Pay SK3112000000198742637541", None),))
    )
    assert "SK3112000000198742637541" not in prompt


def test_candidates_still_listed_next_to_goals() -> None:
    prompt = build_capture_prompt(
        request(
            candidates=(CandidatePerson(7, "Patrik Veselý"),),
            goals=(CandidateGoal(1, 1, "Get fit"),),
        )
    )
    assert "- 7: Patrik Veselý" in prompt and "- 1: 1. Get fit" in prompt


# --- Capture ---


def test_capture_shows_claude_the_active_goals_in_order(db: Session) -> None:
    pato = person(db, "Patrik Veselý", "Paťo")
    fit = goal(db, "Get fit", why="my back hurts")
    save = goal(db, "Save 5000")
    paused = goal(db, "Old")
    goals.set_status(db, paused, GoalStatus.PAUSED, NOW)
    extractor = promise_answer(pato.id, None)
    run_capture(db, extractor)
    (seen,) = extractor.requests
    assert [(g.id, g.rank, g.title, g.why) for g in seen.goals] == [
        (fit.id, 1, "Get fit", "my back hurts"),
        (save.id, 2, "Save 5000", None),
    ]


def test_a_promise_serves_the_goal_claude_picked(db: Session) -> None:
    pato = person(db, "Patrik Veselý", "Paťo")
    fit = goal(db, "Get fit")
    created = run_capture(db, promise_answer(pato.id, str(fit.id)))
    assert created.goal_id == fit.id


@pytest.mark.parametrize("bad", [None, "", "abc", "999", "new:0", " "])
def test_a_goal_claude_made_up_is_dropped(db: Session, bad: str | None) -> None:
    pato = person(db, "Patrik Veselý", "Paťo")
    goal(db, "Get fit")
    assert run_capture(db, promise_answer(pato.id, bad)).goal_id is None


def test_a_paused_goal_cannot_be_picked_by_claude(db: Session) -> None:
    pato = person(db, "Patrik Veselý", "Paťo")
    old = goal(db, "Old")
    goals.set_status(db, old, GoalStatus.PAUSED, NOW)
    assert run_capture(db, promise_answer(pato.id, str(old.id))).goal_id is None


def test_confirming_after_the_goal_was_deleted_is_refused_and_writes_nothing(db: Session) -> None:
    pato = person(db, "Patrik Veselý", "Paťo")
    fit = goal(db, "Get fit")
    result = capture.capture(
        db,
        promise_answer(pato.id, str(fit.id)),
        "poslem Paťovi plán",
        today=TODAY,
        default_currency="EUR",
    )
    (proposal,) = result.proposals
    goals.delete_goal(db, fit)
    with pytest.raises(InvalidInputError, match="no longer exists"):
        inbox.confirm(db, proposal, today=TODAY, default_currency="EUR")
    db.rollback()
    assert db.scalars(select(Promise)).all() == []


def test_capture_without_goals_is_unchanged(db: Session) -> None:
    pato = person(db, "Patrik Veselý", "Paťo")
    extractor = promise_answer(pato.id, "1")
    created = run_capture(db, extractor)
    assert extractor.requests[0].goals == ()
    assert created.goal_id is None


def test_the_inbox_card_picks_and_saves_the_goal(web: TestClient, app: FastAPI) -> None:
    pato = add(web, "Patrik Veselý")
    post(web, f"/people/{pato}/aliases", value="Paťo")
    web.post("/goals", data={"title": "Get fit"})
    web.post("/goals", data={"title": "Save 5000"})
    web.headers.update({"Authorization": f"Bearer {_token(app)}"})
    fit, save = (g["id"] for g in web.get("/api/v1/goals").json())
    app.state.claude = promise_answer(pato, str(fit))
    page = post(web, "/inbox/capture", text="poslem Paťovi plán")
    (proposal,) = proposal_ids(page)

    card = web.get(f"/inbox/{proposal}").text
    assert 'name="promise-0-goal"' in card
    assert f'<option value="{fit}" selected>1. Get fit</option>' in card
    assert f'<option value="{save}" >' in card or f'<option value="{save}">' in card

    form = {
        "full": "1",
        "promise-0-include": "1",
        "promise-0-person": str(pato),
        "promise-0-direction": "i_promised",
        "promise-0-text": "send the training plan",
        "promise-0-due": "",
        "promise-0-goal": str(save),
    }
    assert "Saved your changes." in text(post(web, f"/inbox/{proposal}/save", **form))
    assert f'<option value="{save}" selected>' in web.get(f"/inbox/{proposal}").text
    confirmed = post(web, f"/inbox/{proposal}/confirm", **form)
    assert "Saved: 1 promise." in text(confirmed), text(confirmed)[:600]
    (created,) = web.get("/api/v1/promises").json()
    assert created["goal_id"] == save


def _token(app: FastAPI) -> str:
    return app.state.settings.orbit_api_token.get_secret_value()


# --- Merge and undo ---


def test_merge_moves_goal_links_and_undo_gives_them_back(db: Session) -> None:
    keep = people.create_person(db, people.PersonCreate(display_name="Patrik Veselý"))
    other = people.create_person(db, people.PersonCreate(display_name="pato"))
    both, only_other, only_keep = goal(db, "Both"), goal(db, "Only other"), goal(db, "Only keep")
    for g, who in ((both, keep), (both, other), (only_other, other), (only_keep, keep)):
        goals.add_person(db, g, who.id)
    merged = duplicates.merge(db, keep.id, other.id)
    db.flush()
    db.expire_all()
    assert {g.title for g in goals.goals_of_person(db, merged.id)} == {
        "Both",
        "Only other",
        "Only keep",
    }
    assert len(db.execute(select(goal_person).where(goal_person.c.goal_id == both.id)).all()) == 1
    record = db.scalars(select(MergeRecord)).one()
    restored = duplicates.undo_merge(db, record.id)
    db.flush()
    db.expire_all()  # the merge and its undo move links with SQL; callers commit, which expires
    assert {g.title for g in goals.goals_of_person(db, restored.id)} == {"Both", "Only other"}
    assert {g.title for g in goals.goals_of_person(db, keep.id)} == {"Both", "Only keep"}


def test_undo_of_a_merge_made_before_goals_existed(db: Session) -> None:
    keep = people.create_person(db, people.PersonCreate(display_name="Patrik Veselý"))
    other = people.create_person(db, people.PersonCreate(display_name="pato"))
    duplicates.merge(db, keep.id, other.id)
    record = db.scalars(select(MergeRecord)).one()
    snapshot = dict(record.snapshot_json)
    snapshot.pop("goals")
    record.snapshot_json = snapshot  # as an old record has it
    db.flush()
    duplicates.undo_merge(db, record.id)  # no KeyError


def test_undo_skips_a_goal_deleted_since(db: Session) -> None:
    keep = people.create_person(db, people.PersonCreate(display_name="Patrik Veselý"))
    other = people.create_person(db, people.PersonCreate(display_name="pato"))
    g = goal(db, "Gone")
    goals.add_person(db, g, other.id)
    duplicates.merge(db, keep.id, other.id)
    goals.delete_goal(db, g)
    record = db.scalars(select(MergeRecord)).one()
    restored = duplicates.undo_merge(db, record.id)
    assert goals.goals_of_person(db, restored.id) == []


# --- The digest ---


def test_digest_nudges_after_a_week_without_review(db: Session) -> None:
    goal(db, "Get fit")
    sunday = NOW + timedelta(days=6)  # reviewed 6 days before: fine
    digest = reminders.build_digest(db, sunday, PRAGUE)
    assert digest.goals_review_days is None
    assert not any("goals" in s for s in reminders.digest_sections(digest, PRAGUE))
    later = NOW + timedelta(days=9)
    digest = reminders.build_digest(db, later, PRAGUE)
    assert digest.goals_review_days == 9
    assert reminders.digest_sections(digest, PRAGUE)[-1] == (
        "🎯 Review your goals: last reviewed 9 days ago"
    )


def test_digest_has_no_goal_line_without_goals(db: Session) -> None:
    digest = reminders.build_digest(db, NOW, PRAGUE)
    assert digest.goals_review_days is None


# --- The context ---


def test_goals_context(db: Session) -> None:
    pato = people.create_person(db, people.PersonCreate(display_name="Patrik Veselý"))
    fit = goal(db, "Get fit", why="my back hurts " + "x" * 300, target="10 km")
    goal(db, "Save 5000", target_date=date(2026, 10, 1))
    goals.add_person(db, fit, pato.id)
    created = promises_for(db, pato, fit)
    result = GoalsProvider().build(db, WINDOW)
    assert [(g.rank, g.title) for g in result.goals] == [(1, "Get fit"), (2, "Save 5000")]
    first, second = result.goals
    assert first.why is not None and len(first.why) > 300  # not clipped to a few words
    assert (first.target, [p.name for p in first.people]) == ("10 km", ["Patrik Veselý"])
    assert [(p.id, p.text, p.person) for p in first.open_promises] == [
        (created.id, "send the plan", "Patrik Veselý")
    ]
    assert (second.overdue, second.due_in_window) == (True, False)
    assert first.days_since_review == 0  # reviewed when created (NOW is not WINDOW: clamped)


def promises_for(db: Session, who, g: Goal) -> Promise:
    from orbit.services import promises

    data = promises.PromiseCreate(
        person_id=who.id, direction=PromiseDirection.I_PROMISED, text="send the plan", goal_id=g.id
    )
    return promises.create_promise(db, data, TODAY)


def test_goals_context_focus_and_inactive(db: Session) -> None:
    pato = people.create_person(db, people.PersonCreate(display_name="Patrik Veselý"))
    fit, save, old = goal(db, "Get fit"), goal(db, "Save 5000"), goal(db, "Old")
    goals.add_person(db, fit, pato.id)
    goals.set_status(db, old, GoalStatus.PAUSED, NOW)
    focused = GoalsProvider().build(db, WINDOW, ContextFocus(person_id=pato.id))
    assert [g.title for g in focused.goals] == ["Get fit"]
    assert [g.title for g in GoalsProvider().build(db, WINDOW).goals] == ["Get fit", "Save 5000"]
    assert save.rank == 2


def test_goals_are_first_in_the_snapshot_and_never_trimmed(db: Session) -> None:
    person_ = people.create_person(db, people.PersonCreate(display_name="Pato"))
    for i in range(5):
        goal(db, f"Goal {i}", why="why " * 50)
        promises_for(db, person_, goals.active_goals(db)[0])
    snapshot = build_context(db, WINDOW, budget_chars=1)
    assert list(snapshot.model_dump())[3] == "goals"  # after generated_at, window, focus
    assert len(snapshot.goals.goals) == 5
    assert "goals" not in snapshot.budget.trimmed
    assert snapshot.people.truncated or snapshot.promises.truncated  # others lost items first
    assert snapshot.budget.used_chars > 1  # reported, not hidden


def test_goals_context_is_masked(db: Session) -> None:
    goal(db, "Pay SK3112000000198742637541", why="call +421 905 123 456")
    blob = build_context(db, WINDOW).model_dump_json()
    assert "SK3112000000198742637541" not in blob
    assert "905 123" not in blob


def test_created_goal_reviewed_at_uses_utc(db: Session) -> None:
    g = goal(db, "A")
    assert g.reviewed_at == NOW
    assert g.reviewed_at.tzinfo is UTC
