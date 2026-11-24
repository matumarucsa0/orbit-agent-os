"""Fuzzy name matching shared by capture and meeting linking: diacritics-insensitive, tolerant of
Slovak/Czech case endings ("Paťovi", "s Paťom", "Terke", "Petrovi", "Markom") and close spellings.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from orbit.domain.normalize import normalize_text
from orbit.domain.people import Person

MIN_STEM = 3
MAX_SUFFIX = 4  # Slovak/Czech case endings: -ovi, -om, -ou, -e, -u, -y, -ovej…
FUZZY_RATIO = 0.85
FUZZY_MIN_LENGTH = 5
_WORD_RE = re.compile(r"@?\w+")
_VOWELS = "aeiouy"


def words(text: str) -> list[str]:
    """Normalized words of `text`, keeping a leading "@" (handles)."""
    return _WORD_RE.findall(normalize_text(text))


def _stems(name: str) -> set[str]:
    """Forms an inflected mention starts with: "terka" -> terk; "peter" -> petr (Petra, Petrovi)."""
    stems = {name}
    if len(name) > MIN_STEM and name[-1] in _VOWELS:
        stems.add(name[:-1])
    if len(name) > MIN_STEM + 1 and name[-2] in "eo" and name[-1] not in _VOWELS:
        stems.add(name[:-2] + name[-1])  # the fleeting e/o: Marek -> Marka, Karol -> Karla
    return {s for s in stems if len(s) >= MIN_STEM}


def word_score(word: str, name: str) -> int:
    """3 exact, 2 inflected form, 1 close spelling, 0 no match (both normalized)."""
    if word == name:
        return 3
    if len(name) < MIN_STEM:
        return 0
    for stem in _stems(name):
        if word.startswith(stem) and len(word) - len(stem) <= MAX_SUFFIX:
            return 2
    if (
        min(len(word), len(name)) >= FUZZY_MIN_LENGTH
        and SequenceMatcher(None, word, name).ratio() >= FUZZY_RATIO
    ):
        return 1
    return 0


def name_tokens(person: Person) -> set[str]:
    """The normalized words of a person's names and aliases (aliases must be loaded)."""
    tokens = set(person.search_name_norm.split())
    for alias in person.aliases:
        tokens.update(alias.value_norm.split())
    return tokens
