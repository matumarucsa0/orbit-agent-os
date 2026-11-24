import copy
import json
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from orbit.services.context import ContextFocus, ContextWindow
from orbit.services.context.fitness import FitnessProvider
from orbit.services.fitness import sync_workouts

POSTS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "liftoff" / "post_getMyPosts.json").read_text()
)
PRAGUE = ZoneInfo("Europe/Prague")


class FakeSource:
    def __init__(self, posts: list[dict[str, Any]]) -> None:
        self.posts = posts

    def workouts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.posts)


def window(start: date, end: date) -> ContextWindow:
    return ContextWindow(start, end, PRAGUE)


def test_empty_database_gives_an_empty_module(db: Session) -> None:
    result = FitnessProvider().build(db, window(date(2026, 2, 2), date(2026, 2, 8)))
    assert result.workouts == [] and result.last_workout is None
    assert result.days_since_last_workout is None
    assert len(result.weekly_sessions) == 8 and all(w.sessions == 0 for w in result.weekly_sessions)


def test_workouts_in_the_window_are_summarised_in_kg_and_km(db: Session) -> None:
    sync_workouts(db, FakeSource(POSTS))
    result = FitnessProvider().build(db, window(date(2026, 2, 2), date(2026, 2, 8)))
    assert result.sessions_in_window == 2  # 6 and 8 February
    assert [w.started.date() for w in result.workouts] == [date(2026, 2, 8), date(2026, 2, 6)]
    first = result.workouts[0]
    assert first.minutes == 72 and first.bodyweight_kg == 70.0
    lateral = first.exercises[0]
    assert (lateral.name, lateral.sets, lateral.reps) == ("Cable Lateral Raise", 4, 28)
    assert lateral.top_weight_kg == 7.5 and lateral.volume_kg > 0
    treadmill = first.exercises[7]
    assert treadmill.distance_km is None  # the believable-distance total skips implausible sets


def test_pattern_fields_look_back_from_the_window(db: Session) -> None:
    sync_workouts(db, FakeSource(POSTS))
    result = FitnessProvider().build(db, window(date(2026, 2, 16), date(2026, 2, 22)))
    assert result.sessions_in_window == 0
    assert result.last_workout == date(2026, 2, 8)
    assert result.days_since_last_workout == 8
    assert result.latest_bodyweight_kg == 70.0
    by_week = {w.week_start: w.sessions for w in result.weekly_sessions}
    assert by_week[date(2026, 2, 2)] == 2


def test_a_session_left_open_has_no_minutes(db: Session) -> None:
    posts = copy.deepcopy(POSTS)
    posts[0]["sessionDuration"] = "95 hours 37 minutes 33 seconds"
    sync_workouts(db, FakeSource(posts))
    result = FitnessProvider().build(db, window(date(2026, 2, 8), date(2026, 2, 8)))
    assert result.workouts[0].minutes is None


def test_a_person_focus_has_no_workouts(db: Session) -> None:
    sync_workouts(db, FakeSource(POSTS))
    result = FitnessProvider().build(
        db, window(date(2026, 2, 2), date(2026, 2, 8)), ContextFocus(person_id=1)
    )
    assert result.workouts == [] and result.sessions_in_window == 0
