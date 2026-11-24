"""School resources: fetching, text, checking and the suggestions (Phase 18c, SPEC §2.20)."""

# ruff: noqa: E501  (long one-line asserts and fixtures read better unwrapped)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.orm import Session

from orbit.domain.enums import ResourceKind, ResourceOrigin, ResourceStatus
from orbit.domain.school import Course, OwlCourse, SchoolResource
from orbit.integrations import resources as web
from orbit.integrations.resources import FetchResult, HttpFetcher, ResourceError
from orbit.services import school_resources as svc
from orbit.services.errors import InvalidInputError, NotFoundError
from tests.school_resource_helpers import FakeFetcher, blank_pdf, page, pdf_with_text

TZ = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)  # 11:00 in Prague
URL = "https://example.cz/nmat101"


@pytest.fixture
def course(db: Session) -> Course:
    row = Course(code="NMAT101", name="Lineární algebra 1", credits=6)
    db.add(row)
    db.flush()
    return row


# --- Addresses and kinds ---


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.cz/x",
        "javascript:alert(1)",
        "https://user:pw@example.cz/",
        "http://localhost:8000/",
        "http://127.0.0.1/",
        "http://192.168.1.5/",
        "http://printer.local/",
        "https://example.cz/" + "x" * 1000,
        "not a url",
    ],
)
def test_addresses_that_are_not_fetched(url: str) -> None:
    with pytest.raises(ResourceError):
        web.check_url(url)


def test_kinds_come_from_the_address() -> None:
    assert web.detect_kind("https://github.com/owner/repo") == "github"
    assert web.detect_kind("https://github.com/owner") == "page"
    assert web.detect_kind("https://moodle.example.edu/course/1") == "login"
    assert web.detect_kind("https://example.cz/notes.pdf") == "pdf"
    assert web.detect_kind("https://example.cz/download", "application/pdf") == "pdf"
    assert web.detect_kind("https://example.cz/") == "page"
    assert web.github_repo("https://github.com/owner/repo.git") == ("owner", "repo")


def test_links_in_a_text() -> None:
    text = "See https://a.cz/x, and (https://b.cz/y). Again https://a.cz/x!"
    assert web.links_in(text) == ["https://a.cz/x", "https://b.cz/y"]


# --- Text ---


def test_html_text_drops_scripts_and_navigation() -> None:
    html = (
        "<html><head><style>p{}</style></head><body><nav>Menu</nav><script>x=1</script>"
        "<h1>Linear algebra</h1><p>Test 1: 15 October</p><ul><li>A</li><li>B</li></ul>"
        "<footer>Imprint</footer></body></html>"
    )
    text = web.html_to_text(html.encode())
    assert text == "Linear algebra\nTest 1: 15 October\nA\nB"


def test_charsets_header_meta_and_the_czech_fallback() -> None:
    word = "Zkouška v září"
    assert word in web.html_to_text(
        f"<p>{word}</p>".encode("cp1250"), "text/html; charset=windows-1250"
    )
    meta = f'<meta http-equiv="x" content="text/html; charset=windows-1250"><p>{word}</p>'
    assert word in web.html_to_text(meta.encode("cp1250"))
    assert word in web.html_to_text(f"<p>{word}</p>".encode("cp1250"))  # not UTF-8: cp1250
    assert word in web.html_to_text(f"<p>{word}</p>".encode())


def test_pdf_text_and_a_pdf_without_text() -> None:
    assert web.pdf_to_text(pdf_with_text("Test 1 on 15 October")) == "Test 1 on 15 October"
    assert web.pdf_to_text(blank_pdf()) == ""
    with pytest.raises(ResourceError):
        web.pdf_to_text(b"%PDF-1.4 not really")


def test_atom_commits_as_lines() -> None:
    atom = (
        b"<feed><entry><title>Add task 2</title><updated>2026-10-05T10:00:00Z</updated></entry>"
        b"<entry><title>Fix typo</title><updated>2026-10-01T10:00:00Z</updated></entry></feed>"
    )
    assert web.atom_to_text(atom) == "2026-10-05 Add task 2\n2026-10-01 Fix typo"


# --- The polite fetcher ---


def fetcher_for(handler, **kw):  # type: ignore[no-untyped-def]
    sleeps: list[float] = []
    clock = {"t": 100.0}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    fetcher = HttpFetcher(
        transport=httpx.MockTransport(handler), sleep=sleep, clock=lambda: clock["t"], **kw
    )
    return fetcher, sleeps


def test_fetcher_waits_between_requests_to_one_host_and_reads_robots_once() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<p>hi</p>", headers={"content-type": "text/html"})

    fetcher, sleeps = fetcher_for(handler)
    assert fetcher.fetch("https://example.cz/a").status == "ok"
    assert fetcher.fetch("https://example.cz/b").status == "ok"
    assert seen == ["/robots.txt", "/a", "/b"]
    assert len(sleeps) == 2 and all(0 < s <= 5 for s in sleeps)  # 5 s apart, one host at a time


def test_fetcher_respects_robots_txt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")

    fetcher, _ = fetcher_for(handler)
    refused = fetcher.fetch("https://example.cz/private/x")
    assert refused.status == "error" and "robots.txt" in (refused.error or "")
    assert fetcher.fetch("https://example.cz/public").status == "ok"


def test_conditional_get_and_errors() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/same":
            return httpx.Response(304)
        if request.url.path == "/login":
            return httpx.Response(403)
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * (web.MAX_BYTES + 1))
        return httpx.Response(500)

    fetcher, _ = fetcher_for(handler)
    assert (
        fetcher.fetch("https://example.cz/same", etag='"v1"', last_modified="Mon").status
        == "not_modified"
    )
    conditional = next(r for r in sent if r.url.path == "/same")
    assert (
        conditional.headers["if-none-match"] == '"v1"'
        and conditional.headers["if-modified-since"] == "Mon"
    )
    assert "login" in (fetcher.fetch("https://example.cz/login").error or "")
    assert "20 MB" in (fetcher.fetch("https://example.cz/big").error or "")
    assert fetcher.fetch("https://example.cz/boom").error == "HTTP 500."
    assert fetcher.fetch("http://localhost/").status == "error"


def test_redirects_are_followed_but_not_into_local_addresses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://example.cz/new"})
        if request.url.path == "/evil":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(200, text="<p>new</p>", headers={"content-type": "text/html"})

    fetcher, _ = fetcher_for(handler)
    assert fetcher.fetch("https://example.cz/old").status == "ok"
    assert fetcher.fetch("https://example.cz/evil").status == "error"


# --- Adding, keeping, removing ---


def test_a_url_by_hand_is_kept_and_a_login_page_is_never_fetched(
    db: Session, course: Course
) -> None:
    page_row = svc.add_url(db, URL, course.id)
    assert (page_row.kind, page_row.kept, page_row.origin) == (
        ResourceKind.PAGE,
        True,
        ResourceOrigin.MANUAL,
    )
    assert page_row.label == "example.cz/nmat101"
    login = svc.add_url(db, "https://moodle.example.edu/course/1", course.id)
    assert (login.kind, login.status) == (ResourceKind.LOGIN, ResourceStatus.LOGIN)
    fetcher = FakeFetcher()
    assert svc.check(db, login, fetcher, NOW) is ResourceStatus.LOGIN and fetcher.calls == []
    with pytest.raises(InvalidInputError, match="already"):
        svc.add_url(db, URL, course.id)
    with pytest.raises(InvalidInputError, match="http"):
        svc.add_url(db, "ftp://x/", None)
    with pytest.raises(InvalidInputError, match="course"):
        svc.add_url(db, "https://example.cz/other", 999)


def test_a_suggestion_waits_for_keep_and_dismiss_is_for_good(db: Session, course: Course) -> None:
    row = svc.add_url(db, URL, course.id, origin=ResourceOrigin.OWL, reason="Link in the Owl text")
    assert not row.kept and row.suggested_reason == "Link in the Owl text"
    assert svc.due_resources(db, NOW, TZ, 7, force=True) == []  # not fetched until kept
    svc.dismiss(db, row.id)
    assert svc.list_resources(db) == []
    with pytest.raises(InvalidInputError, match="already"):
        svc.add_url(db, URL, course.id, origin=ResourceOrigin.OWL)  # never suggested again
    svc.keep(db, row.id)
    assert svc.list_resources(db)[0].kept
    with pytest.raises(InvalidInputError, match="suggestion"):
        svc.dismiss(db, row.id)


def test_notes(db: Session, course: Course) -> None:
    note = svc.add_note(db, "  Credit needs 120 of 240 points  \n\n\n second line", course.id)
    assert note.kind is ResourceKind.NOTE and note.kept and note.changed_at is not None
    assert note.text == "Credit needs 120 of 240 points\n\nsecond line"
    assert note.label == "Credit needs 120 of 240 points"
    with pytest.raises(InvalidInputError, match="empty"):
        svc.add_note(db, "  \n ")
    with pytest.raises(InvalidInputError, match="longer"):
        svc.add_note(db, "x" * (svc.NOTE_CHARS + 1))


def test_files_are_pdfs_stored_by_hash(db: Session, course: Course, tmp_path: Path) -> None:
    data = pdf_with_text("Test 1 on 15 October")
    row = svc.add_file(db, data, "Rules 2026.pdf", course.id, tmp_path)
    assert (row.kind, row.label, row.status) == (ResourceKind.FILE, "Rules 2026", ResourceStatus.OK)
    assert row.text == "Test 1 on 15 October" and row.changed_at is not None
    assert (tmp_path / f"{row.file_sha256}.pdf").read_bytes() == data
    with pytest.raises(InvalidInputError, match="already"):
        svc.add_file(db, data, "again.pdf", None, tmp_path)
    with pytest.raises(InvalidInputError, match="PDF"):
        svc.add_file(db, b"hello", "x.pdf", None, tmp_path)
    with pytest.raises(InvalidInputError, match="20 MB"):
        svc.add_file(db, b"%PDF" + b"x" * svc.MAX_FILE_BYTES, "x.pdf", None, tmp_path)
    blank = svc.add_file(db, blank_pdf(), "scan.pdf", None, tmp_path)
    assert (
        blank.status is ResourceStatus.NO_TEXT and blank.text is None and blank.changed_at is None
    )
    svc.remove(db, row.id, tmp_path)
    assert not (tmp_path / f"{row.file_sha256}.pdf").exists()
    with pytest.raises(NotFoundError):
        svc.get_resource(db, row.id)


def test_linking_to_a_course(db: Session, course: Course) -> None:
    row = svc.add_url(db, URL)
    assert row.course_id is None
    assert svc.link_course(db, row.id, course.id).course_id == course.id
    assert svc.link_course(db, row.id, None).course_id is None
    with pytest.raises(InvalidInputError):
        svc.link_course(db, row.id, 999)


def test_suggestions_from_the_course_page_and_owl_text(db: Session, course: Course) -> None:
    course.web_url = "https://math.example.edu/~svobodova/la1/"
    db.add(
        OwlCourse(
            path="ls2526/la1-svobodova",
            name="LA1",
            course_id=course.id,
            description="Notes: https://example.cz/la-notes, tasks https://github.com/svobodova/la1.",
        )
    )
    db.flush()
    assert svc.suggest_links(db) == 3
    assert svc.suggest_links(db) == 0  # idempotent
    rows = svc.list_resources(db, course.id)
    assert {r.origin for r in rows} == {ResourceOrigin.SIS, ResourceOrigin.OWL}
    assert not any(r.kept for r in rows)
    assert {r.kind for r in rows} == {ResourceKind.PAGE, ResourceKind.GITHUB}


# --- Checking ---


def kept(db: Session, url: str = URL, **kw: object) -> SchoolResource:
    row = svc.add_url(db, url)
    for key, value in kw.items():
        setattr(row, key, value)
    return row


def test_first_fetch_then_change_with_a_diff_then_no_change(db: Session) -> None:
    row = kept(db)
    fetcher = FakeFetcher({URL: page("<p>Test 1: 15 October</p><p>Rules: A</p>")})
    assert svc.check(db, row, fetcher, NOW) is ResourceStatus.OK
    assert (
        row.text == "Test 1: 15 October\nRules: A"
        and row.prev_text is None
        and row.last_diff is None
    )
    assert row.etag == '"v1"' and row.changed_at == NOW and row.fail_count == 0

    fetcher.pages[URL] = page("<p>Test 1: 22 October</p><p>Rules: A</p>")
    later = NOW + timedelta(days=1)
    assert svc.check(db, row, fetcher, later) is ResourceStatus.CHANGED
    assert row.prev_text == "Test 1: 15 October\nRules: A" and row.changed_at == later
    assert row.last_diff == "-Test 1: 15 October\n+Test 1: 22 October"

    assert svc.check(db, row, fetcher, later + timedelta(days=1)) is ResourceStatus.UNCHANGED
    assert row.changed_at == later  # a check without a change does not move it
    fetcher.pages[URL] = FetchResult("not_modified")
    assert svc.check(db, row, fetcher, later + timedelta(days=2)) is ResourceStatus.UNCHANGED
    assert fetcher.calls[-1] == (URL, '"v1"')  # asked conditionally


def test_a_pdf_by_url_and_a_page_that_is_a_pdf(db: Session) -> None:
    row = kept(db, "https://example.cz/rules.pdf")
    fetcher = FakeFetcher(
        {
            "https://example.cz/rules.pdf": FetchResult(
                "ok", body=pdf_with_text("Credit: 60 points"), content_type="application/pdf"
            )
        }
    )
    assert svc.check(db, row, fetcher, NOW) is ResourceStatus.OK and row.text == "Credit: 60 points"
    blank = kept(db, "https://example.cz/scan.pdf")
    fetcher.pages["https://example.cz/scan.pdf"] = FetchResult(
        "ok", body=blank_pdf(), content_type="application/pdf"
    )
    assert svc.check(db, blank, fetcher, NOW) is ResourceStatus.NO_TEXT and blank.text is None


def test_github_is_read_through_the_readme_and_commits(db: Session) -> None:
    row = kept(db, "https://github.com/svobodova/la1")
    fetcher = FakeFetcher(
        {
            "https://raw.githubusercontent.com/svobodova/la1/HEAD/README.md": FetchResult(
                "ok", body=b"# LA1\nHomework 1 due 20 Oct", content_type="text/plain"
            ),
            "https://github.com/svobodova/la1/commits/HEAD.atom": FetchResult(
                "ok",
                body=b"<feed><entry><title>Add hw 2</title><updated>2026-10-05T10:00:00Z</updated></entry></feed>",
            ),
        }
    )
    assert row.kind is ResourceKind.GITHUB
    svc.check(db, row, fetcher, NOW)
    assert (
        row.text is not None
        and "Homework 1 due 20 Oct" in row.text
        and "2026-10-05 Add hw 2" in row.text
    )
    only_commits = FakeFetcher(
        {
            "https://github.com/svobodova/la1/commits/HEAD.atom": FetchResult(
                "ok", body=b"<feed></feed>"
            )
        }
    )
    other = kept(db, "https://github.com/svobodova/other")
    assert (
        svc.check(db, other, only_commits, NOW) is ResourceStatus.ERROR
    )  # the README was missing, no commits


def test_failures_count_once_a_day_and_seven_days_break_it(db: Session) -> None:
    row = kept(db)
    fetcher = FakeFetcher()  # every URL answers 404
    for day in range(6):
        status = svc.check(db, row, fetcher, NOW + timedelta(days=day))
        assert status is ResourceStatus.ERROR
        svc.check(db, row, fetcher, NOW + timedelta(days=day, hours=1))  # a second try the same day
    assert row.fail_count == 6 and row.error == "HTTP 404."
    assert svc.check(db, row, fetcher, NOW + timedelta(days=6)) is ResourceStatus.BROKEN
    fetcher.pages[URL] = page("<p>back</p>")
    assert svc.check(db, row, fetcher, NOW + timedelta(days=7)) is ResourceStatus.OK
    assert row.fail_count == 0 and row.error is None


def test_notes_and_files_are_not_fetched(db: Session, tmp_path: Path) -> None:
    note = svc.add_note(db, "rules")
    fetcher = FakeFetcher()
    assert svc.check(db, note, fetcher, NOW) is ResourceStatus.OK and fetcher.calls == []


# --- Which resources are due ---


def test_due_means_kept_fetched_not_yet_checked_today_and_from_the_hour(db: Session) -> None:
    fresh = kept(db, "https://example.cz/a")
    today = kept(db, "https://example.cz/b", checked_at=NOW - timedelta(hours=1))
    yesterday = kept(db, "https://example.cz/c", checked_at=NOW - timedelta(days=1))
    svc.add_note(db, "a note")
    svc.add_url(db, "https://example.cz/d", origin=ResourceOrigin.SIS)  # a suggestion
    due = svc.due_resources(db, NOW, TZ, 7)
    assert due == [fresh, yesterday]
    assert svc.due_resources(db, NOW, TZ, 12) == []  # 11:00 in Prague, before the 12 o'clock
    assert today in svc.due_resources(db, NOW, TZ, 12, force=True)


def test_a_run_checks_what_is_due_and_caps_itself(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "MAX_PER_RUN", 2)
    pages = {}
    for n in range(3):
        kept(db, f"https://example.cz/{n}")
        pages[f"https://example.cz/{n}"] = page(f"<p>{n}</p>")
    report = svc.run_checks(db, FakeFetcher(pages), NOW, TZ, 7)
    assert (report.checked, report.waiting, report.failed) == (2, 1, 0)
    again = svc.run_checks(db, FakeFetcher(pages), NOW, TZ, 7)
    assert again.checked == 1 and again.waiting == 0
    nothing = svc.run_checks(db, FakeFetcher(pages), NOW, TZ, 7)
    assert nothing.checked == 0


def test_preview_is_clipped() -> None:
    row = SchoolResource(
        kind=ResourceKind.NOTE, origin=ResourceOrigin.MANUAL, label="x", text="a" * 700
    )
    assert len(svc.preview(row)) == 600 and svc.preview(row).endswith("…")
