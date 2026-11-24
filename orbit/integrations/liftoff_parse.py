"""Liftoff `post.getMyPosts` -> typed workouts (SPEC §2.35).

Built from a real response (`tests/fixtures/liftoff/post_getMyPosts.json`). Verified there:
- Weights and bodyweight are in **pounds** (30 kg arrives as 66.1386786555); `inputOne` is a
  float or int, `inputTwo` an int, `bodyweight` a string. Stored here as grams.
- `DD` distance (`inputOne`) is **km**, `inputTwo` seconds. A set faster than `MAX_KMH` is
  flagged `implausible`.
- `sessionDuration` is "NN hours NN minutes NN seconds", sometimes absurd (sessions left open).
Fields without training value (media, XP, eggs, streaks...) are ignored.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

GRAMS_PER_POUND = 453.59237
MAX_KMH = 25.0  # a faster treadmill set is not believable
SUSPECT_SECONDS = 6 * 3600  # a session longer than this was left open
_DURATION_RE = re.compile(r"(\d+)\s*hours?\s+(\d+)\s*minutes?\s+(\d+)\s*seconds?", re.IGNORECASE)


class LiftoffParseError(ValueError):
    """A post that does not look like a workout; the message says which field."""


def pounds_to_grams(pounds: float) -> int:
    return round(pounds * GRAMS_PER_POUND)


def parse_duration(text: str) -> int:
    """ "01 hours 06 minutes 01 seconds" -> 3961."""
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None:
        raise LiftoffParseError(f"unreadable session duration {text!r}")
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("not a number")
    return float(value)


class _Set(BaseModel):
    model_config = ConfigDict(extra="ignore")

    setIndex: int = 0
    setType: str = "normal"
    inputOne: float = 0.0
    inputTwo: float = 0.0

    @field_validator("inputOne", "inputTwo", mode="before")
    @classmethod
    def _numeric(cls, value: Any) -> float:
        return 0.0 if value in (None, "") else _number(value)


class _Exercise(BaseModel):
    model_config = ConfigDict(extra="ignore")

    exerciseIndex: int = 0
    exerciseName: str
    exerciseId: str = ""
    exerciseTypes: str = ""
    exerciseNotes: str | None = None
    setsData: list[_Set] = []


class _Post(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    startedAt: datetime
    postedAt: datetime | None = None
    sessionDuration: str
    sessionNotes: str | None = None
    bodyweight: str | float | None = None
    caloriesBurned: int = 0
    prCount: int = 0
    exerciseData: list[_Exercise] = []


@dataclass(frozen=True)
class ParsedSet:
    position: int
    set_type: str
    weight_grams: int | None
    reps: int | None
    distance_m: int | None
    seconds: int | None
    implausible: bool


@dataclass(frozen=True)
class ParsedExercise:
    position: int
    liftoff_exercise_id: str
    name: str
    kind: str
    notes: str | None
    sets: list[ParsedSet] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedWorkout:
    liftoff_id: str
    started_at: datetime
    posted_at: datetime | None
    duration_seconds: int
    duration_suspect: bool
    bodyweight_grams: int | None
    calories: int
    pr_count: int
    notes: str | None
    exercises: list[ParsedExercise] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        """Stable hash of everything stored, to tell a changed workout from an unchanged one."""
        payload = json.dumps(asdict(self), default=str, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bodyweight(value: str | float | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        pounds = float(value)
    except ValueError:
        return None
    return pounds_to_grams(pounds) if pounds > 0 else None


def _clean(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


def _parse_set(position: int, kind: str, raw: _Set) -> ParsedSet:
    if kind == "DD":
        metres = round(raw.inputOne * 1000)
        seconds = round(raw.inputTwo)
        too_fast = seconds > 0 and metres / seconds * 3.6 > MAX_KMH
        return ParsedSet(position, raw.setType, None, None, metres, seconds, too_fast)
    # WR, AB and anything unknown: weight then reps. BR has no weight (`inputOne` is 0).
    weight = pounds_to_grams(raw.inputOne) if kind != "BR" and raw.inputOne > 0 else None
    return ParsedSet(position, raw.setType, weight, round(raw.inputTwo), None, None, False)


def parse_post(data: dict[str, Any]) -> ParsedWorkout:
    try:
        post = _Post.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise LiftoffParseError(f"{where}: {first['msg']}") from None
    seconds = parse_duration(post.sessionDuration)
    exercises = [
        ParsedExercise(
            position=index,
            liftoff_exercise_id=raw.exerciseId,
            name=raw.exerciseName.strip() or "(exercise)",
            kind=raw.exerciseTypes,
            notes=_clean(raw.exerciseNotes),
            sets=[
                _parse_set(position, raw.exerciseTypes, s)
                for position, s in enumerate(raw.setsData)
            ],
        )
        for index, raw in enumerate(post.exerciseData)
    ]
    return ParsedWorkout(
        liftoff_id=post.id,
        started_at=_utc(post.startedAt),
        posted_at=_utc(post.postedAt) if post.postedAt else None,
        duration_seconds=seconds,
        duration_suspect=seconds > SUSPECT_SECONDS,
        bodyweight_grams=_bodyweight(post.bodyweight),
        calories=post.caloriesBurned,
        pr_count=post.prCount,
        notes=_clean(post.sessionNotes),
        exercises=exercises,
    )
