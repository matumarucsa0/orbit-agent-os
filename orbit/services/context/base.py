"""The context provider protocol (`VISION.md §4`): what a module tells agents about itself.

A provider returns a typed, bounded, masked Pydantic summary of one module for a time window and
an optional focus. Agents read module data only through providers. Providers never write.

- Typed: a `ModuleContext` subclass per module, with ids that link to other modules.
- Bounded: a hard cap on every list; `truncated` says when something was left out. The caps are
  generous on purpose (DECISIONS #7): they stop a runaway query, they do not ration information.
- Masked: every free-text value goes through `text()`, which hides IBANs and phone numbers and
  clips the length. Account and contact identifiers (IBANs, phone numbers, emails, handle values)
  are not in the models: they carry no signal for a decision and are the only real fraud risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import ClassVar, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from orbit.integrations.claude import mask_sensitive

TEXT_CHARS = 2000  # the longest free-text value a provider hands out
NAME_CHARS = 200


@dataclass(frozen=True)
class ContextWindow:
    """Whole local days from `start` to `end`, both included. `start` is "today" for the
    provider: open items due before it are overdue."""

    start: date
    end: date
    tz: ZoneInfo

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("The window ends before it starts.")

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def start_utc(self) -> datetime:
        return datetime.combine(self.start, time.min, self.tz).astimezone(ZoneInfo("UTC"))

    @property
    def end_utc(self) -> datetime:
        """The first moment after the window (exclusive bound)."""
        after = datetime.combine(self.end + timedelta(days=1), time.min, self.tz)
        return after.astimezone(ZoneInfo("UTC"))

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class ContextFocus:
    """Narrow a provider to what touches one entity. Today only a person; goals come later."""

    person_id: int | None = None
    # Only people with activity in the window (an interaction, a meeting): "who did he meet",
    # without everyone who merely has something open. For the reviewed week (SPEC §2.30).
    activity_only: bool = False


class ModuleContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    module: str
    # True when a cap cut the list short; the summary is then a selection, not everything.
    truncated: bool = False
    # The list fields the context layer may cut from the end to fit its size budget. Each is
    # ordered most important first. Totals and other summary fields are never listed here.
    trimmable: ClassVar[tuple[str, ...]] = ()
    # True: trim the first non-empty list of `trimmable` first (school cuts lessons before it
    # cuts exams); False: trim the biggest list first.
    trim_in_order: ClassVar[bool] = False


class ContextProvider(Protocol):
    name: str

    def build(
        self, session: Session, window: ContextWindow, focus: ContextFocus | None = None
    ) -> ModuleContext: ...


def text(value: str, limit: int = TEXT_CHARS) -> str:
    """One line, masked, at most `limit` characters. Masks before clipping so an identifier is
    never cut in half and left partly readable."""
    one_line = " ".join(mask_sensitive(value).split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"
