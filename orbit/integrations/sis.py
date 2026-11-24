"""Charles University SIS: the token calendar feeds and two public pages (SPEC §2.20).

Orbit never logs in to SIS and never holds a session. It reads:
- `ical.php?typ=<type>&token=<SIS_ICAL_TOKEN>` (lessons, registered exam terms, SIS events),
- `rozvrhng/roz_externi.php?format=ical&auth_key=<SIS_TIMETABLE_KEY>` (lessons),
- the public course page (`predmety/index.php?do=predmet`) and harmonogram (`harmonogram/`).

Every request goes through `PoliteClient`: a fixed user agent, at most one request per
`THROTTLE_SECONDS`, one at a time, and every secret redacted from anything it raises. The same
client serves ReCodEx and Owl (`integrations/recodex.py`, `integrations/owl.py`).

Parsers are pure functions over saved fixtures (`tests/fixtures/sis/`). A UID or a description
that doesn't have the expected shape is kept as text, never dropped.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as dtime
from html.parser import HTMLParser
from importlib import metadata
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
import icalendar

from orbit.domain.enums import FetchStatus, LessonKind, Semester

log = logging.getLogger(__name__)


def _version() -> str:
    try:
        return metadata.version("orbit")
    except metadata.PackageNotFoundError:
        return "dev"


USER_AGENT = f"Orbit/{_version()} personal-assistant"
THROTTLE_SECONDS = 2.0
TIMEOUT_SECONDS = 30.0
MAX_BODY_BYTES = 5_000_000
PRAGUE = ZoneInfo("Europe/Prague")
REDACTED = "[redacted]"

TICKET_RE = re.compile(
    r"^(?P<src>[A-Z]+?)?(?P<ticket>(?P<yy>\d{2})(?P<term>[ab])(?P<code>[A-Z]{3,5}\d{3}[A-Z]?)"
    r"(?P<kind>[px])(?P<n>\d+))_(?P<jdn>\d+)@(?P<fak>\d+)"
)
_ROOM_LINE_RE = re.compile(r"^(?P<room>.+?)\s+\d{1,2}:\d{2}\b")
_DTSTAMP_RE = re.compile(rb"^DTSTAMP:[^\r\n]*\r?\n", re.MULTILINE)


class SisError(Exception):
    """A fetch that failed; the message never holds a token."""


# --- Secrets and the polite HTTP client ---


def redact(text: str, secrets: Iterable[str]) -> str:
    """`text` with every secret (and its URL-encoded form) replaced."""
    for secret in secrets:
        if not secret:
            continue
        for form in {secret, urlencode({"x": secret})[2:]}:
            text = text.replace(form, REDACTED)
    return text


class Throttle:
    """At most one call per `interval` seconds (process-wide for one client)."""

    def __init__(
        self,
        interval: float = THROTTLE_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < self.interval:
                self._sleep(self.interval - (now - self._last))
                now = self._clock()
            self._last = now


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    body: bytes
    headers: dict[str, str]
    url: str  # redacted


class PoliteClient:
    """GET only, sequential, throttled, with a fixed user agent; errors are redacted."""

    def __init__(
        self,
        secrets: Sequence[str] = (),
        *,
        throttle: Throttle | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._secrets = [s for s in secrets if s]
        self._throttle = throttle or Throttle()
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )
        self._lock = threading.Lock()

    def redact(self, text: str) -> str:
        return redact(text, self._secrets)

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> HttpResult:
        with self._lock:  # never two requests at once
            self._throttle.wait()
            try:
                response = self._client.get(url, headers=headers)
                body = response.content[:MAX_BODY_BYTES]
            except httpx.TimeoutException as exc:
                raise SisError("timed out") from exc
            except httpx.HTTPError as exc:
                raise SisError(self.redact(f"{exc.__class__.__name__}: {exc}")) from None
        return HttpResult(
            status_code=response.status_code,
            body=body,
            headers={k.lower(): v for k, v in response.headers.items()},
            url=self.redact(str(response.url)),
        )

    def close(self) -> None:
        self._client.close()


# --- Feeds ---


@dataclass(frozen=True)
class FeedFetch:
    """One feed download: `status` is OK when `body` is a calendar, else DENIED or ERROR with a
    redacted `error`. EMPTY and UNCHANGED are decided by the sync, not here."""

    status: FetchStatus
    body: bytes = b""
    error: str | None = None


def calendar_fetch(client: PoliteClient, url: str) -> FeedFetch:
    """GET an iCalendar URL; 401/403/404 = DENIED (token rotated or export disabled)."""
    try:
        result = client.get(url)
    except SisError as exc:
        return FeedFetch(FetchStatus.ERROR, error=str(exc))
    return calendar_result(result)


def calendar_result(result: HttpResult) -> FeedFetch:
    if result.status_code in (401, 403, 404):
        return FeedFetch(FetchStatus.DENIED, error=f"HTTP {result.status_code}")
    if result.status_code != 200:
        return FeedFetch(FetchStatus.ERROR, error=f"HTTP {result.status_code}")
    if not result.body.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"BEGIN:VCALENDAR"):
        return FeedFetch(FetchStatus.ERROR, error="not a calendar")
    return FeedFetch(FetchStatus.OK, body=result.body)


def body_hash(body: bytes) -> str:
    """Hash of a feed body without its DTSTAMP lines (they change on every download)."""
    return hashlib.sha256(_DTSTAMP_RE.sub(b"", body)).hexdigest()


@dataclass(frozen=True)
class FeedEvent:
    """One VEVENT, generically: times in UTC, text unfolded and unescaped."""

    uid: str
    start: datetime
    end: datetime | None
    all_day: bool
    summary: str | None
    description: str | None
    location: str | None
    url: str | None
    status: str | None
    raw: dict[str, str] = field(default_factory=dict)


def _plain(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _moment(value: Any, floating_tz: tzinfo) -> tuple[datetime, bool]:
    """(UTC datetime, all-day) for a DTSTART/DTEND value. A date is Prague midnight."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=floating_tz)
        return value.astimezone(UTC), False
    if isinstance(value, date):
        return datetime.combine(value, dtime.min, PRAGUE).astimezone(UTC), True
    raise ValueError("not a date or time")


def parse_feed(body: bytes | str, *, floating_tz: tzinfo = PRAGUE) -> list[FeedEvent]:
    """Every VEVENT of a feed. A floating time is read in `floating_tz` (ReCodEx: UTC).
    Raises SisError when the body isn't iCalendar. The non-standard `ALL_DAY` is ignored."""
    try:
        calendar = icalendar.Calendar.from_ical(body)
    except ValueError as exc:
        raise SisError(f"not a calendar: {exc}") from exc
    events = []
    for component in calendar.walk("VEVENT"):
        uid = _plain(component.get("UID"))
        dtstart = component.get("DTSTART")
        if not uid or dtstart is None:
            log.warning("skipped a VEVENT without UID or DTSTART")
            continue
        try:
            start, all_day = _moment(dtstart.dt, floating_tz)
            dtend = component.get("DTEND")
            end = _moment(dtend.dt, floating_tz)[0] if dtend is not None else None
        except ValueError:
            log.warning("skipped a VEVENT with a broken time")
            continue
        if all_day and end is not None:
            end -= timedelta(minutes=1)  # DTEND of a date is exclusive: end the day before
        raw = {
            str(name): str(value.to_ical(), "utf-8") if hasattr(value, "to_ical") else str(value)
            for name, value in component.property_items(recursive=False)
            if name not in ("BEGIN", "END")
        }
        events.append(
            FeedEvent(
                uid=uid,
                start=start,
                end=end if end is None or end >= start else start,
                all_day=all_day,
                summary=_plain(component.get("SUMMARY")),
                description=_plain(component.get("DESCRIPTION")),
                location=_plain(component.get("LOCATION")),
                url=_plain(component.get("URL")),
                status=(_plain(component.get("STATUS")) or "").upper() or None,
                raw=raw,
            )
        )
    return events


# --- Lessons ---


@dataclass(frozen=True)
class LessonData:
    uid: str  # normalized: "<ticket>_<julianDay>", or the raw UID when it doesn't parse
    ticket: str | None
    course_code: str | None
    year: int | None  # the school year the ticket belongs to (2026 for "26a…")
    kind: LessonKind
    title: str
    teachers: str | None
    room: str | None
    location: str | None
    start: datetime
    end: datetime
    raw_uid: str


def parse_uid(uid: str) -> re.Match[str] | None:
    return TICKET_RE.match(uid)


_BARE_TICKET_RE = re.compile(r"^(\d{2})([ab])([A-Z]{3,5}\d{3}[A-Z]?)([px])(\d+)$")


def ticket_course_code(ticket: str | None) -> str | None:
    """ "26aNINF030x03" -> "NINF030"; None for anything that isn't a ticket id."""
    match = _BARE_TICKET_RE.match(ticket or "")
    return match.group(3) if match else None


def ticket_year(ticket: str | None) -> int | None:
    """ "26aNINF030x03" -> 2026, the school year the ticket belongs to."""
    match = _BARE_TICKET_RE.match(ticket or "")
    return 2000 + int(match.group(1)) if match else None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _room(line: str) -> str | None:
    match = _ROOM_LINE_RE.match(_one_line(line))
    return match.group("room") if match else None


def _split_description(description: str) -> tuple[str | None, str | None, str | None]:
    """(subject, teachers, room) from either feed's DESCRIPTION; (None, None, None) when the
    shape is unknown.

    ical.php: "Rozvrhový lístek: <subject>; <teachers>; <room> <HH:MM> <weeks> týden <code>".
    roz_externi: "<subject>\\n<teachers>\\n<room> <HH:MM> <code>"."""
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    if len(lines) >= 3:
        return _one_line(lines[0]), _one_line(", ".join(lines[1:-1])), _room(lines[-1])
    text = _one_line(description)
    pieces = [p.strip() for p in text.split("; ")]
    if len(pieces) >= 3 and pieces[0].startswith("Rozvrhový lístek:"):
        subject = pieces[0].removeprefix("Rozvrhový lístek:").strip() or None
        return subject, "; ".join(pieces[1:-1]) or None, _room(pieces[-1])
    return None, None, None


def lesson_from_event(event: FeedEvent) -> LessonData:
    match = parse_uid(event.uid)
    ticket = code = None
    year = None
    kind = LessonKind.OTHER
    uid = event.uid
    if match:
        ticket = match.group("ticket")
        code = match.group("code")
        year = 2000 + int(match.group("yy"))
        kind = {"p": LessonKind.LECTURE, "x": LessonKind.SEMINAR}[match.group("kind")]
        uid = f"{ticket}_{match.group('jdn')}"
    subject, teachers, room = _split_description(event.description or "")
    title = subject or event.summary or ticket or "(lesson)"
    if room is None and event.location:
        room = re.split(r" - | \(", event.location, maxsplit=1)[0].strip() or None
    return LessonData(
        uid=uid,
        ticket=ticket,
        course_code=code,
        year=year,
        kind=kind,
        title=title,
        teachers=teachers,
        room=room,
        location=_one_line(event.location) if event.location else None,
        start=event.start,
        end=event.end or event.start,
        raw_uid=event.uid,
    )


# --- Public pages: HTML helpers ---


class _TableParser(HTMLParser):
    """Collects `<tr><th>Label</th><td>value</td></tr>` pairs, `<tr class=...>` rows of `<td>`s,
    elements by class, and links, from a legacy HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: dict[str, tuple[str, list[str], list[str]]] = {}  # label -> text, parts, hrefs
        self.rows: list[tuple[str, list[str]]] = []  # (tr class, cell texts)
        self.by_class: dict[str, list[str]] = {}
        self._cell: str | None = None  # "th" | "td"
        self._buf: list[str] = []
        self._parts: list[list[str]] = []
        self._hrefs: list[str] = []
        self._label: str | None = None
        self._row_class: str | None = None
        self._row_cells: list[str] = []
        self._class_stack: list[tuple[str, str, list[str]]] = []  # (tag, class, buffer)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        cls = values.get("class") or ""
        if tag == "tr":
            self._row_class = cls
            self._row_cells = []
        elif tag in ("th", "td"):
            self._cell = tag
            self._buf = []
            self._parts = [[]]
            self._hrefs = []
        elif tag == "br" and self._cell:
            self._parts.append([])
        elif tag == "a" and self._cell and values.get("href"):
            self._hrefs.append(values["href"] or "")
        if tag == "div" and cls:
            self._class_stack.append((tag, cls, []))
        elif tag == "div":
            self._class_stack.append((tag, "", []))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._cell == tag:
            text = _one_line("".join(self._buf))
            parts = [_one_line("".join(p)) for p in self._parts if _one_line("".join(p))]
            if tag == "th":
                self._label = text.rstrip(":").strip()
            else:
                if self._label is not None and self._label not in self.pairs:
                    self.pairs[self._label] = (text, parts, list(self._hrefs))
                self._label = None
                self._row_cells.append(text)
            self._cell = None
        elif tag == "tr":
            if self._row_class:
                self.rows.append((self._row_class, self._row_cells))
            self._row_class = None
            self._label = None
        elif tag == "div" and self._class_stack:
            _, cls, buf = self._class_stack.pop()
            if cls:
                self.by_class.setdefault(cls, []).append(_one_line("".join(buf)))

    def handle_data(self, data: str) -> None:
        if self._cell:
            self._buf.append(data)
            self._parts[-1].append(data)
        for _, _, buf in self._class_stack:
            buf.append(data)


# --- Course page ---


@dataclass(frozen=True)
class CoursePage:
    code: str
    name: str
    name_en: str | None = None
    credits: int | None = None
    hours_lecture: int | None = None
    hours_seminar: int | None = None
    completion: str | None = None
    semester: Semester | None = None
    faculty_code: str | None = None
    department: str | None = None
    guarantor: str | None = None
    teachers: tuple[str, ...] = ()
    language: str | None = None
    web_url: str | None = None


_SCOPE_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*,\s*(Z\+Zk|KZ|Zk|Z|Kv)\b")
_SEMESTERS = {"zimní": Semester.WINTER, "letní": Semester.SUMMER, "oba": Semester.BOTH}


def _query_value(href: str, name: str) -> str | None:
    values = parse_qs(urlsplit(href.replace("&amp;", "&")).query).get(name)
    return values[0] if values else None


def parse_course_page(html: str) -> CoursePage | None:
    """The details of a public SIS course page, by Czech label; None when it isn't a course
    page (an error page also comes with HTTP 200). Missing labels are left empty."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    header = next(iter(parser.by_class.get("form_div_title", [])), "")
    match = re.fullmatch(r"(?P<name>.+) - (?P<code>[A-Z]{3,5}\d{3}[A-Z]?)", header)
    if not match:
        return None
    pairs = parser.pairs

    def value(label: str) -> str | None:
        found = pairs.get(label)
        return found[0] or None if found else None

    credits = value("E-Kredity")
    scope = _SCOPE_RE.search(value("Rozsah, examinace") or "")
    semester = (value("Semestr") or "").casefold()
    faculty = pairs.get("Fakulta")
    department = value("Zajišťuje")
    web = pairs.get("Další informace")
    links = [h for h in web[2] if h.startswith(("http://", "https://"))] if web else []
    web_url = links[0] if links else None
    if web_url is None and web and web[0].startswith(("http://", "https://")):
        web_url = web[0].split()[0]
    guarantors = pairs.get("Garant")
    teachers = pairs.get("Vyučující")
    return CoursePage(
        code=match.group("code"),
        name=match.group("name").strip(),
        name_en=value("Anglický název"),
        credits=int(credits) if credits and credits.isdigit() else None,
        hours_lecture=int(scope.group(1)) if scope else None,
        hours_seminar=int(scope.group(2)) if scope else None,
        completion=scope.group(3) if scope else None,
        semester=next((s for k, s in _SEMESTERS.items() if semester.startswith(k)), None),
        faculty_code=_query_value(faculty[2][0], "kod") if faculty and faculty[2] else None,
        department=department,
        guarantor="; ".join(guarantors[1]) or None if guarantors else None,
        teachers=tuple(teachers[1]) if teachers else (),
        language=value("Jazyk výuky"),
        web_url=web_url,
    )


# --- Public harmonogram ---


@dataclass(frozen=True)
class HarmonogramRow:
    start: datetime  # UTC
    end: datetime
    faculty: str
    title: str
    note: str | None


def _prague(text: str) -> datetime:
    local = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=PRAGUE)
    return local.astimezone(UTC)


def parse_harmonogram(html: str) -> list[HarmonogramRow]:
    """The rows (`<tr class="row1|row2">`: Od | Do | Fakulta | Název | Poznámka) of a public
    harmonogram page. Rows that don't have that shape are skipped."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    found = []
    for cls, cells in parser.rows:
        if cls not in ("row1", "row2") or len(cells) < 4:
            continue
        try:
            start, end = _prague(cells[0]), _prague(cells[1])
        except ValueError:
            continue
        found.append(
            HarmonogramRow(
                start=start,
                end=max(end, start),
                faculty=cells[2],
                title=cells[3],
                note=(cells[4] if len(cells) > 4 else "") or None,
            )
        )
    return found


# --- The source ---


class SisSource(Protocol):
    def fetch_feed(self, feed: str) -> FeedFetch:
        """`feed` is "roz_externi" or an ical.php type ("rozvrhng", "term_st2", "stev")."""
        ...

    def fetch_course(self, code: str, year: int) -> CoursePage | None:
        """None when the page is missing or has an unknown layout. Raises SisError."""
        ...

    def fetch_harmonogram(self, faculty: str, start: date, end: date) -> list[HarmonogramRow]:
        """Raises SisError."""
        ...

    @property
    def feeds(self) -> list[str]:
        """The feeds that are set up: "roz_externi" or "rozvrhng" for lessons, then the others."""
        ...


class SisHttpSource:
    def __init__(
        self,
        client: PoliteClient,
        base_url: str,
        *,
        ical_token: str | None,
        timetable_key: str | None,
        feed_types: Sequence[str],
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._ical_token = ical_token
        self._timetable_key = timetable_key
        self._types = list(feed_types)

    @property
    def feeds(self) -> list[str]:
        found = []
        if self._timetable_key:
            found.append("roz_externi")
        if self._ical_token:
            found += [t for t in self._types if not (t == "rozvrhng" and self._timetable_key)]
        return found

    def fetch_feed(self, feed: str) -> FeedFetch:
        if feed == "roz_externi":
            if not self._timetable_key:
                return FeedFetch(FetchStatus.ERROR, error="SIS_TIMETABLE_KEY isn't set")
            query = urlencode({"format": "ical", "auth_key": self._timetable_key})
            return calendar_fetch(self._client, f"{self._base}/rozvrhng/roz_externi.php?{query}")
        if not self._ical_token:
            return FeedFetch(FetchStatus.ERROR, error="SIS_ICAL_TOKEN isn't set")
        query = urlencode({"typ": feed, "token": self._ical_token})
        return calendar_fetch(self._client, f"{self._base}/ical.php?{query}")

    def _page(self, path: str, params: dict[str, str]) -> str | None:
        result = self._client.get(f"{self._base}/{path}?{urlencode(params)}")
        if result.status_code == 404:
            return None
        if result.status_code != 200:
            raise SisError(f"HTTP {result.status_code}")
        return result.body.decode("utf-8", errors="replace")

    def fetch_course(self, code: str, year: int) -> CoursePage | None:
        html = self._page("predmety/index.php", {"do": "predmet", "kod": code, "skr": str(year)})
        return parse_course_page(html) if html is not None else None

    def fetch_harmonogram(self, faculty: str, start: date, end: date) -> list[HarmonogramRow]:
        params = {
            "do": "filtr",
            "fak": faculty,
            "skr": str(start.year if start.month >= 9 else start.year - 1),
            "platnaod": f"{start:%d.%m.%Y}",
            "platnado": f"{end:%d.%m.%Y}",
        }
        html = self._page("harmonogram/index.php", params)
        return parse_harmonogram(html) if html is not None else []


def course_page_url(base_url: str, code: str, year: int) -> str:
    """The public SIS page of a course (no token, safe to show)."""
    query = urlencode({"do": "predmet", "kod": code, "skr": str(year)})
    return f"{base_url.rstrip('/')}/predmety/index.php?{query}"
