"""Fitness: sync Liftoff workouts into Orbit (SPEC §2.35).

Read-only mirror. A workout is upserted by its Liftoff id; one whose parsed content is unchanged
is skipped, a changed one is rebuilt. Workouts that vanish from Liftoff are kept. A post that
cannot be parsed is counted and skipped, never fatal. Callers commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.fitness import Workout, WorkoutExercise, WorkoutSet
from orbit.integrations.liftoff_parse import LiftoffParseError, ParsedWorkout, parse_post
from orbit.services.errors import NotFoundError

log = logging.getLogger(__name__)


class WorkoutSource(Protocol):
    def workouts(self) -> list[dict[str, Any]]: ...


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = f"{self.created} new, {self.updated} changed, {self.unchanged} unchanged"
        if self.skipped:
            text += f", {len(self.skipped)} unreadable"
        return text


def _fill(workout: Workout, parsed: ParsedWorkout) -> None:
    workout.started_at = parsed.started_at
    workout.posted_at = parsed.posted_at
    workout.duration_seconds = parsed.duration_seconds
    workout.duration_suspect = parsed.duration_suspect
    workout.bodyweight_grams = parsed.bodyweight_grams
    workout.calories = parsed.calories
    workout.pr_count = parsed.pr_count
    workout.notes = parsed.notes
    workout.content_hash = parsed.content_hash
    workout.exercises = [
        WorkoutExercise(
            position=exercise.position,
            liftoff_exercise_id=exercise.liftoff_exercise_id,
            name=exercise.name,
            kind=exercise.kind,
            notes=exercise.notes,
            sets=[
                WorkoutSet(
                    position=s.position,
                    set_type=s.set_type,
                    weight_grams=s.weight_grams,
                    reps=s.reps,
                    distance_m=s.distance_m,
                    seconds=s.seconds,
                    implausible=s.implausible,
                )
                for s in exercise.sets
            ],
        )
        for exercise in parsed.exercises
    ]


def sync_workouts(session: Session, source: WorkoutSource) -> SyncReport:
    report = SyncReport()
    existing = {w.liftoff_id: w for w in session.scalars(select(Workout))}
    for raw in source.workouts():
        try:
            parsed = parse_post(raw)
        except LiftoffParseError as exc:
            report.skipped.append(f"{raw.get('id', '?')}: {exc}")
            log.warning("liftoff post %s skipped: %s", raw.get("id", "?"), exc)
            continue
        workout = existing.get(parsed.liftoff_id)
        if workout is None:
            workout = Workout(liftoff_id=parsed.liftoff_id)
            session.add(workout)
            _fill(workout, parsed)
            existing[parsed.liftoff_id] = workout
            report.created += 1
        elif workout.content_hash != parsed.content_hash:
            _fill(workout, parsed)
            report.updated += 1
        else:
            report.unchanged += 1
    session.flush()
    return report


def recent_workouts(session: Session, limit: int = 60) -> list[Workout]:
    """Newest first, with exercises and sets loaded."""
    return list(
        session.scalars(
            select(Workout)
            .options(selectinload(Workout.exercises).selectinload(WorkoutExercise.sets))
            .order_by(Workout.started_at.desc(), Workout.id.desc())
            .limit(limit)
        )
    )


def get_workout(session: Session, workout_id: int) -> Workout:
    workout = session.scalars(
        select(Workout)
        .where(Workout.id == workout_id)
        .options(selectinload(Workout.exercises).selectinload(WorkoutExercise.sets))
    ).first()
    if workout is None:
        raise NotFoundError(f"No workout {workout_id}.")
    return workout


def total_workouts(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Workout)) or 0
