"""How big is the weekly review's input? Seeds a throwaway in-memory database with data about the
size of the 2026-10-07 audit (400 people, 2400 notes, 1500 interactions, 800 debts, 400 promises,
1200 meetings) and prints the characters sent before (two full snapshots) and now (SPEC §2.30).

    uv run python scripts/measure_review_prompt.py
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from orbit.db import make_engine, make_sessionmaker
from orbit.domain import Base
from orbit.domain.enums import DebtDirection, DebtType, PromiseDirection
from orbit.domain.meetings import Meeting
from orbit.integrations.claude import build_review_prompt
from orbit.services import goals, interactions, ledger, people, promises, review
from orbit.services.context import ContextWindow, build_context

TZ = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 10, 4, 17, 30, tzinfo=UTC)
TODAY = date(2026, 10, 4)
PEOPLE, NOTES, INTERACTIONS, DEBTS, PROMISES, MEETINGS = 400, 2400, 1500, 800, 400, 1200


def seed(db) -> None:  # type: ignore[no-untyped-def]
    rng = random.Random(7)
    everyone = [
        people.create_person(db, people.PersonCreate(display_name=f"Person Number{n:03}"))
        for n in range(PEOPLE)
    ]
    for _ in range(NOTES):
        people.add_note(db, rng.choice(everyone), "A note about something that happened " * 3)
    for _ in range(INTERACTIONS):
        who = rng.choice(everyone)
        day = TODAY - timedelta(days=rng.randrange(0, 120))
        interactions.create_interaction(
            db,
            interactions.InteractionCreate(occurred_on=day, person_ids=[who.id], summary="met"),
        )
    for n in range(DEBTS):
        ledger.create_debt(
            db,
            ledger.DebtCreate(
                person_id=rng.choice(everyone).id,
                direction=rng.choice(list(DebtDirection)),
                type=DebtType.MONEY,
                amount_minor=rng.randrange(100, 9000),
                currency="EUR",
                description=f"thing {n}",
            ),
            "EUR",
        )
    for n in range(PROMISES):
        promises.create_promise(
            db,
            promises.PromiseCreate(
                person_id=rng.choice(everyone).id,
                direction=rng.choice(list(PromiseDirection)),
                text=f"promise number {n}",
                due=TODAY + timedelta(days=rng.randrange(-30, 30)),
            ),
            TODAY - timedelta(days=40),
        )
    for n in range(MEETINGS):
        start = NOW + timedelta(days=rng.randrange(-60, 60), hours=rng.randrange(0, 12))
        db.add(
            Meeting(
                calendar_uid=f"u{n}",
                calendar_name="Personal",
                title=f"Meeting {n}",
                start_utc=start,
                end_utc=start + timedelta(hours=1),
            )
        )
    for rank in range(5):
        goals.create_goal(db, goals.GoalCreate(title=f"Goal {rank}", why="because"), NOW)
    db.commit()


def main() -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with make_sessionmaker(engine)() as db:
        seed(db)
        row = review.claim(db, date(2026, 10, 5), NOW)
        assert row is not None
        request, _ = review.build_request(db, row, tz=TZ, budget_chars=400_000, now=NOW)
        review_window = ContextWindow(row.review_start, row.review_end, TZ)
        plan_window = ContextWindow(row.plan_start, row.plan_end, TZ)
        before = len(
            build_context(db, review_window, now=NOW).model_dump_json()
            + build_context(db, plan_window, now=NOW).model_dump_json()
        )
        after = (
            len(request.last_week_json) + len(request.next_week_json) + len(request.signals_json)
        )
        print(f"before: {before:>9,} characters  (about {before // 4:,} tokens)")
        print(f"after:  {after:>9,} characters  (about {after // 4:,} tokens)")
        print(
            f"        last_week {len(request.last_week_json):,}, next_week "
            f"{len(request.next_week_json):,}, signals {len(request.signals_json):,}"
        )
        print(f"prompt: {len(build_review_prompt(request)):,} characters in all")


if __name__ == "__main__":
    main()
