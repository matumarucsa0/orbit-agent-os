"""SK/CZ name-day calendars from data/namedays_{sk,cz}.json (see data/README.md)."""

from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from orbit.domain.normalize import normalize_text

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
COUNTRIES = ("SK", "CZ")


class NamedayCalendar:
    def __init__(self, days: dict[str, list[str]]) -> None:
        self.days = days
        # Keyed by normalized name. A set of dates: the SK calendar lists accent variants on the
        # same day (Šimon, Simon), which normalize to the same key.
        self._dates: dict[str, set[tuple[int, int]]] = defaultdict(set)
        # The calendar's own spelling; the first one seen wins ("Šimon" before "Simon").
        self._spelling: dict[str, str] = {}
        for key, names in days.items():
            month, day = int(key[:2]), int(key[3:])
            for name in names:
                norm = normalize_text(name)
                self._dates[norm].add((month, day))
                self._spelling.setdefault(norm, name)

    def dates_for(self, name: str) -> list[tuple[int, int]]:
        """All (month, day) on which `name` has its name day; diacritics-insensitive."""
        return sorted(self._dates.get(normalize_text(name), set()))

    def names_on(self, month: int, day: int) -> list[str]:
        return list(self.days.get(f"{month:02d}-{day:02d}", []))

    def canonical(self, name: str) -> str | None:
        """The calendar's own spelling of `name` ("marian" -> "Marián"), or None."""
        return self._spelling.get(normalize_text(name))

    def search(self, query: str, limit: int = 20) -> list[str]:
        """Calendar names matching `query`: prefix matches first, then substring, alphabetical."""
        needle = normalize_text(query)
        if not needle:
            return []
        prefix = sorted(k for k in self._spelling if k.startswith(needle))
        inner = sorted(k for k in self._spelling if needle in k and not k.startswith(needle))
        return [self._spelling[k] for k in prefix + inner][:limit]


@lru_cache
def get_calendar(country: str) -> NamedayCalendar:
    if country not in COUNTRIES:
        raise ValueError(f"No name-day calendar for {country!r}")
    path = DATA_DIR / f"namedays_{country.lower()}.json"
    return NamedayCalendar(json.loads(path.read_text(encoding="utf-8")))


def dates_for(name: str, country: str | None) -> list[tuple[int, int]]:
    if not country or country not in COUNTRIES or not name.strip():
        return []
    return get_calendar(country).dates_for(name)


def names_on(month: int, day: int, country: str) -> list[str]:
    return get_calendar(country).names_on(month, day)


def canonical(name: str, country: str | None) -> str | None:
    if not country or country not in COUNTRIES:
        return None
    return get_calendar(country).canonical(name)
