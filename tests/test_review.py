"""The weekly review agent (SPEC §2.17), with a fake Claude."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from orbit.domain.enums import GoalStatus, ProposalKind, ProposalStatus, ReviewStatus
from orbit.domain.goals import Goal
from orbit.domain.inbox import Notification, Proposal
from orbit.domain.ledger import Promise
from orbit.domain.review import WeeklyReview
from orbit.integrations.claude import (
    ClaudeError,
    FollowUpIdea,
    GoalLinkIdea,
    GoalRerankIdea,
    PromiseDueIdea,
    ReviewAnswer,
    ReviewConflict,
    ReviewItem,
    ReviewOutput,
    ReviewPriority,
    ReviewProposals,
    ReviewRequest,
    build_review_prompt,
)
from orbit.jobs import scheduler
from orbit.services import goals, inbox, promises, review
from orbit.services.errors import InvalidInputError
from orbit.services.notifications import QuietHours
from tests.test_context_providers import IBAN, PHONE, meeting, money, person, promise
from tests.test_goals import NOW, goal

PRAGUE = ZoneInfo("Europe/Prague")
QUIET = QuietHours(time(22, 0), time(8, 0), PRAGUE)
# A Sunday, 19:30 in Prague.
SUNDAY = datetime(2026, 10, 4, 17, 30, tzinfo=UTC)
PLAN = date(2026, 10, 5)
URL = "https://orbit.example.com"


def empty_output(**changes: Any) -> ReviewOutput:
    values: dict[str, Any] = {
        "summary": "A decent week.",
        "wins": [],
        "slips": [],
        "conflicts": [],
        "priorities": [],
        "proposals": ReviewProposals(
            follow_ups=[], promise_dues=[], goal_rerank=None, goal_links=[]
        ),
    }
    values.update(changes)
    return ReviewOutput(**values)


def proposals_of(**changes: Any) -> ReviewProposals:
    values: dict[str, Any] = {
        "follow_ups": [],
        "promise_dues": [],
        "goal_rerank": None,
        "goal_links": [],
    }
    values.update(changes)
    return ReviewProposals(**values)


class FakeReviewer:
    def __init__(self, result: ReviewOutput | Exception | None = None) -> None:
        self.result = result or empty_output()
        self.requests: list[ReviewRequest] = []

    def weekly_review(self, request: ReviewRequest) -> ReviewAnswer:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return ReviewAnswer(self.result.model_copy(deep=True), "test-model", 1200, 340)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, message: Any) -> None:
        self.sent.append(message)


def generate(
    db: Session, reviewer: FakeReviewer, now: datetime = SUNDAY, budget: int = 400_000
) -> WeeklyReview:
    row = review.claim(db, PLAN, now)
    assert row is not None
    review.generate(db, row, reviewer, tz=PRAGUE, budget_chars=budget, now=now)
    return row


@pytest.fixture
def life(db: Session) -> dict[str, Any]:
    """Two goals, Pato (with a promise and a debt), Terka, and a meeting next week."""
    fit, save = goal(db, "Get fit", why="my back hurts"), goal(db, "Save 5000")
    pato, terka = person(db, "Patrik Veselý"), person(db, "Terka")
    goals.add_person(db, fit, pato.id)
    promise(db, pato, "send the training plan", due=date(2026, 10, 6))
    owed = db.scalars(select(Promise)).one()
    money(db, pato, 2400, "pizza")
    money(db, terka, 500)  # something open with Terka, so she is in the snapshots
    start = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
    gym = meeting(db, "gym", "Gym with Paťo", start)
    db.commit()
    return {"fit": fit, "save": save, "pato": pato, "terka": terka, "promise": owed, "meeting": gym}


# --- Which week, and when ---


def test_weeks_around() -> None:
    weeks = review.weeks_around(date(2026, 10, 7))  # a Wednesday
    assert (weeks.review_start, weeks.review_end) == (date(2026, 10, 5), date(2026, 10, 11))
    assert (weeks.plan_start, weeks.plan_end) == (date(2026, 10, 12), date(2026, 10, 18))
    assert review.weeks_around(date(2026, 10, 4)).plan_start == date(2026, 10, 5)  # Sunday


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (datetime(2026, 10, 4, 18, 59), None),  # Sunday, before 19:00
        (datetime(2026, 10, 4, 19, 0), date(2026, 10, 5)),
        (datetime(2026, 10, 4, 23, 59), date(2026, 10, 5)),
        (datetime(2026, 10, 5, 8, 0), date(2026, 10, 5)),  # Monday: the missed one
        (datetime(2026, 10, 7, 23, 0), date(2026, 10, 5)),  # Wednesday night: last chance
        (datetime(2026, 10, 8, 0, 0), None),  # Thursday: skipped
        (datetime(2026, 10, 10, 12, 0), None),
        (datetime(2026, 10, 11, 18, 59), None),
        (datetime(2026, 10, 11, 19, 0), date(2026, 10, 12)),
    ],
)
def test_due_plan_start(local: datetime, expected: date | None) -> None:
    assert review.due_plan_start(local.replace(tzinfo=PRAGUE), PRAGUE) == expected


# --- Claiming ---


def test_claim_makes_one_row_per_week(db: Session) -> None:
    row = review.claim(db, PLAN, NOW)
    assert row is not None
    assert (row.review_start, row.review_end) == (date(2026, 9, 28), date(2026, 10, 4))
    assert (row.plan_start, row.plan_end) == (date(2026, 10, 5), date(2026, 10, 11))
    assert (row.status, row.attempts) == (ReviewStatus.PENDING, 1)
    assert review.claim(db, PLAN, NOW + timedelta(minutes=1)) is None  # being made right now
    assert db.scalar(select(func.count(WeeklyReview.id))) == 1


def test_a_stopped_server_leaves_a_stale_claim_that_can_be_retaken(db: Session) -> None:
    row = review.claim(db, PLAN, NOW)
    assert row is not None
    again = review.claim(db, PLAN, NOW + review.STALE_AFTER)
    assert again is row
    assert row.attempts == 2


def test_failed_weeks_are_retried_up_to_the_limit(db: Session) -> None:
    row = review.claim(db, PLAN, NOW)
    assert row is not None
    for n in range(2, review.MAX_ATTEMPTS + 1):
        row.status = ReviewStatus.FAILED
        again = review.claim(db, PLAN, NOW)
        assert again is row and row.attempts == n
    row.status = ReviewStatus.FAILED
    assert review.claim(db, PLAN, NOW) is None  # out of attempts
    assert review.claim(db, PLAN, NOW, retry=True) is row
    assert row.attempts == 1


def test_a_ready_review_is_final(db: Session, life: dict[str, Any]) -> None:
    generate(db, FakeReviewer())
    assert review.claim(db, PLAN, SUNDAY, retry=True) is None


# --- Generating ---


def test_generate_stores_the_review_and_what_claude_saw(db: Session, life: dict[str, Any]) -> None:
    reviewer = FakeReviewer(
        empty_output(
            priorities=[ReviewPriority(text="Call Peter", why="x", goal_id=life["fit"].id)]
        )
    )
    row = generate(db, reviewer)
    assert row.status is ReviewStatus.READY
    assert row.output_json is not None and row.output_json["summary"] == "A decent week."
    assert (row.model, row.input_tokens, row.output_tokens) == ("test-model", 1200, 340)
    assert row.input_chars and row.input_chars > 1000
    assert row.input_json is not None
    assert row.input_json["last_week"]["window"]["start"] == "2026-09-28"
    assert row.input_json["next_week"]["window"]["start"] == "2026-10-05"
    (request,) = reviewer.requests
    assert request.review_week == (date(2026, 9, 28), date(2026, 10, 4))
    assert request.plan_week == (date(2026, 10, 5), date(2026, 10, 11))
    assert "Get fit" in request.next_week_json and "my back hurts" in request.next_week_json
    assert request.today == date(2026, 10, 4)
    assert request.previous is None


def test_what_claude_is_given_is_masked(db: Session, life: dict[str, Any]) -> None:
    goal(db, f"Pay {IBAN}", why=f"call {PHONE}")
    reviewer = FakeReviewer()
    generate(db, reviewer)
    (request,) = reviewer.requests
    prompt = build_review_prompt(request)
    for secret in (IBAN, "1987426375", "905 123"):
        assert secret not in prompt
    row = db.scalars(select(WeeklyReview)).one()
    assert IBAN not in str(row.input_json)


def test_the_prompt_carries_the_previous_review_and_the_inbox(
    db: Session, life: dict[str, Any]
) -> None:
    first = (
        generate(
            db,
            FakeReviewer(
                empty_output(
                    summary="Quiet week.",
                    priorities=[ReviewPriority(text="Run twice", why="fit", goal_id=None)],
                )
            ),
            now=SUNDAY - timedelta(days=7),
        )
        if False
        else None
    )
    assert first is None
    old = WeeklyReview(
        plan_start=date(2026, 9, 28),
        plan_end=date(2026, 10, 4),
        review_start=date(2026, 9, 21),
        review_end=date(2026, 9, 27),
        status=ReviewStatus.READY,
        output_json={"summary": "Quiet week.", "priorities": [{"text": "Run twice"}]},
        attempts=1,
        input_tokens=0,
        output_tokens=0,
    )
    db.add(old)
    db.add_all(
        [
            Proposal(
                kind=ProposalKind.FOLLOW_UP,
                status=ProposalStatus.PENDING,
                summary="Follow up with Terka: call",
                payload_json={},
                source_type="weekly_review",
            ),
            Proposal(
                kind=ProposalKind.FOLLOW_UP,
                status=ProposalStatus.REJECTED,
                summary="Follow up with Jano: ski",
                payload_json={},
                source_type="weekly_review",
                resolved_at=SUNDAY - timedelta(days=3),
            ),
            Proposal(
                kind=ProposalKind.FOLLOW_UP,
                status=ProposalStatus.REJECTED,
                summary="Long ago rejected",
                payload_json={},
                source_type="weekly_review",
                resolved_at=SUNDAY - timedelta(days=90),
            ),
        ]
    )
    db.flush()
    reviewer = FakeReviewer()
    generate(db, reviewer)
    (request,) = reviewer.requests
    assert request.previous is not None
    assert (request.previous.summary, request.previous.priorities) == (
        "Quiet week.",
        ("Run twice",),
    )
    assert request.pending == ("Follow up with Terka: call",)
    assert request.rejected == ("Follow up with Jano: ski",)
    prompt = build_review_prompt(request)
    assert "Quiet week." in prompt and "do not repeat" in prompt and "rejected recently" in prompt


def test_a_claude_error_fails_the_review_and_keeps_the_input(
    db: Session, life: dict[str, Any]
) -> None:
    row = generate(db, FakeReviewer(ClaudeError("Claude API error: Timeout")))
    assert (row.status, row.error) == (ReviewStatus.FAILED, "Claude API error: Timeout")
    assert row.output_json is None and row.input_json is not None
    assert db.scalars(select(Proposal)).all() == []
    again = review.claim(db, PLAN, SUNDAY)
    assert again is row
    review.generate(db, row, FakeReviewer(), tz=PRAGUE, budget_chars=400_000, now=SUNDAY)
    assert (row.status, row.error, row.attempts) == (ReviewStatus.READY, None, 2)


# --- Checking Claude's answer ---


def test_unknown_ids_are_dropped_everywhere(db: Session, life: dict[str, Any]) -> None:
    fit, pato, gym, owed = life["fit"], life["pato"], life["meeting"], life["promise"]
    answer = empty_output(
        wins=[ReviewItem(text="Kept a promise", goal_ids=[fit.id, 999], person_ids=[pato.id, 999])],
        conflicts=[
            ReviewConflict(
                title="Gym vs plan",
                detail="d",
                suggestion="s",
                goal_ids=[fit.id, 888],
                person_ids=[777],
                meeting_ids=[gym.id, 666],
                promise_ids=[owed.id, 555],
            )
        ],
        priorities=[
            ReviewPriority(text="a", why="w", goal_id=999),
            ReviewPriority(text="b", why="w", goal_id=fit.id),
        ],
    )
    row = generate(db, FakeReviewer(answer))
    out = row.output_json
    assert out is not None
    assert out["wins"][0]["goal_ids"] == [fit.id] and out["wins"][0]["person_ids"] == [pato.id]
    conflict = out["conflicts"][0]
    assert (conflict["goal_ids"], conflict["person_ids"]) == ([fit.id], [])
    assert (conflict["meeting_ids"], conflict["promise_ids"]) == ([gym.id], [owed.id])
    assert [p["goal_id"] for p in out["priorities"]] == [None, fit.id]


def test_priorities_are_capped(db: Session, life: dict[str, Any]) -> None:
    many = [ReviewPriority(text=f"p{i}", why="w", goal_id=None) for i in range(12)]
    row = generate(db, FakeReviewer(empty_output(priorities=many)))
    assert row.output_json is not None
    assert len(row.output_json["priorities"]) == review.MAX_PRIORITIES


def test_proposals_are_created_and_checked(db: Session, life: dict[str, Any]) -> None:
    fit, save, pato, terka, owed = (life[k] for k in ("fit", "save", "pato", "terka", "promise"))
    ideas = proposals_of(
        follow_ups=[
            FollowUpIdea(
                person_id=terka.id,
                text="call about the move",
                due=date(2026, 10, 9),
                goal_id=save.id,
                reason="she asked",
            ),
            FollowUpIdea(person_id=999, text="ghost", due=None, goal_id=None, reason="x"),
            FollowUpIdea(person_id=pato.id, text="  ", due=None, goal_id=None, reason="x"),
            FollowUpIdea(
                person_id=pato.id, text="ask about gym", due=None, goal_id=999, reason="x"
            ),
        ],
        promise_dues=[
            PromiseDueIdea(promise_id=owed.id, due=date(2026, 10, 9), reason="meeting Wednesday"),
            PromiseDueIdea(promise_id=999, due=date(2026, 10, 9), reason="x"),
        ],
        goal_rerank=GoalRerankIdea(goal_ids=[save.id, fit.id], reason="money first"),
        goal_links=[
            GoalLinkIdea(goal_id=fit.id, promise_id=owed.id, person_id=None, reason="the plan"),
            GoalLinkIdea(goal_id=fit.id, promise_id=None, person_id=terka.id, reason="trains too"),
            GoalLinkIdea(goal_id=fit.id, promise_id=owed.id, person_id=terka.id, reason="both"),
            GoalLinkIdea(goal_id=fit.id, promise_id=None, person_id=None, reason="neither"),
            GoalLinkIdea(goal_id=999, promise_id=owed.id, person_id=None, reason="x"),
        ],
    )
    row = generate(db, FakeReviewer(empty_output(proposals=ideas)))
    made = review.review_proposals(db, row)
    assert [p.kind for p in made] == [
        ProposalKind.GOAL_RERANK,
        ProposalKind.PROMISE_DUE,
        ProposalKind.FOLLOW_UP,
        ProposalKind.FOLLOW_UP,
        ProposalKind.GOAL_LINK,
        ProposalKind.GOAL_LINK,
    ]
    assert all(p.status is ProposalStatus.PENDING and p.source_id == str(row.id) for p in made)
    assert all(p.source_type == "weekly_review" and p.confidence is None for p in made)
    summaries = [p.summary for p in made]
    assert summaries[0] == "New goal order: 1. Save 5000; 2. Get fit. Why: money first"
    assert (
        summaries[1]
        == "Move “send the training plan” (Patrik Veselý) to 9 Oct. Why: meeting Wednesday"
    )
    assert summaries[2] == (
        "Follow up with Terka: call about the move by 9 Oct (serves “Save 5000”). Why: she asked"
    )
    assert "serves" not in summaries[3]  # an unknown goal became none
    assert summaries[4] == "“send the training plan” serves “Get fit”. Why: the plan"
    assert summaries[5] == "Link Terka to “Get fit”. Why: trains too"


def test_a_rerank_must_cover_every_active_goal_and_change_something(
    db: Session, life: dict[str, Any]
) -> None:
    fit, save = life["fit"], life["save"]
    for bad in ([fit.id], [fit.id, save.id], [fit.id, fit.id], [save.id, fit.id, 999]):
        row = generate(
            db,
            FakeReviewer(
                empty_output(
                    proposals=proposals_of(goal_rerank=GoalRerankIdea(goal_ids=bad, reason="r"))
                )
            ),
        )
        assert review.review_proposals(db, row) == []
        db.delete(row)
        db.flush()


def test_proposals_are_capped_from_the_end(db: Session, life: dict[str, Any]) -> None:
    pato = life["pato"]
    follows = [
        FollowUpIdea(person_id=pato.id, text=f"thing {i}", due=None, goal_id=None, reason="r")
        for i in range(25)
    ]
    row = generate(db, FakeReviewer(empty_output(proposals=proposals_of(follow_ups=follows))))
    made = review.review_proposals(db, row)
    assert len(made) == review.MAX_PROPOSALS
    assert made[0].payload_json["text"] == "thing 0"


def test_a_proposal_that_repeats_a_pending_one_is_not_made_again(
    db: Session, life: dict[str, Any]
) -> None:
    pato, owed = life["pato"], life["promise"]
    ideas = proposals_of(
        follow_ups=[
            FollowUpIdea(
                person_id=pato.id, text="Ask about gym", due=None, goal_id=None, reason="r"
            )
        ],
        promise_dues=[PromiseDueIdea(promise_id=owed.id, due=date(2026, 10, 9), reason="r")],
    )
    generate(db, FakeReviewer(empty_output(proposals=ideas)))
    other = review.claim(db, PLAN + timedelta(days=7), SUNDAY + timedelta(days=7))
    assert other is not None
    review.generate(
        db,
        other,
        FakeReviewer(
            empty_output(
                proposals=proposals_of(
                    follow_ups=[
                        FollowUpIdea(
                            person_id=pato.id,
                            text="ask about GYM",
                            due=None,
                            goal_id=None,
                            reason="r",
                        )
                    ],
                    promise_dues=ideas.promise_dues,
                )
            )
        ),
        tz=PRAGUE,
        budget_chars=400_000,
        now=SUNDAY + timedelta(days=7),
    )
    assert review.review_proposals(db, other) == []
    assert db.scalar(select(func.count(Proposal.id))) == 2


def test_the_review_never_writes_to_a_domain_table(db: Session, life: dict[str, Any]) -> None:
    fit, save, owed = life["fit"], life["save"], life["promise"]
    ideas = proposals_of(
        promise_dues=[PromiseDueIdea(promise_id=owed.id, due=date(2026, 12, 1), reason="r")],
        goal_rerank=GoalRerankIdea(goal_ids=[save.id, fit.id], reason="r"),
        goal_links=[GoalLinkIdea(goal_id=save.id, promise_id=owed.id, person_id=None, reason="r")],
    )
    generate(db, FakeReviewer(empty_output(proposals=ideas)))
    db.refresh(owed)
    assert owed.due == date(2026, 10, 6) and owed.goal_id is None
    assert [(g.title, g.rank) for g in goals.active_goals(db)] == [("Get fit", 1), ("Save 5000", 2)]


# --- Confirming each proposal kind ---


def confirm(db: Session, proposal: Proposal) -> inbox.ConfirmResult:
    return inbox.confirm(db, proposal, today=date(2026, 10, 5), default_currency="EUR")


def one(db: Session, kind: ProposalKind, payload: dict[str, Any]) -> Proposal:
    proposal = Proposal(
        kind=kind,
        status=ProposalStatus.PENDING,
        summary="s",
        payload_json=payload,
        source_type="weekly_review",
    )
    db.add(proposal)
    db.flush()
    return proposal


def test_confirm_follow_up_creates_a_promise(db: Session, life: dict[str, Any]) -> None:
    terka, save = life["terka"], life["save"]
    result = confirm(
        db,
        one(
            db,
            ProposalKind.FOLLOW_UP,
            {"person_id": terka.id, "text": "call her", "due": "2026-10-09", "goal_id": save.id},
        ),
    )
    assert result.message == "Added a promise for Terka: call her."
    assert result.person_id == terka.id
    created = db.scalars(select(Promise).where(Promise.text == "call her")).one()
    assert (created.due, created.goal_id, created.direction.value) == (
        date(2026, 10, 9),
        save.id,
        "i_promised",
    )


def test_confirm_follow_up_defaults_the_due_date_and_checks_the_person(
    db: Session, life: dict[str, Any]
) -> None:
    terka = life["terka"]
    confirm(db, one(db, ProposalKind.FOLLOW_UP, {"person_id": terka.id, "text": "x"}))
    assert db.scalars(select(Promise).where(Promise.text == "x")).one().due == date(2026, 10, 12)
    with pytest.raises(InvalidInputError, match="not found"):
        confirm(db, one(db, ProposalKind.FOLLOW_UP, {"person_id": 999, "text": "y"}))


def test_confirm_promise_due(db: Session, life: dict[str, Any]) -> None:
    owed = life["promise"]
    result = confirm(
        db, one(db, ProposalKind.PROMISE_DUE, {"promise_id": owed.id, "due": "2026-10-09"})
    )
    assert owed.due == date(2026, 10, 9)
    assert result.message == "“send the training plan” is now due 9 Oct."


def test_promise_due_is_refused_once_the_promise_is_closed_or_gone(
    db: Session, life: dict[str, Any]
) -> None:
    owed = life["promise"]
    promises.set_status(owed, promises.PromiseStatus.DONE, NOW)
    with pytest.raises(InvalidInputError, match="already done"):
        confirm(db, one(db, ProposalKind.PROMISE_DUE, {"promise_id": owed.id, "due": "2026-10-09"}))
    with pytest.raises(InvalidInputError, match="no longer exists"):
        confirm(db, one(db, ProposalKind.PROMISE_DUE, {"promise_id": 999, "due": "2026-10-09"}))


def test_confirm_goal_rerank_and_its_refusal_when_goals_changed(
    db: Session, life: dict[str, Any]
) -> None:
    fit, save = life["fit"], life["save"]
    result = confirm(db, one(db, ProposalKind.GOAL_RERANK, {"goal_ids": [save.id, fit.id]}))
    assert result.message == "New order: 1. Save 5000; 2. Get fit."
    stale = one(db, ProposalKind.GOAL_RERANK, {"goal_ids": [fit.id, save.id]})
    goal(db, "A third goal")
    with pytest.raises(InvalidInputError, match="active goals changed"):
        confirm(db, stale)
    assert [g.title for g in goals.active_goals(db)] == ["Save 5000", "Get fit", "A third goal"]


def test_confirm_goal_link(db: Session, life: dict[str, Any]) -> None:
    fit, save, terka, owed = life["fit"], life["save"], life["terka"], life["promise"]
    confirm(db, one(db, ProposalKind.GOAL_LINK, {"goal_id": save.id, "promise_id": owed.id}))
    assert owed.goal_id == save.id
    result = confirm(
        db, one(db, ProposalKind.GOAL_LINK, {"goal_id": fit.id, "person_id": terka.id})
    )
    assert result.message == "Terka is now linked to “Get fit”."
    assert terka in fit.people


def test_goal_link_is_refused_for_inactive_goals_and_closed_promises(
    db: Session, life: dict[str, Any]
) -> None:
    save, owed = life["save"], life["promise"]
    goals.set_status(db, save, GoalStatus.PAUSED, NOW)
    with pytest.raises(InvalidInputError, match="isn't an active goal"):
        confirm(db, one(db, ProposalKind.GOAL_LINK, {"goal_id": save.id, "promise_id": owed.id}))
    promises.set_status(owed, promises.PromiseStatus.DROPPED, NOW)
    with pytest.raises(InvalidInputError, match="isn't open"):
        confirm(
            db, one(db, ProposalKind.GOAL_LINK, {"goal_id": life["fit"].id, "promise_id": owed.id})
        )


def test_goal_link_payload_needs_exactly_one_target(db: Session, life: dict[str, Any]) -> None:
    fit, terka, owed = life["fit"], life["terka"], life["promise"]
    for payload in (
        {"goal_id": fit.id},
        {"goal_id": fit.id, "promise_id": owed.id, "person_id": terka.id},
    ):
        with pytest.raises(InvalidInputError, match="Invalid proposal data"):
            confirm(db, one(db, ProposalKind.GOAL_LINK, payload))


# --- The job and the push ---


def make_sessions(app: FastAPI) -> sessionmaker[Session]:
    return app.state.sessionmaker  # type: ignore[no-any-return]


def test_the_job_makes_one_review_and_one_push_per_week(app: FastAPI, settings: Any) -> None:
    sessions = make_sessions(app)
    with sessions() as db:
        goal(db, "Get fit")
        db.commit()
    reviewer, sender = FakeReviewer(), FakeSender()
    assert scheduler.weekly_review(sessions, reviewer, sender, settings, SUNDAY) == 1
    assert scheduler.weekly_review(sessions, reviewer, sender, settings, SUNDAY) is None
    assert (
        scheduler.weekly_review(sessions, reviewer, sender, settings, SUNDAY + timedelta(hours=1))
        is None
    )
    assert (
        scheduler.weekly_review(sessions, reviewer, sender, settings, SUNDAY + timedelta(days=1))
        is None
    )
    assert len(reviewer.requests) == 1
    with sessions() as db:
        assert db.scalar(select(func.count(WeeklyReview.id))) == 1
        assert (
            db.scalar(
                select(func.count(Notification.id)).where(Notification.kind == "weekly_review")
            )
            == 1
        )
        row = db.scalars(select(WeeklyReview)).one()
        assert row.pushed_at is not None
    (push,) = sender.sent
    assert push.title == "🗓 Weekly review ready"
    assert push.body.startswith("A decent week.")
    assert push.click == f"{settings.orbit_url}/reviews/{row.id}"


def test_the_job_does_nothing_outside_its_window(app: FastAPI, settings: Any) -> None:
    reviewer = FakeReviewer()
    thursday = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert (
        scheduler.weekly_review(make_sessions(app), reviewer, FakeSender(), settings, thursday)
        is None
    )
    assert reviewer.requests == []


def test_the_job_retries_a_failed_week_then_gives_up(app: FastAPI, settings: Any) -> None:
    sessions = make_sessions(app)
    reviewer = FakeReviewer(ClaudeError("down"))
    for n in range(review.MAX_ATTEMPTS):
        moment = SUNDAY + timedelta(minutes=n)
        assert scheduler.weekly_review(sessions, reviewer, FakeSender(), settings, moment) == 1
    assert (
        scheduler.weekly_review(
            sessions, reviewer, FakeSender(), settings, SUNDAY + timedelta(hours=1)
        )
        is None
    )
    assert len(reviewer.requests) == review.MAX_ATTEMPTS
    with sessions() as db:
        row = db.scalars(select(WeeklyReview)).one()
        assert (row.status, row.error, row.attempts) == (ReviewStatus.FAILED, "down", 3)
        assert db.scalar(select(func.count(Notification.id))) == 0  # nothing to push


def test_an_unexpected_error_marks_the_review_failed(app: FastAPI, settings: Any) -> None:
    class Broken:
        def weekly_review(self, request: ReviewRequest) -> ReviewAnswer:
            raise RuntimeError("bug")

    sessions = make_sessions(app)
    assert scheduler.weekly_review(sessions, Broken(), FakeSender(), settings, SUNDAY) == 1
    with sessions() as db:
        row = db.scalars(select(WeeklyReview)).one()
        assert (row.status, row.error) == (ReviewStatus.FAILED, "RuntimeError: bug")


def test_quiet_hours_hold_the_push_not_the_review(db: Session, life: dict[str, Any]) -> None:
    row = generate(db, FakeReviewer(), now=datetime(2026, 10, 4, 21, 30, tzinfo=UTC))  # 23:30 local
    sender = FakeSender()
    assert review.send_push(
        db, row, sender, base_url=URL, quiet=QUIET, now=datetime(2026, 10, 4, 21, 30, tzinfo=UTC)
    )
    assert sender.sent == []  # queued until quiet hours end
    assert row.status is ReviewStatus.READY
    note = db.scalars(select(Notification)).one()
    assert note.dedupe_key == "weekly_review:2026-10-05" and note.sent_at is None


def test_the_push_counts_waiting_proposals(db: Session, life: dict[str, Any]) -> None:
    owed = life["promise"]
    ideas = proposals_of(
        promise_dues=[PromiseDueIdea(promise_id=owed.id, due=date(2026, 10, 9), reason="r")]
    )
    row = generate(db, FakeReviewer(empty_output(proposals=ideas)))
    message = review.push_message(db, row, URL)
    assert message.body == "A decent week.\n\n1 proposal in your Inbox."
    assert message.click == f"{URL}/reviews/{row.id}"
    assert [a.label for a in message.actions] == ["Open"]


def test_failed_reviews_are_not_pushed(db: Session, life: dict[str, Any]) -> None:
    row = generate(db, FakeReviewer(ClaudeError("x")))
    assert review.send_push(db, row, FakeSender(), base_url=URL, quiet=QUIET) is False


# --- API and web ---


def use_reviewer(app: FastAPI, reviewer: Any) -> Any:
    app.state.reviewer = reviewer
    return reviewer


def test_api_requires_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/reviews").status_code == 401
    assert client.post("/api/v1/reviews/run").status_code == 401


def test_api_run_list_get_and_input(api: TestClient, app: FastAPI) -> None:
    api.post("/api/v1/goals", json={"title": "Get fit", "why": "my back hurts"})
    reviewer = use_reviewer(
        app,
        FakeReviewer(empty_output(priorities=[ReviewPriority(text="Run", why="w", goal_id=None)])),
    )
    ran = api.post("/api/v1/reviews/run")
    assert ran.status_code == 200, ran.text
    data = ran.json()
    assert data["status"] == "ready" and data["output"]["summary"] == "A decent week."
    assert (data["model"], data["input_tokens"], data["output_tokens"]) == ("test-model", 1200, 340)
    assert [r["id"] for r in api.get("/api/v1/reviews").json()] == [data["id"]]
    assert (
        api.get(f"/api/v1/reviews/{data['id']}").json()["output"]["priorities"][0]["text"] == "Run"
    )
    seen = api.get(f"/api/v1/reviews/{data['id']}/input").json()
    assert "Get fit" in str(seen["next_week"]["goals"])
    assert api.get("/api/v1/reviews/999").status_code == 404
    assert api.post("/api/v1/reviews/run").status_code == 422  # this week's is final
    assert len(reviewer.requests) == 1


def test_api_run_without_claude_is_422(api: TestClient, app: FastAPI) -> None:
    use_reviewer(app, None)
    assert api.post("/api/v1/reviews/run").status_code == 422


def test_api_failed_run_can_be_retried(api: TestClient, app: FastAPI) -> None:
    reviewer = use_reviewer(app, FakeReviewer(ClaudeError("down")))
    failed = api.post("/api/v1/reviews/run").json()
    assert failed["status"] == "failed" and failed["error"] == "down"
    reviewer.result = empty_output()
    assert api.post("/api/v1/reviews/run").json()["status"] == "ready"


def test_web_requires_login(client: TestClient) -> None:
    for method, path in (("get", "/reviews"), ("get", "/reviews/1"), ("post", "/reviews/run")):
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_web_generate_and_read_a_review(web: TestClient, api: TestClient, app: FastAPI) -> None:
    fit = api.post("/api/v1/goals", json={"title": "Get fit"}).json()["id"]
    pato = api.post("/api/v1/people", json={"display_name": "Patrik Veselý"}).json()["id"]
    promise_id = api.post(
        "/api/v1/promises",
        json={"person_id": pato, "direction": "i_promised", "text": "send the plan"},
    ).json()["id"]
    answer = empty_output(
        summary="You kept your word but skipped the gym.",
        conflicts=[
            ReviewConflict(
                title="Exam vs gym",
                detail="Thursday exam, five sessions planned",
                suggestion="Drop two",
                goal_ids=[fit],
                person_ids=[pato],
                meeting_ids=[],
                promise_ids=[promise_id],
            )
        ],
        priorities=[ReviewPriority(text="Study first", why="exam", goal_id=fit)],
        proposals=proposals_of(
            promise_dues=[
                PromiseDueIdea(promise_id=promise_id, due=date(2026, 12, 1), reason="exam week")
            ]
        ),
    )
    use_reviewer(app, FakeReviewer(answer))
    assert "Generate now" in web.get("/reviews").text
    page = web.post("/reviews/run", follow_redirects=True)
    assert page.status_code == 200
    for expected in (
        "You kept your word",
        "Exam vs gym",
        "Drop two",
        "Study first",
        "Get fit",
        "Patrik Veselý",
        "send the plan",
        "test-model",
        "In your Inbox",
        "New due date",
    ):
        assert expected in page.text, expected
    assert "Week of" in web.get("/reviews").text
    assert "Weekly review" in web.get("/").text  # Home links to it
    again = web.post("/reviews/run", follow_redirects=True)
    assert "already made" in again.text


def test_web_shows_a_failed_review_with_try_again(web: TestClient, app: FastAPI) -> None:
    use_reviewer(app, FakeReviewer(ClaudeError("Claude API error: Timeout")))
    page = web.post("/reviews/run", follow_redirects=True)
    assert "Claude API error: Timeout" in page.text and "Try again" in page.text


def test_web_without_claude(web: TestClient, app: FastAPI) -> None:
    use_reviewer(app, None)
    assert "Claude isn't connected" in web.get("/reviews").text
    assert "no weekly review" in web.post("/reviews/run", follow_redirects=True).text


def test_the_review_page_is_in_the_inbox_labels(web: TestClient) -> None:
    assert "Review" in web.get("/").text


def test_settings_default(settings: Any) -> None:
    assert settings.weekly_review_enabled is True


def test_goal_status_is_unused_here() -> None:
    assert GoalStatus.ACTIVE.value == "active"
    assert Goal.__tablename__ == "goal"
