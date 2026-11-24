"""Fakes and fixtures shared by the school tests. No test touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from orbit.domain.enums import FetchStatus
from orbit.integrations import owl as owl_mod
from orbit.integrations import recodex as recodex_mod
from orbit.integrations.sis import CoursePage, FeedFetch, HarmonogramRow, parse_course_page

FIXTURES = Path(__file__).parent / "fixtures"
SIS = FIXTURES / "sis"
RECODEX = FIXTURES / "recodex"
OWL = FIXTURES / "owl"

ROZVRHNG = (SIS / "ical_rozvrhng.ics").read_bytes()
ROZ_EXTERNI = (SIS / "roz_externi_ical.ics").read_bytes()
EMPTY = (SIS / "ical_empty.ics").read_bytes()

# Hand-built: no real term_st2 sample exists yet (the feed was empty during research; SPEC §2.20).
EXAM_FEED = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Erudio//SIS//EN\r
BEGIN:VEVENT\r
UID:TST2_12345@10000@sis.example.edu\r
DTSTAMP:20261006T183045Z\r
DTSTART:20270112T070000Z\r
DTEND:20270112T090000Z\r
SUMMARY:NMAT202 Diskr\xc3\xa9tn\xc3\xad matematika - zkou\xc5\xa1ka\r
LOCATION:S5\r
STATUS:CONFIRMED\r
END:VEVENT\r
END:VCALENDAR\r
"""


def ok(body: bytes) -> FeedFetch:
    return FeedFetch(FetchStatus.OK, body=body)


def replace_in(body: bytes, old: str, new: str) -> bytes:
    assert old.encode() in body, old
    return body.replace(old.encode(), new.encode())


def drop_event(body: bytes, uid: str) -> bytes:
    """The feed without the VEVENT whose UID contains `uid`."""
    text = body.decode()
    parts = text.split("BEGIN:VEVENT")
    kept = [parts[0]] + [p for p in parts[1:] if uid not in p.split("END:VEVENT")[0]]
    assert len(kept) == len(parts) - 1, uid
    tail = "END:VCALENDAR" if not kept[-1].rstrip().endswith("END:VCALENDAR") else ""
    return ("BEGIN:VEVENT".join(kept) + tail).encode()


@dataclass
class FakeSis:
    """`feeds` maps a feed name to what fetching it returns (a FeedFetch or a callable)."""

    answers: dict[str, FeedFetch | Callable[[], FeedFetch]] = field(default_factory=dict)
    pages: dict[str, CoursePage | None] = field(default_factory=dict)
    harmonogram: list[HarmonogramRow] = field(default_factory=list)
    course_calls: list[tuple[str, int]] = field(default_factory=list)
    harmonogram_calls: list[tuple[str, date, date]] = field(default_factory=list)
    course_error: Exception | None = None

    @property
    def feeds(self) -> list[str]:
        return list(self.answers)

    def fetch_feed(self, feed: str) -> FeedFetch:
        answer = self.answers[feed]
        return answer() if callable(answer) else answer

    def fetch_course(self, code: str, year: int) -> CoursePage | None:
        self.course_calls.append((code, year))
        if self.course_error is not None:
            raise self.course_error
        return self.pages.get(code)

    def fetch_harmonogram(self, faculty: str, start: date, end: date) -> list[HarmonogramRow]:
        self.harmonogram_calls.append((faculty, start, end))
        return self.harmonogram


def ninf030_page() -> CoursePage:
    page = parse_course_page((SIS / "course_NINF030.html").read_text())
    assert page is not None
    return page


@dataclass
class FakeRecodex:
    feed: FeedFetch | None = None
    groups: Any = None  # JSON, or an exception to raise
    assignments: dict[str, Any] = field(default_factory=dict)
    expires: datetime | None = None
    user: str = "00000000-0000-4000-8000-000000000001"
    api: bool = True

    @property
    def has_feed(self) -> bool:
        return self.feed is not None

    @property
    def claims(self) -> recodex_mod.TokenClaims | None:
        return recodex_mod.TokenClaims(self.user, self.expires) if self.api else None

    def fetch_feed(self) -> FeedFetch:
        assert self.feed is not None
        return self.feed

    def assignment_url(self, assignment: str) -> str:
        return f"https://recodex.example.edu/app/assignment/{assignment}"

    def fetch_groups(self) -> list[recodex_mod.RecodexGroup]:
        if isinstance(self.groups, Exception):
            raise self.groups
        return recodex_mod.parse_groups(self.groups)

    def fetch_assignments(self, group_id: str) -> list[recodex_mod.RecodexAssignment]:
        data = self.assignments.get(group_id, {"success": True, "payload": []})
        return recodex_mod.parse_assignments(data)


def recodex_groups() -> Any:
    return json.loads((RECODEX / "user_groups.json").read_text())


def recodex_assignments() -> dict[str, Any]:
    data = json.loads((RECODEX / "group_assignments.json").read_text())
    return {"4f881122-1667-5cff-abd4-8fe7c0ed4086": data}


@dataclass
class FakeOwl:
    courses_html: str = ""
    pages: dict[str, str] = field(default_factory=dict)  # "<sem>/<ident>" -> html
    denied: bool = False
    fetched: list[str] = field(default_factory=list)

    def fetch_courses(self) -> list[owl_mod.OwlCourseRef]:
        if self.denied:
            raise owl_mod.OwlDenied("the Owl login expired")
        return owl_mod.parse_course_list(self.courses_html)

    def fetch_course(self, ref: owl_mod.OwlCourseRef) -> owl_mod.OwlCoursePage:
        self.fetched.append(ref.path)
        return owl_mod.parse_course_page(self.pages[ref.path])


def owl_fixture() -> FakeOwl:
    return FakeOwl(
        courses_html=(OWL / "all_courses.html").read_text(),
        pages={
            "zs2627/26aNMAT202x06": (OWL / "course_NMAT202_handbuilt.html").read_text(),
            "ls2526/la1-svobodova": (OWL / "course_la1_svobodova.html").read_text(),
        },
    )
