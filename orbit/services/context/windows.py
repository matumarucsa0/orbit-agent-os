"""Context windows: the presets (and custom dates) the debug view and agents ask for."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, get_args
from zoneinfo import ZoneInfo

from orbit.services.context.base import ContextWindow
from orbit.services.errors import InvalidInputError

Preset = Literal["this_week", "next_7_days"]
PRESETS: tuple[Preset, ...] = get_args(Preset)
DEFAULT_PRESET: Preset = "this_week"
MAX_WINDOW_DAYS = 92  # about a quarter; a longer window would only fill the size budget


def preset_window(preset: Preset, today: date, tz: ZoneInfo) -> ContextWindow:
    """`this_week`: Monday to Sunday of the week `today` is in; `next_7_days`: today + 6 days."""
    if preset == "this_week":
        monday = today - timedelta(days=today.weekday())
        return ContextWindow(monday, monday + timedelta(days=6), tz)
    return ContextWindow(today, today + timedelta(days=6), tz)


def resolve_window(
    today: date,
    tz: ZoneInfo,
    preset: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> ContextWindow:
    """Two dates make a custom window; otherwise a preset (default `this_week`)."""
    if start is not None or end is not None:
        if start is None or end is None:
            raise InvalidInputError("Give both a start and an end date.")
        if end < start:
            raise InvalidInputError("The window ends before it starts.")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise InvalidInputError(f"A window can be at most {MAX_WINDOW_DAYS} days.")
        return ContextWindow(start, end, tz)
    name = preset or DEFAULT_PRESET
    if name not in PRESETS:
        raise InvalidInputError(f"Unknown window: {name}. Use one of {', '.join(PRESETS)}.")
    return preset_window(name, today, tz)
