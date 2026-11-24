"""Fetching and reading school resources (Phase 18c): pages, PDFs and GitHub repositories.

Orbit fetches and extracts text; Claude only ever reads the text Orbit hands over. GET only, one
host at a time and 5 s apart, a fixed user agent, `robots.txt` respected, conditional requests,
a body cap, and charset taken from the header or the page (some teacher pages are windows-1250).
"""

from __future__ import annotations

import io
import re
import threading
import time
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from ipaddress import ip_address
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

USER_AGENT = "Orbit/1.0 (personal school reader; GET only)"
MAX_BYTES = 20 * 1024 * 1024
HOST_GAP_SECONDS = 5.0
TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 5
SKIP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "noscript", "svg", "form"})
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "section",
        "ul",
        "ol",
    }
)
LOGIN_HOST_HINTS = ("moodle", "teams.microsoft.com", "login.", "sso.", "cas.")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I)
_GITHUB = re.compile(r"^/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


class ResourceError(Exception):
    """A resource could not be fetched or read; the message is safe to show."""


# --- URLs ---


def check_url(url: str) -> str:
    """The URL cleaned up, or ResourceError: http(s) only, no credentials, no local addresses."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ResourceError("Use a web address that starts with http:// or https://.")
    if parts.username or parts.password:
        raise ResourceError("A web address with a password in it is not accepted.")
    if len(url) > 1000:
        raise ResourceError("The web address is longer than 1000 characters.")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ResourceError("Local addresses are not fetched.")
    try:
        address = ip_address(host)
    except ValueError:
        return url
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        raise ResourceError("Local addresses are not fetched.")
    return url


def detect_kind(
    url: str, content_type: str | None = None
) -> Literal["page", "pdf", "github", "login"]:
    """The kind of a resource from its URL (and the response type when known)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in ("github.com", "www.github.com") and _GITHUB.match(parts.path):
        return "github"
    if any(hint in host for hint in LOGIN_HOST_HINTS):
        return "login"
    if parts.path.lower().endswith(".pdf") or "pdf" in (content_type or "").lower():
        return "pdf"
    return "page"


def github_repo(url: str) -> tuple[str, str]:
    match = _GITHUB.match(urlsplit(url).path)
    if match is None:
        raise ResourceError("Not a GitHub repository address.")
    return match.group(1), match.group(2)


def links_in(text: str) -> list[str]:
    """The web addresses in a text, in order, without duplicates or trailing punctuation."""
    found = [u.rstrip(".,;:!?") for u in _URL.findall(text)]
    return list(dict.fromkeys(found))


# --- Text extraction ---


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def decode(body: bytes, content_type: str | None) -> str:
    """Body text: the charset from the HTTP header, else from a <meta> tag, else UTF-8, else
    windows-1250 (Czech and Slovak pages that never said)."""
    match = re.search(r"charset\s*=\s*([\w-]+)", content_type or "", re.I)
    meta = _META_CHARSET.search(body[:4096])
    for name in (match.group(1) if match else None, meta.group(1).decode() if meta else None):
        if name:
            try:
                return body.decode(name, errors="replace")
            except LookupError:
                continue
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1250", errors="replace")


def normalize_text(text: str, *, squeeze: bool = False) -> str:
    """One blank line at most (none with `squeeze`: pages and PDFs, so a diff shows only real
    changes), no trailing spaces, no zero-width noise."""
    lines = [" ".join(line.split()) for line in text.replace("​", "").splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (not squeeze and out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def html_to_text(body: bytes, content_type: str | None = None) -> str:
    parser = _TextParser()
    parser.feed(decode(body, content_type))
    parser.close()
    return normalize_text("".join(parser.parts), squeeze=True)


def pdf_to_text(body: bytes) -> str:
    """The text layer of a PDF ("" when it has none: no OCR). ResourceError for a broken file."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(body))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError, OSError, KeyError) as exc:
        raise ResourceError(f"The PDF could not be read ({exc.__class__.__name__}).") from None
    return normalize_text("\n".join(pages), squeeze=True)


def atom_to_text(body: bytes) -> str:
    """A GitHub commits.atom feed as lines of "date title" (newest first)."""
    text = body.decode("utf-8", errors="replace")
    lines = []
    for entry in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        updated = re.search(r"<updated>(.*?)</updated>", entry)
        if title:
            when = updated.group(1)[:10] if updated else ""
            lines.append(f"{when} {' '.join(title.group(1).split())}".strip())
    return "\n".join(lines[:30])


# --- Fetching ---


@dataclass(frozen=True)
class FetchResult:
    status: Literal["ok", "not_modified", "error"]
    body: bytes = b""
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


class ResourceFetcher(Protocol):
    def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult: ...


class HttpFetcher:
    """GET only, sequential, polite. Safe to share between threads (requests are serialised)."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        gap: float = HOST_GAP_SECONDS,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        self._gap = gap
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = self._gap - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()

    def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        self._wait(urlsplit(url).netloc)
        return self._client.get(url, headers=headers)

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = self._get(f"{origin}/robots.txt")
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                    self._robots[origin] = parser
                else:
                    self._robots[origin] = None  # no robots.txt: everything is allowed
            except httpx.HTTPError:
                self._robots[origin] = None
        parser_or_none = self._robots[origin]
        return parser_or_none is None or parser_or_none.can_fetch(USER_AGENT, url)

    def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        with self._lock:
            try:
                url = check_url(url)
                if not self._allowed(url):
                    return FetchResult(
                        "error", error="The site's robots.txt asks not to fetch this."
                    )
                headers = {}
                if etag:
                    headers["If-None-Match"] = etag
                if last_modified:
                    headers["If-Modified-Since"] = last_modified
                response = self._get(url, headers)
                hops = 0
                while (
                    response.has_redirect_location
                    and response.next_request is not None
                    and hops < MAX_REDIRECTS
                ):
                    target = (
                        check_url(str(response.next_request.url)) if response.next_request else ""
                    )
                    response = self._get(target, headers)
                    hops += 1
            except ResourceError as exc:
                return FetchResult("error", error=str(exc))
            except httpx.TimeoutException:
                return FetchResult("error", error="Timed out.")
            except httpx.HTTPError as exc:
                return FetchResult("error", error=f"{exc.__class__.__name__}.")
        if response.status_code == 304:
            return FetchResult("not_modified")
        if response.status_code in (401, 403):
            return FetchResult(
                "error", error=f"The page needs a login (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            return FetchResult("error", error=f"HTTP {response.status_code}.")
        if len(response.content) > MAX_BYTES:
            return FetchResult("error", error="The page is larger than 20 MB.")
        return FetchResult(
            "ok",
            body=response.content,
            content_type=response.headers.get("content-type"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    def close(self) -> None:
        self._client.close()
