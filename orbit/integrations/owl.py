"""The Postal Owl (owl.example.edu), read with the `owl_session` cookie the owner copies from their
browser (SPEC §2.20, `DECISIONS.md` #13). GET only, through the same polite client as SIS.

Only three kinds of path are ever fetched (`allowed_path`): `/`, `/c/all` and a course page
`/c/<sem>/<course>/`. Never a topic page (it would mark threads read), never `/acct/*`,
`/join/*`, `/settings/*`, never the "Leave the course" link, never a POST. A redirect to
`/acct/login/` (or HTTP 401/403) means the cookie died.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlsplit

from orbit.integrations.sis import PRAGUE, PoliteClient, SisError

COOKIE_NAME = "owl_session"
_COURSE_PATH_RE = re.compile(r"^/c/(?P<sem>[a-z]{2}\d{4})/(?P<ident>[A-Za-z0-9][A-Za-z0-9_.-]*)/$")
_DEADLINE_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2})")
_TOTAL_RE = re.compile(r"([\d.]+)\s+out of\s+([\d.]+)")
_THRESH_RE = re.compile(r"get\s+([\d.]+)\s+points")
_FORBIDDEN = ("/acct", "/join", "/settings", "/admin", "/api")


class OwlDenied(SisError):
    """The session cookie is missing, expired or rejected."""


def allowed_path(path: str) -> bool:
    """The only Owl pages Orbit may GET: the home page, the course list and a course page."""
    if path in ("/", "/c/all"):
        return True
    if any(path.startswith(prefix) for prefix in _FORBIDDEN):
        return False
    return _COURSE_PATH_RE.match(path) is not None and not path.rstrip("/").endswith("/leave")


def course_path(path: str) -> tuple[str, str] | None:
    """(semester, course ident) of "/c/zs2627/26aNMAT202x06/"."""
    match = _COURSE_PATH_RE.match(path)
    return (match.group("sem"), match.group("ident")) if match else None


# --- Parsers ---


@dataclass(frozen=True)
class OwlCourseRef:
    semester: str  # "zs2627"
    ident: str  # "26aNMAT202x06" or "la1-svobodova"
    name: str  # "Diskrétní matematika (Ján Plachý, Čt 15:40)"
    semester_label: str | None  # "Winter semester 2026/2027"

    @property
    def path(self) -> str:
        return f"{self.semester}/{self.ident}"


@dataclass(frozen=True)
class OwlTopic:
    ident: str  # "ukol-1"
    href: str  # "/c/zs2627/26aNMAT202x06/ukol-1/" (for a link; Orbit never fetches it)
    title: str
    group: str | None  # the h3 heading above its table ("Domácí úkoly")
    deadline: datetime | None  # UTC
    points: Decimal | None
    max_points: Decimal | None


@dataclass(frozen=True)
class OwlCoursePage:
    title: str
    description: str | None
    topics: list[OwlTopic] = field(default_factory=list)
    total_points: Decimal | None = None
    total_max: Decimal | None = None
    threshold: Decimal | None = None


class _OwlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.h2: list[str] = []
        self.links: list[tuple[str, str, str | None]] = []  # (href, text, last h2)
        self.rows: list[tuple[str | None, list[tuple[str, list[str]]]]] = []  # (h3, cells)
        self.desc_parts: list[str] = []
        self.by_id: dict[str, str] = {}
        self._h3: str | None = None
        self._capture: str | None = None  # "h2" | "h3" | "a" | "desc" | "id:<x>"
        self._buf: list[str] = []
        self._href: str | None = None
        self._in_topics = False
        self._row: list[tuple[str, list[str]]] | None = None
        self._cell: list[str] | None = None
        self._cell_hrefs: list[str] = []
        self._desc_depth = 0

    def _flush_cell(self) -> None:
        if self._row is not None and self._cell is not None:
            self._row.append((" ".join("".join(self._cell).split()), self._cell_hrefs))
        self._cell = None
        self._cell_hrefs = []

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._row:
            self.rows.append((self._h3, self._row))
        self._row = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if self._desc_depth:
            if tag == "div":
                self._desc_depth += 1
            if tag == "p":
                self.desc_parts.append("\n")
        elif tag == "div" and ("course-desc" in classes or "course-note" in classes):
            self._desc_depth = 1
        if tag in ("h2", "h3"):
            self._capture, self._buf = tag, []
        elif tag == "p" and values.get("id") in ("totalpoints", "totalthresh"):
            self._capture, self._buf = f"id:{values['id']}", []
        elif tag == "table" and "topics" in classes:
            self._in_topics = True
        elif self._in_topics and tag == "tr":
            self._flush_row()
            self._row = []
        elif self._in_topics and tag in ("td", "th") and self._row is not None:
            self._flush_cell()
            self._cell = [] if tag == "td" else None
        elif tag == "a" and values.get("href"):
            if self._cell is not None:
                self._cell_hrefs.append(values["href"] or "")
            elif not self._in_topics and self._capture is None:
                self._capture, self._buf, self._href = "a", [], values["href"]

    def handle_endtag(self, tag: str) -> None:
        if self._desc_depth and tag == "div":
            self._desc_depth -= 1
        text = " ".join("".join(self._buf).split())
        if tag == "h2" and self._capture == "h2":
            self.h2.append(text)
            self._capture = None
        elif tag == "h3" and self._capture == "h3":
            self._h3 = text or None
            self._capture = None
        elif tag == "a" and self._capture == "a":
            self.links.append((self._href or "", text, self.h2[-1] if self.h2 else None))
            self._capture = None
        elif tag == "table" and self._in_topics:
            self._flush_row()
            self._in_topics = False

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)
            if self._capture.startswith("id:"):
                key = self._capture[3:]
                self.by_id[key] = self.by_id.get(key, "") + data
        if self._cell is not None:
            self._cell.append(data)
        if self._desc_depth:
            self.desc_parts.append(data)


def parse_course_list(html: str) -> list[OwlCourseRef]:
    """The courses of `/c/all` (or `/`), with the semester heading each one is under."""
    parser = _OwlParser()
    parser.feed(html)
    parser.close()
    found: dict[str, OwlCourseRef] = {}
    for href, text, heading in parser.links:
        parts = course_path(urlsplit(href).path)
        if parts is None:
            continue
        ref = OwlCourseRef(parts[0], parts[1], text or parts[1], heading)
        found.setdefault(ref.path, ref)
    return list(found.values())


def _points(text: str) -> Decimal | None:
    try:
        return Decimal(text.strip()) if text.strip() else None
    except InvalidOperation:
        return None


def _deadline(text: str) -> datetime | None:
    """ "2026-10-15 15:40 (in 8 days)" (Prague time) -> UTC; the relative part is dropped."""
    match = _DEADLINE_RE.search(text)
    if not match:
        return None
    local = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=PRAGUE)
    return local.astimezone(UTC)


def parse_course_page(html: str) -> OwlCoursePage:
    """Topics (deadline, own points, max), the course total, the pass threshold and the course
    description of an Owl course page. Raises SisError when it isn't a course page."""
    parser = _OwlParser()
    parser.feed(html)
    parser.close()
    if not parser.h2:
        raise SisError("not an Owl course page")
    topics = []
    for group, cells in parser.rows:
        if len(cells) < 4 or not cells[0][1]:
            continue
        href = urlsplit(cells[0][1][0]).path
        ident = href.strip("/").split("/")[-1]
        if not ident:
            continue
        topics.append(
            OwlTopic(
                ident=ident,
                href=href,
                title=cells[0][0],
                group=group,
                deadline=_deadline(cells[1][0]),
                points=_points(cells[2][0]),
                max_points=_points(cells[3][0]),
            )
        )
    description = "\n".join(
        " ".join(line.split()) for line in "".join(parser.desc_parts).split("\n")
    ).strip()
    total = _TOTAL_RE.search(parser.by_id.get("totalpoints", ""))
    thresh = _THRESH_RE.search(parser.by_id.get("totalthresh", ""))
    return OwlCoursePage(
        title=parser.h2[0],
        description="\n".join(line for line in description.splitlines() if line) or None,
        topics=topics,
        total_points=_points(total.group(1).rstrip(".")) if total else None,
        total_max=_points(total.group(2).rstrip(".")) if total else None,
        threshold=_points(thresh.group(1).rstrip(".")) if thresh else None,
    )


# --- The source ---


class OwlSource(Protocol):
    def fetch_courses(self) -> list[OwlCourseRef]:
        """Raises OwlDenied or SisError."""
        ...

    def fetch_course(self, ref: OwlCourseRef) -> OwlCoursePage:
        """Raises OwlDenied or SisError."""
        ...


class OwlHttpSource:
    def __init__(self, client: PoliteClient, base_url: str, *, cookie: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._cookie = cookie

    def _get(self, path: str) -> str:
        if not allowed_path(path):
            raise SisError(f"refusing to fetch Owl path {path!r}")
        result = self._client.get(
            f"{self._base}{path}", headers={"Cookie": f"{COOKIE_NAME}={self._cookie}"}
        )
        location = result.headers.get("location", "")
        if result.status_code in (401, 403) or "/acct/login" in location:
            raise OwlDenied("the Owl login expired")
        if result.status_code != 200:
            raise SisError(f"HTTP {result.status_code}")
        return result.body.decode("utf-8", errors="replace")

    def fetch_courses(self) -> list[OwlCourseRef]:
        return parse_course_list(self._get("/c/all"))

    def fetch_course(self, ref: OwlCourseRef) -> OwlCoursePage:
        return parse_course_page(self._get(f"/c/{ref.semester}/{ref.ident}/"))
