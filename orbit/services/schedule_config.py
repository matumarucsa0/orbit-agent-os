"""Schedule settings that can be edited in the app (SPEC §2.21).

A value is read as: stored override (`AppSetting`) > env (`Settings`) > code default. Every
editable value is a `Settings` field, so the services keep reading `Settings`; `effective` returns
a copy of it with the overrides applied. A stored value is in canonical JSON form (an int, a bool,
"HH:MM", [7, 1], "HH:MM-HH:MM"). Callers commit.
"""

from __future__ import annotations

import logging
import re
import threading
import time as clock
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from orbit.domain.app_settings import AppSetting, AppSettingLog
from orbit.domain.base import utcnow
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.settings import Settings

log = logging.getLogger(__name__)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_QUIET_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$")


class Kind(StrEnum):
    INT = "int"
    BOOL = "bool"
    TIME = "time"
    WEEKDAY = "weekday"
    DAYS = "days"  # a list of day counts: "7, 1"
    QUIET = "quiet"  # "22:00-08:00"


@dataclass(frozen=True)
class SettingDef:
    key: str  # the Settings field
    group: str
    label: str
    kind: Kind
    help: str = ""
    unit: str = ""
    low: int = 0  # INT, WEEKDAY: smallest value; DAYS: smallest day count
    high: int = 0  # INT, WEEKDAY: largest value; DAYS: largest day count
    job: str | None = None  # the scheduled job whose interval this value sets

    @property
    def choices(self) -> list[tuple[int, str]]:
        return [(day, WEEKDAYS[day]) for day in range(self.low, self.high + 1)]


GROUPS = (
    "Calendar",
    "Reminders",
    "Weekly review",
    "School",
    "Fitness",
    "Notifications",
    "Housekeeping",
)

DEFINITIONS: tuple[SettingDef, ...] = (
    SettingDef(
        "caldav_poll_minutes",
        "Calendar",
        "Read the calendar every",
        Kind.INT,
        "How often Orbit pulls meetings from iCloud.",
        "min",
        1,
        1440,
        job="sync_calendar",
    ),
    SettingDef(
        "brief_minutes_before",
        "Calendar",
        "Meeting brief before the start",
        Kind.INT,
        "Push with the brief of a linked meeting.",
        "min",
        0,
        720,
    ),
    SettingDef(
        "capture_minutes_after",
        "Calendar",
        "Capture prompt after the end",
        Kind.INT,
        "Push that asks what was agreed.",
        "min",
        0,
        720,
    ),
    SettingDef(
        "reminder_time",
        "Reminders",
        "Daily reminders from",
        Kind.TIME,
        "Birthdays, name days, lent items and promises go out from this time.",
    ),
    SettingDef(
        "reminder_close_days_ahead",
        "Reminders",
        "Close contacts: heads-up",
        Kind.INT,
        "Birthday heads-up for people tagged close.",
        "days ahead",
        1,
        30,
    ),
    SettingDef(
        "item_reminder_days",
        "Reminders",
        "Ask for a lent item back after",
        Kind.INT,
        "Default for items without their own reminder date.",
        "days",
        1,
        365,
    ),
    SettingDef("digest_weekday", "Reminders", "Digest day", Kind.WEEKDAY, "", "", 0, 6),
    SettingDef("digest_time", "Reminders", "Digest from", Kind.TIME),
    SettingDef("weekly_review_enabled", "Weekly review", "Weekly review", Kind.BOOL),
    SettingDef(
        "weekly_review_weekday",
        "Weekly review",
        "Review starts on",
        Kind.WEEKDAY,
        "Friday to Sunday; it plans the next week.",
        "",
        4,
        6,
    ),
    SettingDef("weekly_review_time", "Weekly review", "Review starts at", Kind.TIME),
    SettingDef(
        "weekly_review_catch_up_weekday",
        "Weekly review",
        "A missed review is made until",
        Kind.WEEKDAY,
        "Monday to Thursday.",
        "",
        0,
        3,
    ),
    SettingDef(
        "sis_poll_minutes",
        "School",
        "Read SIS, ReCodEx and Owl every",
        Kind.INT,
        "At least 15.",
        "min",
        15,
        1440,
        job="sis_sync",
    ),
    SettingDef(
        "liftoff_poll_hours",
        "Fitness",
        "Read Liftoff workouts every",
        Kind.INT,
        "Between 1 and 168 hours.",
        "h",
        1,
        168,
        job="liftoff_sync",
    ),
    SettingDef("school_pushes_enabled", "School", "School pushes", Kind.BOOL),
    SettingDef(
        "school_evening_time",
        "School",
        "Tomorrow's lessons from",
        Kind.TIME,
    ),
    SettingDef(
        "school_reminder_time",
        "School",
        "Exam and deadline reminders from",
        Kind.TIME,
    ),
    SettingDef(
        "school_exam_days",
        "School",
        "Exam reminders",
        Kind.DAYS,
        "Days before the exam, comma separated.",
        "days before",
        1,
        60,
    ),
    SettingDef(
        "school_deadline_days",
        "School",
        "Deadline reminders",
        Kind.DAYS,
        "Days before the deadline, comma separated.",
        "days before",
        1,
        60,
    ),
    SettingDef(
        "school_change_gap_minutes",
        "School",
        "Schedule-change pushes at most every",
        Kind.INT,
        "",
        "min",
        1,
        1440,
    ),
    SettingDef(
        "school_sites_hour",
        "School",
        "Check course pages from",
        Kind.INT,
        "Local hour; each kept page is fetched once a day.",
        "o'clock",
        0,
        23,
    ),
    SettingDef(
        "school_reads_per_day",
        "School",
        "Claude reads of course pages per day",
        Kind.INT,
        "Protects the subscription; the rest wait for the next day.",
        "reads",
        1,
        200,
    ),
    SettingDef(
        "school_token_warn_days",
        "School",
        "Warn about the ReCodEx token",
        Kind.INT,
        "",
        "days before it ends",
        1,
        90,
    ),
    SettingDef(
        "quiet_hours",
        "Notifications",
        "Quiet hours",
        Kind.QUIET,
        "Pushes wait until they end (meeting briefs excepted). Same time twice = none.",
    ),
    SettingDef(
        "notification_retry_minutes",
        "Notifications",
        "Retry a failed push after",
        Kind.INT,
        "",
        "min",
        1,
        120,
    ),
    SettingDef(
        "notification_max_attempts",
        "Notifications",
        "Give up after",
        Kind.INT,
        "",
        "attempts",
        1,
        20,
    ),
    SettingDef(
        "notification_max_age_hours",
        "Notifications",
        "Drop a push older than",
        Kind.INT,
        "",
        "hours",
        1,
        168,
    ),
    SettingDef(
        "claude_job_retention_days",
        "Housekeeping",
        "Keep Claude prompts and answers",
        Kind.INT,
        "",
        "days",
        1,
        365,
    ),
    SettingDef(
        "contact_backup_keep_days",
        "Housekeeping",
        "Keep saved Apple cards",
        Kind.INT,
        "",
        "days",
        1,
        3650,
    ),
    SettingDef(
        "backup_prune_hours",
        "Housekeeping",
        "Clear old Apple cards every",
        Kind.INT,
        "",
        "hours",
        1,
        168,
        job="prune_contact_backups",
    ),
    SettingDef(
        "claude_prune_hours",
        "Housekeeping",
        "Clear old Claude jobs every",
        Kind.INT,
        "",
        "hours",
        1,
        168,
        job="prune_claude_jobs",
    ),
)

_BY_KEY = {d.key: d for d in DEFINITIONS}


@dataclass(frozen=True)
class ExternalRow:
    """A schedule that lives outside the app process: shown, not editable."""

    label: str
    value: str
    where: str


EXTERNAL_ROWS: tuple[ExternalRow, ...] = (
    ExternalRow(
        "Database backup",
        "daily at BACKUP_TIME (03:30 by default), kept BACKUP_KEEP_DAYS / BACKUP_KEEP_LOCAL_DAYS",
        "`backup` container: edit .env, restart it (docs/DEPLOY.md §7)",
    ),
    ExternalRow(
        "Weekly review timer",
        "hourly at :05",
        "deploy/orbit-review.timer (systemd; off when the Claude worker runs)",
    ),
    ExternalRow(
        "Claude worker",
        "always on, asks Orbit for work",
        "deploy/orbit-claude-worker.service (systemd)",
    ),
)


def definition(key: str) -> SettingDef:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise NotFoundError(f"Unknown setting {key!r}.") from None


# --- Values: parsing, canonical form, Settings form ---


def _parse_time(text: str, label: str) -> str:
    match = _TIME_RE.match(text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise InvalidInputError(f"{label}: use a time like 09:00.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def parse(defn: SettingDef, raw: object) -> Any:
    """A raw value (form text, JSON) in canonical form; InvalidInputError when not valid."""
    label = defn.label
    match defn.kind:
        case Kind.BOOL:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in ("1", "true", "on", "yes"):
                return True
            if isinstance(raw, str) and raw.strip().lower() in ("0", "false", "off", "no"):
                return False
            raise InvalidInputError(f"{label}: use on or off.")
        case Kind.INT | Kind.WEEKDAY:
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                raise InvalidInputError(f"{label}: use a whole number.")
            try:
                number = int(raw)
            except ValueError:
                raise InvalidInputError(f"{label}: use a whole number.") from None
            if not defn.low <= number <= defn.high:
                if defn.kind is Kind.WEEKDAY:
                    names = ", ".join(WEEKDAYS[d] for d in range(defn.low, defn.high + 1))
                    raise InvalidInputError(f"{label}: pick {names}.")
                raise InvalidInputError(f"{label}: between {defn.low} and {defn.high}.")
            return number
        case Kind.TIME:
            if not isinstance(raw, str):
                raise InvalidInputError(f"{label}: use a time like 09:00.")
            return _parse_time(raw, label)
        case Kind.QUIET:
            match_ = _QUIET_RE.match(raw) if isinstance(raw, str) else None
            if not match_:
                raise InvalidInputError(f"{label}: use HH:MM-HH:MM, like 22:00-08:00.")
            return f"{_parse_time(match_.group(1), label)}-{_parse_time(match_.group(2), label)}"
        case Kind.DAYS:
            parts: Sequence[Any]
            if isinstance(raw, str):
                parts = [p.strip() for p in raw.split(",") if p.strip()]
            elif isinstance(raw, list):
                parts = raw
            else:
                parts = []
            try:
                days = [int(p) for p in parts]
            except (TypeError, ValueError):
                raise InvalidInputError(f"{label}: use day counts like 7, 1.") from None
            if not days or any(not defn.low <= d <= defn.high for d in days):
                raise InvalidInputError(
                    f"{label}: one or more day counts between {defn.low} and {defn.high}."
                )
            return sorted(set(days), reverse=True)
    raise AssertionError(defn.kind)  # pragma: no cover


def _hhmm(value: time) -> str:
    return f"{value:%H:%M}"


def canonical(defn: SettingDef, value: Any) -> Any:
    """A `Settings` attribute value in the canonical form."""
    match defn.kind:
        case Kind.TIME:
            return _hhmm(value)
        case Kind.QUIET:
            return f"{_hhmm(value[0])}-{_hhmm(value[1])}"
        case Kind.DAYS:
            return list(value)
        case _:
            return value


def to_setting(defn: SettingDef, stored: Any) -> Any:
    """The canonical form as the `Settings` attribute value."""
    match defn.kind:
        case Kind.TIME:
            hours, minutes = stored.split(":")
            return time(int(hours), int(minutes))
        case Kind.QUIET:
            start, end = stored.split("-")
            return (to_setting(_TIME_DEF, start), to_setting(_TIME_DEF, end))
        case Kind.DAYS:
            return list(stored)
        case _:
            return stored


_TIME_DEF = SettingDef("", "", "", Kind.TIME)


def display(defn: SettingDef, stored: Any) -> str:
    """The canonical form as text for a form field or a hint."""
    match defn.kind:
        case Kind.BOOL:
            return "on" if stored else "off"
        case Kind.WEEKDAY:
            return WEEKDAYS[int(stored)]
        case Kind.DAYS:
            return ", ".join(str(d) for d in stored)
        case _:
            return str(stored)


# --- The overrides ---


def stored_overrides(session: Session) -> dict[str, Any]:
    """The overrides in the database, ignoring a key this version does not know."""
    rows = session.scalars(select(AppSetting)).all()
    return {row.key: row.value_json for row in rows if row.key in _BY_KEY}


def apply_overrides(settings: Settings, overrides: Mapping[str, Any]) -> Settings:
    """A copy of `settings` with the overrides applied. A stored value that no longer parses (the
    bounds changed in a later version) is skipped, so a bad row never breaks the app."""
    update: dict[str, Any] = {}
    for key, stored in overrides.items():
        defn = _BY_KEY.get(key)
        if defn is None:
            continue
        try:
            update[key] = to_setting(defn, parse(defn, stored))
        except (InvalidInputError, ValueError, AttributeError):
            log.warning("ignoring the stored override of %s: %r", key, stored)
    return settings.model_copy(update=update) if update else settings


class OverrideStore:
    """The stored overrides for the running app, re-read at most every `ttl` seconds (another
    process may have saved) and at once after `invalidate`. `apply` lays them over a Settings."""

    def __init__(self, sessions: sessionmaker[Session], base: Settings, ttl: float = 5.0) -> None:
        self._sessions = sessions
        self.base = base
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._read_at = 0.0

    def overrides(self) -> dict[str, Any]:
        with self._lock:
            now = clock.monotonic()
            if self._cached is None or now - self._read_at >= self._ttl:
                self._cached = self._load()
                self._read_at = now
            return self._cached

    def _load(self) -> dict[str, Any]:
        try:
            with self._sessions() as session:
                return stored_overrides(session)
        except SQLAlchemyError:  # no table yet (before the migration), or the database is away
            log.warning("could not read the schedule overrides; using the env values")
            return {}

    def apply(self, settings: Settings) -> Settings:
        return apply_overrides(settings, self.overrides())

    def effective(self) -> Settings:
        return self.apply(self.base)

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None


# --- The page and the API ---


@dataclass(frozen=True)
class SettingRow:
    definition: SettingDef
    default: Any  # canonical, from env / code default
    value: Any  # canonical, the effective value
    overridden: bool

    @property
    def default_text(self) -> str:
        return display(self.definition, self.default)

    @property
    def value_text(self) -> str:
        return display(self.definition, self.value)


def rows(session: Session, base: Settings) -> list[SettingRow]:
    overrides = stored_overrides(session)
    result = []
    for defn in DEFINITIONS:
        default = canonical(defn, getattr(base, defn.key))
        overridden = defn.key in overrides
        value = parse(defn, overrides[defn.key]) if overridden else default
        result.append(SettingRow(defn, default, value, overridden))
    return result


def save(
    session: Session,
    base: Settings,
    values: Mapping[str, object],
    now: datetime | None = None,
) -> list[str]:
    """Validate all values, then store them (all or nothing). A value equal to the env value
    removes the override. Returns the keys that changed."""
    parsed: dict[str, Any] = {}
    for key, raw in values.items():
        defn = definition(key)
        parsed[key] = parse(defn, raw)
    stored = stored_overrides(session)
    moment = now or utcnow()
    changed = []
    for key, value in parsed.items():
        default = canonical(definition(key), getattr(base, key))
        current = parse(definition(key), stored[key]) if key in stored else default
        if value == current:
            continue
        _write(session, key, stored.get(key), None if value == default else value, moment)
        changed.append(key)
    session.flush()
    return changed


def reset(session: Session, key: str, now: datetime | None = None) -> bool:
    """Remove the override of one setting; True when there was one."""
    definition(key)
    old = session.get(AppSetting, key)
    if old is None:
        return False
    _write(session, key, old.value_json, None, now or utcnow())
    session.flush()
    return True


def _write(session: Session, key: str, old: Any, new: Any | None, now: datetime) -> None:
    row = session.get(AppSetting, key)
    if new is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(AppSetting(key=key, value_json=new, updated_at=now))
    else:
        row.value_json = new
        row.updated_at = now
    session.add(AppSettingLog(key=key, old_json=old, new_json=new, created_at=now))


def recent_changes(session: Session, limit: int = 20) -> list[AppSettingLog]:
    return list(
        session.scalars(select(AppSettingLog).order_by(AppSettingLog.id.desc()).limit(limit))
    )
