"""Fitness: gym workouts from Liftoff in the window, and the training pattern around it.

Read-only. Weights are kg, distances km, durations minutes (a session left open in the app has
`minutes: None`). With a person focus the module is empty: workouts do not belong to people.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.fitness import Workout, WorkoutExercise, WorkoutSet
from orbit.services.context.base import (
    NAME_CHARS,
    ContextFocus,
    ContextWindow,
    ModuleContext,
    text,
)

MAX_WORKOUTS = 100
TREND_WEEKS = 8


def kg(grams: int | None) -> float | None:
    return None if grams is None else round(grams / 1000, 1)


class ExerciseSummary(BaseModel):
    name: str
    kind: str
    sets: int
    reps: int
    top_weight_kg: float | None
    volume_kg: float  # sum of weight x reps over normal and drop sets
    distance_km: float | None  # believable distance only
    notes: str | None


class WorkoutContext(BaseModel):
    id: int
    started: datetime  # local time
    minutes: int | None
    bodyweight_kg: float | None
    pr_count: int
    notes: str | None
    exercises: list[ExerciseSummary]


class WeekCount(BaseModel):
    week_start: date  # Monday
    sessions: int


class FitnessContext(ModuleContext):
    trimmable: ClassVar[tuple[str, ...]] = ("workouts", "weekly_sessions")
    module: Literal["fitness"] = "fitness"
    workouts: list[WorkoutContext] = Field(default_factory=list)
    sessions_in_window: int = 0
    last_workout: date | None = None
    days_since_last_workout: int | None = None
    latest_bodyweight_kg: float | None = None
    weekly_sessions: list[WeekCount] = Field(
        default_factory=list
    )  # the last weeks, oldest first, zeros included


def _summary(exercise: WorkoutExercise) -> ExerciseSummary:
    sets: list[WorkoutSet] = exercise.sets
    weighted = [s for s in sets if s.weight_grams and s.reps]
    metres = sum(s.distance_m or 0 for s in sets if s.distance_m and not s.implausible)
    return ExerciseSummary(
        name=text(exercise.name, NAME_CHARS),
        kind=exercise.kind,
        sets=len(sets),
        reps=sum(s.reps or 0 for s in sets),
        top_weight_kg=kg(max((s.weight_grams or 0 for s in weighted), default=0) or None),
        volume_kg=round(sum((s.weight_grams or 0) * (s.reps or 0) for s in weighted) / 1000, 1),
        distance_km=round(metres / 1000, 2) if metres else None,
        notes=text(exercise.notes) if exercise.notes else None,
    )


class FitnessProvider:
    name = "fitness"

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> FitnessContext:
        if focus is not None and focus.person_id is not None:
            return FitnessContext()
        rows = list(
            session.scalars(
                select(Workout)
                .where(Workout.started_at >= window.start_utc, Workout.started_at < window.end_utc)
                .options(selectinload(Workout.exercises).selectinload(WorkoutExercise.sets))
                .order_by(Workout.started_at.desc(), Workout.id.desc())
                .limit(MAX_WORKOUTS + 1)
            )
        )
        truncated = len(rows) > MAX_WORKOUTS
        workouts = [
            WorkoutContext(
                id=w.id,
                started=w.started_at.astimezone(window.tz),
                minutes=None if w.duration_suspect else round(w.duration_seconds / 60),
                bodyweight_kg=kg(w.bodyweight_grams),
                pr_count=w.pr_count,
                notes=text(w.notes) if w.notes else None,
                exercises=[_summary(e) for e in w.exercises],
            )
            for w in rows[:MAX_WORKOUTS]
        ]
        latest = session.scalars(
            select(Workout).order_by(Workout.started_at.desc(), Workout.id.desc()).limit(1)
        ).first()
        weigh = session.scalars(
            select(Workout)
            .where(Workout.bodyweight_grams.is_not(None))
            .order_by(Workout.started_at.desc(), Workout.id.desc())
            .limit(1)
        ).first()
        last_day = latest.started_at.astimezone(window.tz).date() if latest else None
        return FitnessContext(
            truncated=truncated,
            workouts=workouts,
            sessions_in_window=session.query(Workout)
            .filter(Workout.started_at >= window.start_utc, Workout.started_at < window.end_utc)
            .count(),
            last_workout=last_day,
            days_since_last_workout=(window.start - last_day).days if last_day else None,
            latest_bodyweight_kg=kg(weigh.bodyweight_grams) if weigh else None,
            weekly_sessions=self._weeks(session, window),
        )

    @staticmethod
    def _weeks(session: Session, window: ContextWindow) -> list[WeekCount]:
        this_monday = window.start - timedelta(days=window.start.weekday())
        first = this_monday - timedelta(weeks=TREND_WEEKS - 1)
        start = datetime.combine(first, datetime.min.time(), window.tz)
        starts = session.scalars(select(Workout.started_at).where(Workout.started_at >= start))
        counts: dict[date, int] = {first + timedelta(weeks=i): 0 for i in range(TREND_WEEKS)}
        for moment in starts:
            day = moment.astimezone(window.tz).date()
            monday = day - timedelta(days=day.weekday())
            if monday in counts:
                counts[monday] += 1
        return [WeekCount(week_start=w, sessions=n) for w, n in counts.items()]
