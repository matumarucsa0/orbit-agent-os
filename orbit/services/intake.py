"""One entry point for what the owner shares: decide where it goes, without asking Claude
(SPEC §2.29).

Routing is deterministic: the shape of the thing says what it is. Claude only reads the text of a
plain note afterwards (capture), as it always did.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from orbit.domain.normalize import normalize_text
from orbit.domain.school import Course
from orbit.integrations.bank_parsers import BankFormatError, decode, detect_bank
from orbit.services import school
from orbit.services.errors import InvalidInputError

_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
MAX_SNIFF = 4096  # bytes of a file looked at to tell what it is


class Route(StrEnum):
    CAPTURE = "capture"  # text: dictated or typed
    BANK = "bank_import"  # a CSV whose header a bank parser knows
    CONTACTS = "contacts"  # a vCard file
    PDF = "school_resource"  # a PDF
    URL = "school_resource_url"  # a link


@dataclass(frozen=True)
class Decision:
    route: Route
    url: str | None = None  # for Route.URL: the link
    course_hint: str = ""  # text to find a course in (the filename, or the words around a link)


def _text_hint(text: str | None, url: str | None) -> str:
    rest = (text or "").replace(url or "", " ") if url else (text or "")
    return rest.strip()


def decide(
    *, text: str | None, url: str | None, filename: str | None, data: bytes | None
) -> Decision:
    """Where a share goes. A file wins over a link, a link over plain text. Raises
    InvalidInputError for a file that is none of the known kinds (nothing is saved)."""
    text = (text or "").strip() or None
    url = (url or "").strip() or None
    if data is not None:
        head = data[:MAX_SNIFF]
        name = filename or ""
        hint = f"{name} {text or ''}".strip()
        if head.startswith(b"%PDF"):
            return Decision(Route.PDF, course_hint=hint)
        if head.lstrip().upper().startswith(b"BEGIN:VCARD"):
            return Decision(Route.CONTACTS)
        try:
            first = decode(head).splitlines()[0] if head.strip() else ""
            dialect = ";" if first.count(";") > first.count(",") else ","
            header = next(csv.reader(io.StringIO(first), delimiter=dialect), [])
        except (BankFormatError, UnicodeDecodeError, csv.Error):
            header = []
        if detect_bank(header) is not None:
            return Decision(Route.BANK)
        raise InvalidInputError(
            "Orbit does not know this file: it is not a PDF, a vCard or a bank statement it reads."
        )
    if url is None and text is not None and _URL.match(text):
        url, text = text, None
    if url is not None:
        if not _URL.match(url):
            raise InvalidInputError("That does not look like a web link.")
        return Decision(Route.URL, url=url, course_hint=_text_hint(text, url))
    if text is None:
        raise InvalidInputError("Nothing was shared: send text, a link or a file.")
    return Decision(Route.CAPTURE)


def match_course(session: Session, hint: str) -> Course | None:
    """The one current course whose code or name appears in `hint`; None for none or several."""
    if not hint.strip():
        return None
    haystack = normalize_text(hint)
    found = [
        c
        for c in school.current_courses(session)
        if normalize_text(c.code) in haystack
        or (len(normalize_text(c.name)) >= 6 and normalize_text(c.name) in haystack)
    ]
    return found[0] if len(found) == 1 else None
