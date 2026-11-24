import copy
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.fitness import Workout, WorkoutExercise, WorkoutSet
from orbit.services.fitness import sync_workouts

POSTS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "liftoff" / "post_getMyPosts.json").read_text()
)


class FakeSource:
    def __init__(self, posts: list[dict[str, Any]]) -> None:
        self.posts = posts

    def workouts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.posts)


def count(db: Session, model: Any) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_first_sync_creates_everything(db: Session) -> None:
    report = sync_workouts(db, FakeSource(POSTS))
    assert (report.created, report.updated, report.unchanged) == (4, 0, 0)
    assert count(db, Workout) == 4
    assert count(db, WorkoutExercise) == sum(len(p["exerciseData"]) for p in POSTS)
    assert count(db, WorkoutSet) == sum(
        len(e["setsData"]) for p in POSTS for e in p["exerciseData"]
    )


def test_second_sync_changes_nothing(db: Session) -> None:
    sync_workouts(db, FakeSource(POSTS))
    ids = set(db.scalars(select(WorkoutSet.id)))
    report = sync_workouts(db, FakeSource(POSTS))
    assert (report.created, report.updated, report.unchanged) == (0, 0, 4)
    assert set(db.scalars(select(WorkoutSet.id))) == ids  # nothing was rebuilt


def test_a_changed_workout_is_rebuilt_in_place(db: Session) -> None:
    sync_workouts(db, FakeSource(POSTS))
    changed = copy.deepcopy(POSTS)
    changed[3]["exerciseData"][0]["setsData"][0]["inputTwo"] = 30
    report = sync_workouts(db, FakeSource(changed))
    assert (report.created, report.updated, report.unchanged) == (0, 1, 3)
    assert count(db, Workout) == 4
    workout = db.scalars(select(Workout).where(Workout.liftoff_id == "post-004")).one()
    assert workout.exercises[0].sets[0].reps == 30
    assert count(db, WorkoutSet) == sum(
        len(e["setsData"]) for p in POSTS for e in p["exerciseData"]
    )  # the old sets are gone, not duplicated


def test_a_workout_that_vanishes_from_liftoff_is_kept(db: Session) -> None:
    sync_workouts(db, FakeSource(POSTS))
    sync_workouts(db, FakeSource(POSTS[:1]))
    assert count(db, Workout) == 4


def test_an_unreadable_post_is_skipped_not_fatal(db: Session) -> None:
    broken = copy.deepcopy(POSTS)
    broken[0]["sessionDuration"] = "soon"
    del broken[1]["startedAt"]
    report = sync_workouts(db, FakeSource(broken))
    assert (report.created, len(report.skipped)) == (2, 2)
    assert "2 unreadable" in report.summary
    assert count(db, Workout) == 2
