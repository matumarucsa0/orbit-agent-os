"""The review sends state once and counts the collisions itself (SPEC §2.30)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from orbit.domain.enums import LessonKind, SchoolEventKind, SchoolEventSource
from orbit.domain.ledger import Debt
from orbit.domain.school import Lesson, SchoolEvent
from orbit.integrations.claude import ReportRequest, build_report_prompt, build_review_prompt
from orbit.services import review
from orbit.services.context import ContextWindow, build_context
from orbit.services.context.signals import compute_signals
from tests.test_context_providers import meeting, person, promise
from tests.test_review import PLAN, PRAGUE, SUNDAY, FakeReviewer, generate, life  # noqa: F401

REVIEW_WINDOW = ContextWindow(date(2026, 9, 28), date(2026, 10, 4), PRAGUE)
PLAN_WINDOW = ContextWindow(date(2026, 10, 5), date(2026, 10, 11), PRAGUE)


def signals(db: Session) -> Any:
    return compute_signals(
        db, review=REVIEW_WINDOW, plan=PLAN_WINDOW, today=date(2026, 10, 4), tz=PRAGUE
    )


def test_last_week_carries_only_what_depends_on_the_week(
    db: Session,
    life: dict[str, Any],  # noqa: F811
) -> None:
    reviewer = FakeReviewer()
    generate(db, reviewer)
    (request,) = reviewer.requests
    last, ahead = json.loads(request.last_week_json), json.loads(request.next_week_json)
    assert set(last) >= {"people", "meetings", "school", "window", "budget"}
    assert not set(last) & {"goals", "ledger", "promises", "omitted"}
    assert set(ahead) >= {"goals", "ledger", "promises", "people", "meetings", "school"}
    assert json.loads(request.signals_json)["net_money"]
    prompt = build_review_prompt(request)
    assert "signals (exact, computed by Orbit):" in prompt


def test_the_stored_input_still_validates_for_checking_the_answer(
    db: Session,
    life: dict[str, Any],  # noqa: F811
) -> None:
    row = generate(db, FakeReviewer())
    assert row.input_json is not None and "signals" in row.input_json
    assert row.input_json["last_week"]["omitted"] == ["goals", "ledger", "promises"]
    assert row.status.value == "ready"


def test_the_review_input_is_smaller_than_two_full_snapshots(
    db: Session,
    life: dict[str, Any],  # noqa: F811
) -> None:
    reviewer = FakeReviewer()
    generate(db, reviewer)
    (request,) = reviewer.requests
    now = SUNDAY
    old = len(
        build_context(db, REVIEW_WINDOW, now=now).model_dump_json()
        + build_context(db, PLAN_WINDOW, now=now).model_dump_json()
    )
    new = len(request.last_week_json) + len(request.next_week_json) + len(request.signals_json)
    assert new < old


def test_overdue_money_and_goals_are_counted(db: Session, life: dict[str, Any]) -> None:  # noqa: F811
    promise(db, life["terka"], "send the notes", due=date(2026, 10, 1))
    debt = db.query(Debt).first()
    assert debt is not None
    debt.due = date(2026, 9, 30)
    db.flush()
    found = signals(db)
    assert found.overdue.count == 2
    assert any("send the notes" in line and "4 days over" in line for line in found.overdue.lines)
    assert any(line.startswith("debt #") and "5 days over" in line for line in found.overdue.lines)
    assert found.net_money == ["EUR: owed to you 29.00, you owe 0.00, net +29.00"]


def test_goals_with_nothing_linked(db: Session, life: dict[str, Any]) -> None:  # noqa: F811
    found = signals(db)
    assert found.goals_with_nothing_linked.count == 1
    assert "Save 5000" in found.goals_with_nothing_linked.lines[0]  # Get fit has Pato linked


def test_a_meeting_over_a_lesson_or_an_exam_is_a_clash(db: Session) -> None:
    start = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    meeting(db, "m1", "Káva s Paťom", start + timedelta(minutes=30))
    db.add(
        Lesson(
            uid="26aX_1",
            ticket_code="26aX",
            kind=LessonKind.LECTURE,
            title="Algebra",
            start_utc=start,
            end_utc=start + timedelta(hours=1, minutes=30),
        )
    )
    db.add(
        SchoolEvent(
            source=SchoolEventSource.PROPOSAL,
            kind=SchoolEventKind.EXAM,
            title="Zápočet",
            start_utc=start + timedelta(minutes=45),
            all_day=False,
        )
    )
    db.flush()
    found = signals(db)
    assert found.clashes.count == 2
    assert "Algebra" in found.clashes.lines[0] or "Algebra" in found.clashes.lines[1]
    assert any("exam" in line for line in found.clashes.lines)


def test_three_deadlines_in_two_days_are_bunched(db: Session) -> None:
    pato = person(db, "Paťo")
    for n, day in enumerate((6, 7, 7)):
        promise(db, pato, f"thing {n}", due=date(2026, 10, day))
    promise(db, pato, "far away", due=date(2026, 10, 11))
    found = signals(db)
    assert found.bunched_deadlines.count == 1
    assert found.bunched_deadlines.lines[0].startswith("3 within 48 h")


def test_people_past_their_cadence(db: Session) -> None:
    ola = person(db, "Oľga", cadence_days=7)
    ola.created_at = datetime(2026, 8, 1, tzinfo=UTC)
    db.flush()
    found = signals(db)
    assert found.people_past_cadence.count == 1
    assert "Oľga" in found.people_past_cadence.lines[0]


def test_an_empty_database_has_empty_signals(db: Session) -> None:
    found = signals(db)
    assert found.model_dump() == {
        "clashes": {"count": 0, "lines": []},
        "overdue": {"count": 0, "lines": []},
        "people_past_cadence": {"count": 0, "lines": []},
        "net_money": [],
        "bunched_deadlines": {"count": 0, "lines": []},
        "goals_with_nothing_linked": {"count": 0, "lines": []},
    }
    assert review.LAST_WEEK_MODULES == ("people", "meetings", "school", "fitness", "health")


def test_the_report_prompt_includes_signals() -> None:
    request = ReportRequest(
        today=date(2026, 10, 8),
        start=date(2026, 10, 1),
        end=date(2026, 10, 7),
        snapshot_json="{}",
        signals_json='{"net_money":[]}',
    )
    assert "signals (exact, computed by Orbit):" in build_report_prompt(request)
    bare = ReportRequest(
        today=date(2026, 10, 8), start=date(2026, 10, 1), end=date(2026, 10, 7), snapshot_json="{}"
    )
    assert "signals" not in build_report_prompt(bare)


def test_activity_only_lists_just_the_people_met_in_the_window(db: Session) -> None:
    from orbit.services import interactions
    from orbit.services.context import ContextFocus

    met = person(db, "Met Friend")
    person(db, "Owes Money")  # has an open item, but no activity in the window
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(occurred_on=date(2026, 10, 1), person_ids=[met.id]),
    )
    snapshot = build_context(
        db,
        REVIEW_WINDOW,
        ContextFocus(activity_only=True),
        only=("people",),
    )
    assert [p.name for p in snapshot.people.people] == ["Met Friend"]
    assert snapshot.omitted == (
        "goals",
        "school",
        "ledger",
        "meetings",
        "promises",
        "fitness",
        "health",
    )
    assert "goals" not in snapshot.prompt_json()
