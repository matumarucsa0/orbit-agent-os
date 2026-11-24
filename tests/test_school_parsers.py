"""SIS, ReCodEx and Owl parsers and HTTP readers, against the saved fixtures (no network)."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from orbit.domain.enums import FetchStatus, LessonKind, Semester
from orbit.integrations import owl, recodex, sis
from tests.school_helpers import EMPTY, OWL, RECODEX, ROZ_EXTERNI, ROZVRHNG, SIS

SECRET = "S3cr3tT0ken" + "q" * 21


# --- Feeds ---


def test_rozvrhng_feed_lessons() -> None:
    events = sis.parse_feed(ROZVRHNG)
    assert len(events) == 11
    lessons = [sis.lesson_from_event(e) for e in events]
    first = lessons[0]
    assert first.uid == "26aNMAT202p1_2461315"  # RNG prefix and @… suffix dropped
    assert first.raw_uid == "RNG26aNMAT202p1_2461315@10000@sis.example.edu"
    assert (first.ticket, first.course_code, first.year) == ("26aNMAT202p1", "NMAT202", 2026)
    assert first.kind is LessonKind.LECTURE
    assert first.title == "Diskrétní matematika"
    # Teachers carry commas (titles): kept whole, as one string.
    assert first.teachers == "Teacher01 Name, Mgr., Ph.D., Teacher02 Name, RNDr., Ph.D."
    assert (first.room, first.location) == ("N1", "N1 - Building A")
    assert first.start == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert first.end == datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    seminar = lessons[1]
    assert seminar.kind is LessonKind.SEMINAR and seminar.teachers == "Teacher03 Name, Bc."
    sport = lessons[-1]
    assert (sport.course_code, sport.room) == ("NSPO014", "SPORT")
    assert "Teacher23 Name, Mgr." in (sport.teachers or "")
    assert {e.raw.get("ALL_DAY") for e in events} == {"0"}  # kept in raw, not interpreted
    assert not any(e.all_day for e in events)


def test_both_feeds_give_the_same_lesson_rows() -> None:
    a = {sis.lesson_from_event(e).uid for e in sis.parse_feed(ROZVRHNG)}
    b = [sis.lesson_from_event(e) for e in sis.parse_feed(ROZ_EXTERNI)]
    assert a == {lesson.uid for lesson in b}
    first = b[0]
    assert first.title == "Diskrétní matematika"  # not the SUMMARY with teachers
    assert first.teachers == "Teacher01 Name, Mgr., Ph.D., Teacher02 Name, RNDr., Ph.D."
    assert first.room == "N1"
    assert first.location == "N1 (Vzorová 1, 10000 Praha 1)"


def _calendar(*events: str) -> bytes:
    body = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    for event in events:
        body += "BEGIN:VEVENT\r\n" + event.strip().replace("\n", "\r\n") + "\r\nEND:VEVENT\r\n"
    return (body + "END:VCALENDAR\r\n").encode()


def test_a_uid_that_does_not_parse_is_kept() -> None:
    body = _calendar(
        """UID:odd-thing@example
DTSTART:20261007T080000Z
DTEND:20261007T093000Z
SUMMARY:Something
DESCRIPTION:no known shape
LOCATION:S3 - Karlín"""
    )
    (event,) = sis.parse_feed(body)
    lesson = sis.lesson_from_event(event)
    assert lesson.uid == "odd-thing@example"
    assert (lesson.ticket, lesson.course_code, lesson.kind) == (None, None, LessonKind.OTHER)
    assert (lesson.title, lesson.teachers, lesson.room) == ("Something", None, "S3")


def test_all_day_and_floating_times() -> None:
    body = _calendar(
        """UID:a
DTSTART;VALUE=DATE:20261020
DTEND;VALUE=DATE:20261021
SUMMARY:Rector's day""",
        """UID:b
DTSTART:20261025T225959
SUMMARY:floating""",
    )
    day, floating = sis.parse_feed(body)
    assert day.all_day and day.start == datetime(2026, 10, 19, 22, 0, tzinfo=UTC)  # Prague midnight
    assert day.end == datetime(2026, 10, 20, 21, 59, tzinfo=UTC)
    assert floating.start == datetime(2026, 10, 25, 21, 59, 59, tzinfo=UTC)  # read as Prague
    (as_utc,) = [e for e in sis.parse_feed(body, floating_tz=UTC) if e.uid == "b"]
    assert as_utc.start == datetime(2026, 10, 25, 22, 59, 59, tzinfo=UTC)


def test_empty_feed_and_garbage() -> None:
    assert sis.parse_feed(EMPTY) == []
    with pytest.raises(sis.SisError):
        sis.parse_feed(b"<html>Neopr\xc3\xa1vn\xc4\x9bn\xc3\xbd p\xc5\x99\xc3\xadstup</html>")


def test_body_hash_ignores_dtstamp() -> None:
    other = ROZVRHNG.replace(b"DTSTAMP:20261006T183045Z", b"DTSTAMP:20261107T010101Z")
    assert other != ROZVRHNG
    assert sis.body_hash(other) == sis.body_hash(ROZVRHNG)
    assert sis.body_hash(ROZVRHNG.replace(b"N8 - Building A", b"N9 - Building A")) != sis.body_hash(
        ROZVRHNG
    )


# --- Public pages ---


def test_course_page() -> None:
    page = sis.parse_course_page((SIS / "course_NINF030.html").read_text())
    assert page is not None
    assert (page.code, page.name, page.name_en) == ("NINF030", "Programování 1", "Programming 1")
    assert page.credits == 5
    assert (page.hours_lecture, page.hours_seminar, page.completion) == (2, 2, "Z")
    assert page.semester is Semester.WINTER
    assert page.faculty_code == "10000"
    assert page.department == "Katedra informatiky (32-KINF)"
    assert page.guarantor and page.guarantor.startswith("RNDr. Ivan Vzorný, Ph.D.; ")
    assert "Mgr. Martin Vzorek, Ph.D." in page.teachers
    assert page.language == "čeština, angličtina"
    assert page.web_url == "https://www.example.edu/~vzorny/"


def test_course_page_with_missing_labels_and_not_a_course() -> None:
    html = """<div class="form_div_title">Lineární algebra 1 - NMAT101</div>
    <table><tr><th>E-Kredity:</th><td>5</td></tr><tr><th>Nové pole:</th><td>x</td></tr>
    <tr><th>Rozsah, examinace:</th><td>zimní s.:2/2, Z+Zk [HT]</td></tr></table>"""
    page = sis.parse_course_page(html)
    assert page is not None
    assert (page.code, page.credits, page.completion) == ("NMAT101", 5, "Z+Zk")
    assert page.semester is None and page.guarantor is None and page.web_url is None
    assert sis.parse_course_page("<html><body>Chyba: neznámý předmět</body></html>") is None


def test_harmonogram_page() -> None:
    (row,) = sis.parse_harmonogram((SIS / "harmonogram_2026_public.html").read_text())
    assert row.title == "Akademický rok - výuka"
    assert row.faculty == "Uni"
    assert row.start == datetime(2026, 9, 28, 22, 0, tzinfo=UTC)  # 29.09.2026 00:00 Prague
    assert row.end == datetime(2027, 1, 8, 22, 59, tzinfo=UTC)
    assert row.note == "Výuka v zimním semestru"


# --- The polite client ---


def test_throttle_spaces_requests() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = sis.Throttle(2.0, clock=lambda: now[0], sleep=sleep)
    throttle.wait()
    now[0] += 0.5
    throttle.wait()
    now[0] += 3.0
    throttle.wait()
    assert slept == [1.5]


def _client(handler: httpx.MockTransport, *secrets: str) -> sis.PoliteClient:
    return sis.PoliteClient(
        list(secrets), throttle=sis.Throttle(0, sleep=lambda _: None), transport=handler
    )


def test_feed_fetch_statuses_user_agent_and_url() -> None:
    seen: list[httpx.Request] = []
    answers = {
        "rozvrhng": httpx.Response(200, content=ROZVRHNG),
        "term_st2": httpx.Response(403, text="forbidden"),
        "stev": httpx.Response(200, text="<html>login</html>"),
    }

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers[request.url.params["typ"]]

    source = sis.SisHttpSource(
        _client(httpx.MockTransport(handle), SECRET),
        "https://sis.example.edu/studium",
        ical_token=SECRET,
        timetable_key=None,
        feed_types=["rozvrhng", "term_st2", "stev"],
    )
    assert source.feeds == ["rozvrhng", "term_st2", "stev"]
    assert source.fetch_feed("rozvrhng").status is FetchStatus.OK
    denied = source.fetch_feed("term_st2")
    assert (denied.status, denied.error) == (FetchStatus.DENIED, "HTTP 403")
    assert source.fetch_feed("stev").error == "not a calendar"
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/studium/ical.php"
    assert request.url.params["token"] == SECRET
    assert request.headers["user-agent"].startswith("Orbit/")
    assert request.headers["user-agent"].endswith("personal-assistant")


def test_timetable_key_replaces_rozvrhng() -> None:
    source = sis.SisHttpSource(
        _client(httpx.MockTransport(lambda r: httpx.Response(200, content=ROZ_EXTERNI))),
        "https://sis.example.edu/studium",
        ical_token=SECRET,
        timetable_key="k" * 40,
        feed_types=["rozvrhng", "term_st2", "stev"],
    )
    assert source.feeds == ["roz_externi", "term_st2", "stev"]
    assert source.fetch_feed("roz_externi").status is FetchStatus.OK


def test_token_is_redacted_from_errors_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    source = sis.SisHttpSource(
        _client(httpx.MockTransport(handle), SECRET),
        "https://sis.example.edu/studium",
        ical_token=SECRET,
        timetable_key=None,
        feed_types=["rozvrhng"],
    )
    with caplog.at_level(logging.DEBUG):
        fetch = source.fetch_feed("rozvrhng")
    assert fetch.status is FetchStatus.ERROR
    assert fetch.error and "ical.php" in fetch.error
    assert SECRET not in fetch.error and sis.REDACTED in fetch.error
    assert SECRET not in caplog.text
    assert sis.redact(f"a {SECRET} b", [SECRET]) == "a [redacted] b"


# --- ReCodEx ---


def test_recodex_feed_and_titles() -> None:
    (event,) = sis.parse_feed((RECODEX / "ical.ics").read_bytes(), floating_tz=UTC)
    assert event.start == datetime(2026, 10, 25, 22, 59, 59, tzinfo=UTC)  # = API firstDeadline
    assert recodex.clean_title(event.summary) == "5P 0.1 Hello World"
    assert event.location == "Programming 1 (Thu, 9:50, N8)"
    assert recodex.assignment_id(event.uid) == "7766c471-2a3e-51d0-a399-1fdb06ea5a2f"


def test_recodex_groups_and_assignments() -> None:
    groups = recodex.parse_groups(json.loads((RECODEX / "user_groups.json").read_text()))
    by_code = {g.course_code: g for g in groups}
    assert set(by_code) == {"NINF062", "NINF030", "NINF141"}
    prog = by_code["NINF030"]
    assert prog.ticket == "26aNINF030x03" and prog.name == "Programování 1 (Čt, 9:50, N8)"
    assert prog.threshold == Decimal("0.7") and prog.points_limit is None
    assert prog.stats is not None and prog.stats.gained == 2 and prog.stats.passes is True
    assert prog.stats.points == {"7766c471-2a3e-51d0-a399-1fdb06ea5a2f": Decimal(1)}
    assert by_code["NINF141"].points_limit == 2
    (assignment,) = recodex.parse_assignments(
        json.loads((RECODEX / "group_assignments.json").read_text())
    )
    assert assignment.first_deadline == datetime(2026, 10, 25, 22, 59, 59, tzinfo=UTC)
    assert assignment.second_deadline is None
    assert assignment.max_points == 1 and assignment.is_bonus


def _jwt(payload: dict[str, object]) -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part(payload)}.signature"


def test_token_claims() -> None:
    claims = recodex.token_claims(_jwt({"sub": "user-1", "exp": 1822262400}))
    assert claims.user_id == "user-1"
    assert claims.expires == datetime(2027, 9, 30, tzinfo=UTC)
    assert recodex.token_claims("not a jwt") == recodex.TokenClaims(None, None)


def test_recodex_http_expired_and_denied() -> None:
    token = _jwt({"sub": "user-1", "exp": 1822262400})

    def handle(request: httpx.Request) -> httpx.Response:
        if "/ical/" in request.url.path:
            return httpx.Response(400, json={"msg": "Given iCal identifier has expired."})
        assert request.headers["authorization"] == f"Bearer {token}"
        return httpx.Response(401)

    source = recodex.RecodexHttpSource(
        _client(httpx.MockTransport(handle), SECRET, token),
        "https://recodex.example.edu/api/v1",
        ical_token=SECRET,
        api_token=token,
    )
    assert source.fetch_feed().status is FetchStatus.DENIED
    with pytest.raises(recodex.RecodexDenied):
        source.fetch_groups()
    assert source.assignment_url("abc") == "https://recodex.example.edu/app/assignment/abc"


# --- Owl ---


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/", True),
        ("/c/all", True),
        ("/c/zs2627/26aNMAT202x06/", True),
        ("/c/ls2526/la1-svobodova/", True),
        ("/c/zs2627/26aNMAT202x06/ukol-1/", False),  # a topic page marks threads read
        ("/c/ls2526/la1-svobodova/leave", False),
        ("/acct/login/", False),
        ("/acct/logout", False),
        ("/join/abc/", False),
        ("/settings/", False),
        ("/admin/course/zs2627/x/api-key/create", False),
        ("/c/zs2627/26aNMAT202x06", False),
    ],
)
def test_owl_path_allow_list(path: str, allowed: bool) -> None:
    assert owl.allowed_path(path) is allowed


def test_owl_course_list() -> None:
    refs = owl.parse_course_list((OWL / "all_courses.html").read_text())
    assert [(r.path, r.semester_label) for r in refs] == [
        ("zs2627/26aNMAT202x06", "Winter semester 2026/2027"),
        ("ls2526/la1-svobodova", "Summer semester 2025/2026"),
    ]
    assert refs[0].name == "Diskrétní matematika (Dr. Petr Dvořák, Čt 15:40)"


def test_owl_course_page_handbuilt() -> None:
    page = owl.parse_course_page((OWL / "course_NMAT202_handbuilt.html").read_text())
    assert page.title.startswith("Diskrétní matematika")
    assert page.description and page.description.startswith("Pro získání zápočtu")
    topics = {t.ident: t for t in page.topics}
    assert set(topics) == {"oznamy", "otazky", "ukol-1", "ukol-2", "zapocet"}
    hw = topics["ukol-2"]
    assert hw.group == "Domácí úkoly" and hw.title == "Křečci"
    assert hw.deadline == datetime(2026, 10, 15, 13, 40, tzinfo=UTC)  # 15:40 Prague
    assert (hw.points, hw.max_points) == (Decimal("1.50"), Decimal("3.00"))
    assert hw.href == "/c/zs2627/26aNMAT202x06/ukol-2/"
    assert topics["ukol-1"].points is None and topics["oznamy"].deadline is None
    assert (page.total_points, page.total_max, page.threshold) == (
        Decimal("1.50"),
        Decimal("5.00"),
        Decimal("100.00"),
    )


def test_owl_course_page_real() -> None:
    page = owl.parse_course_page((OWL / "course_la1_svobodova.html").read_text())
    assert page.title == "Lineární algebra 1"
    assert page.description and "www.example.edu/~svobodova/" in page.description
    (topic,) = page.topics
    assert topic.ident == "hw1" and topic.group is None
    assert topic.deadline == datetime(2026, 10, 20, 21, 59, tzinfo=UTC)
    assert topic.max_points == 20 and topic.points is None
    assert (page.total_points, page.total_max, page.threshold) == (Decimal(0), Decimal(20), None)


def test_owl_http_reader() -> None:
    cookie = "c" * 40
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/c/all":
            return httpx.Response(200, text=(OWL / "all_courses.html").read_text())
        return httpx.Response(302, headers={"location": "/acct/login/"})

    source = owl.OwlHttpSource(
        _client(httpx.MockTransport(handle), cookie), "https://owl.example.edu", cookie=cookie
    )
    refs = source.fetch_courses()
    assert seen[0].headers["cookie"] == f"owl_session={cookie}"
    assert all(r.method == "GET" for r in seen)
    with pytest.raises(owl.OwlDenied):
        source.fetch_course(refs[0])
    bad = owl.OwlCourseRef("zs2627", "26aNMAT202x06/ukol-1", "x", None)
    with pytest.raises(sis.SisError, match="refusing"):
        source.fetch_course(bad)
    assert len(seen) == 2  # the refused path was never requested


def test_school_year_helpers() -> None:
    assert sis.ticket_course_code("26aNINF030x03") == "NINF030"
    assert sis.ticket_year("27bNMAI058p1") == 2027
    assert sis.ticket_course_code("la1-svobodova") is None
