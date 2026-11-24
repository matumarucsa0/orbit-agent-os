"""Fitness: gym workouts read from Liftoff (SPEC §2.35). Liftoff is the source: Orbit's copy is
read-only and is rebuilt when a workout changes there."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, TimestampMixin


class Workout(TimestampMixin, Base):
    """One gym session, upserted by `liftoff_id`. Weights are grams, distances metres."""

    __tablename__ = "workout"

    id: Mapped[int] = mapped_column(primary_key=True)
    liftoff_id: Mapped[str] = mapped_column(String(100), unique=True)
    started_at: Mapped[datetime] = mapped_column(index=True)
    posted_at: Mapped[datetime | None]
    duration_seconds: Mapped[int]
    # A session left open in the app (over `SUSPECT_SECONDS`): kept as given, never trusted.
    duration_suspect: Mapped[bool] = mapped_column(default=False)
    bodyweight_grams: Mapped[int | None]
    calories: Mapped[int] = mapped_column(default=0)
    pr_count: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    # Hash of the parsed workout: unchanged means nothing to rewrite.
    content_hash: Mapped[str] = mapped_column(String(64))

    exercises: Mapped[list[WorkoutExercise]] = relationship(
        back_populates="workout",
        cascade="all, delete-orphan",
        order_by="WorkoutExercise.position",
    )


class WorkoutExercise(Base):
    __tablename__ = "workout_exercise"

    id: Mapped[int] = mapped_column(primary_key=True)
    workout_id: Mapped[int] = mapped_column(
        ForeignKey("workout.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    liftoff_exercise_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    # Liftoff's type: WR weight and reps, BR bodyweight reps, AB assisted or added bodyweight,
    # DD distance and time; anything else is kept as given.
    kind: Mapped[str] = mapped_column(String(10))
    notes: Mapped[str | None] = mapped_column(Text)

    workout: Mapped[Workout] = relationship(back_populates="exercises")
    sets: Mapped[list[WorkoutSet]] = relationship(
        back_populates="exercise", cascade="all, delete-orphan", order_by="WorkoutSet.position"
    )


class WorkoutSet(Base):
    __tablename__ = "workout_set"

    id: Mapped[int] = mapped_column(primary_key=True)
    exercise_id: Mapped[int] = mapped_column(
        ForeignKey("workout_exercise.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    set_type: Mapped[str] = mapped_column(String(20), default="normal")  # normal, drop
    weight_grams: Mapped[int | None]
    reps: Mapped[int | None]
    distance_m: Mapped[int | None]
    seconds: Mapped[int | None]
    # Distance that cannot be true (faster than `MAX_KMH`): kept, left out of totals.
    implausible: Mapped[bool] = mapped_column(default=False)

    exercise: Mapped[WorkoutExercise] = relationship(back_populates="sets")
