"""Custom review reports over a date range (SPEC §2.22)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.claude_job import ClaudeJob
from orbit.domain.enums import JobStatus, ReviewStatus
from orbit.domain.inbox import Notification
from orbit.domain.meetings import Meeting
from orbit.domain.report import ReviewReport
from orbit.integrations.claude import (
    ClaudeError,
    ReportAnswer,
    ReportOutput,
    ReportRequest,
    ReviewItem,
    build_report_prompt,
)
from orbit.services import claude_jobs, people, report_run, reports
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.notifications import QuietHours
from orbit.settings import Settings
from tests.test_notifications import FakeSender

TZ = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 7)
NO_QUIET = QuietHours(datetime.min.time(), datetime.min.time(), TZ)


def data(
    start: date = date(2026, 10, 1), end: date = date(2026, 10, 7), **kw: Any
) -> reports.ReportInput:
    return reports.ReportInput(start, end, **kw)


def answer(**kw: Any) -> ReportOutput:
    values: dict[str, Any] = {
        "summary": "A busy week.",
        "highlights": [ReviewItem(text="Met Paťo", goal_ids=[], person_ids=[])],
        "slips": [],
        "patterns": [],
        "questions": [],
    }
    values.update(kw)
    return ReportOutput(**values)


class FakeWriter:
    def __init__(self, result: ReportOutput | Exception | None = None) -> None:
        self.result = result or answer()
        self.requests: list[ReportRequest] = []

    def make_report(self, request: ReportRequest) -> ReportAnswer:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return ReportAnswer(self.result.model_copy(deep=True), "test-model", 900, 120)


def meeting(db: Session, day: date, title: str = "kafe") -> None:
    start = datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC)
    db.add(
        Meeting(
            calendar_uid=f"u-{title}-{day}",
            calendar_name="Personal",
            title=title,
            start_utc=start,
            end_utc=start + timedelta(hours=1),
        )
    )
    db.flush()


def pending(db: Session, **kw: Any) -> ReviewReport:
    return reports.create(db, data(**kw), TODAY, NOW)


# --- The range ---


def test_presets() -> None:
    assert reports.preset_range("last_7", TODAY) == (date(2026, 10, 1), TODAY)
    assert reports.preset_range("last_30", TODAY) == (date(2026, 9, 8), TODAY)
    assert reports.preset_range("this_month", TODAY) == (date(2026, 10, 1), TODAY)
    assert reports.preset_range("last_month", TODAY) == (date(2026, 9, 1), date(2026, 9, 30))
    assert reports.preset_range("this_semester", TODAY) == (date(2026, 9, 1), TODAY)
    assert reports.preset_range("this_semester", date(2027, 1, 20))[0] == date(2026, 10, 21)  # cut
    assert reports.preset_range("this_semester", date(2027, 3, 1))[0] == date(2027, 2, 1)
    with pytest.raises(InvalidInputError):
        reports.preset_range("nope", TODAY)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"start": date(2026, 10, 8), "end": date(2026, 10, 7)}, "ends before"),
        ({"start": date(2026, 6, 1), "end": date(2026, 10, 7)}, "at most 92 days"),
        ({"start": date(2026, 10, 1), "end": date(2026, 11, 7)}, "30 days ahead"),
        ({"focus_text": "x" * 501}, "500 characters"),
    ],
)
def test_ranges_that_are_not_allowed(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(InvalidInputError, match=message):
        reports.validate(data(**kw), TODAY)


def test_the_future_and_the_limits_are_allowed() -> None:
    ok = reports.validate(data(date(2026, 10, 7), date(2026, 11, 6), focus_text="  a   b "), TODAY)
    assert ok.focus_text == "a b"
    reports.validate(data(date(2026, 7, 8), date(2026, 10, 7)), TODAY)  # exactly 92 days


def test_one_report_is_made_at_a_time(db: Session) -> None:
    first = pending(db)
    with pytest.raises(InvalidInputError, match="Another report"):
        reports.create(db, data(), TODAY, NOW + timedelta(minutes=1))
    # A pending row left by a stopped server does not block a new one.
    assert reports.create(db, data(), TODAY, NOW + reports.STALE_AFTER + timedelta(minutes=1))
    assert first.status is ReviewStatus.PENDING


def test_links_must_exist(db: Session) -> None:
    for kw in ({"goal_id": 99}, {"person_id": 99}, {"course_id": 99}):
        with pytest.raises(InvalidInputError, match="does not exist"):
            reports.create(db, data(**kw), TODAY, NOW)


# --- Building and counting ---


def test_prepare_keeps_the_input_and_counts_the_numbers(db: Session) -> None:
    people.create_person(db, people.PersonCreate(display_name="Paťo"))
    meeting(db, date(2026, 10, 2))
    meeting(db, date(2026, 10, 20), "later")  # outside the range
    report = pending(db)
    request = reports.prepare(db, report, tz=TZ, budget_chars=400_000, now=NOW)
    assert request.start == date(2026, 10, 1) and request.today == TODAY
    assert report.input_json and report.input_json["snapshot"]["window"]["start"] == "2026-10-01"
    assert report.input_chars == len(request.snapshot_json)
    numbers = {n["label"]: n["value"] for n in report.numbers_json or []}
    assert numbers["Meetings"] == 1
    assert "Lessons" not in numbers  # no school data: no school rows
    assert numbers["Active goals now"] == 0


def test_a_person_focus_narrows_the_meetings(db: Session) -> None:
    paťo = people.create_person(db, people.PersonCreate(display_name="Paťo"))
    meeting(db, date(2026, 10, 2))
    report = pending(db, person_id=paťo.id, focus_text="friends")
    request = reports.prepare(db, report, tz=TZ, budget_chars=400_000, now=NOW)
    assert request.focus_person == "Paťo" and request.focus_text == "friends"
    assert (
        report.numbers_json
        and {n["label"]: n["value"] for n in report.numbers_json}["Meetings"] == 0
    )
    prompt = build_report_prompt(request)
    assert "Focus person: Paťo" in prompt and "Range: 2026-10-01 to 2026-10-07 (running)" in prompt


def test_the_prompt_says_whether_the_range_is_over_or_ahead() -> None:
    base = ReportRequest(TODAY, date(2026, 9, 1), date(2026, 9, 30), "{}")
    assert "(over)" in build_report_prompt(base)
    ahead = ReportRequest(TODAY, date(2026, 10, 8), date(2026, 10, 20), "{}")
    assert "(not started)" in build_report_prompt(ahead)


def test_an_iban_in_the_focus_is_masked_in_the_prompt() -> None:
    request = ReportRequest(TODAY, TODAY, TODAY, "{}", focus_text="pay SK3112000000198742637541")
    assert "SK3112000000198742637541" not in build_report_prompt(request)


# --- Generating ---


def test_generate_stores_the_answer_and_drops_unknown_ids(db: Session) -> None:
    writer = FakeWriter(
        answer(
            highlights=[
                ReviewItem(text="Real", goal_ids=[5], person_ids=[7]),
                ReviewItem(text="   ", goal_ids=[], person_ids=[]),
            ],
            questions=["a?", "b?", "c?", "d?", " "],
        )
    )
    report = pending(db)
    reports.generate(db, report, writer, tz=TZ, budget_chars=400_000, now=NOW)
    assert report.status is ReviewStatus.READY and report.claude_used
    assert (report.model, report.input_tokens, report.output_tokens) == ("test-model", 900, 120)
    out = report.output_json or {}
    assert [i["text"] for i in out["highlights"]] == ["Real"]
    assert out["highlights"][0]["goal_ids"] == [] and out["highlights"][0]["person_ids"] == []
    assert out["questions"] == ["a?", "b?", "c?"]


def test_a_claude_error_fails_the_report_and_try_again_works(db: Session) -> None:
    report = pending(db)
    reports.generate(
        db, report, FakeWriter(ClaudeError("Claude API error: Timeout")), tz=TZ, budget_chars=1000
    )
    assert report.status is ReviewStatus.FAILED and "Timeout" in (report.error or "")
    with pytest.raises(InvalidInputError, match="Only a failed"):
        reports.reopen(db, pending_id(db, ready=True), NOW)
    again = reports.reopen(db, report.id, NOW)
    assert again.status is ReviewStatus.PENDING and again.error is None and again.attempts == 1


def pending_id(db: Session, *, ready: bool) -> int:
    row = ReviewReport(
        range_start=TODAY,
        range_end=TODAY,
        status=ReviewStatus.READY if ready else ReviewStatus.PENDING,
        attempts=1,
        input_tokens=0,
        output_tokens=0,
        claude_used=False,
    )
    db.add(row)
    db.flush()
    return row.id


def test_finish_needs_the_stored_input(db: Session) -> None:
    report = pending(db)
    with pytest.raises(InvalidInputError, match="stored input"):
        reports.finish(db, report, answer())


def test_fail_never_unmakes_a_ready_report(db: Session) -> None:
    report = pending(db)
    reports.generate(db, report, FakeWriter(), tz=TZ, budget_chars=400_000, now=NOW)
    reports.fail(db, report.id, "late failure")
    assert report.status is ReviewStatus.READY


def test_plain_report_lists_what_the_range_held(db: Session) -> None:
    meeting(db, date(2026, 10, 2), "kafe s Paťom")
    report = pending(db)
    reports.prepare(db, report, tz=TZ, budget_chars=400_000, now=NOW)
    reports.finish_plain(db, report, "Claude is off.")
    assert report.status is ReviewStatus.READY and not report.claude_used
    from orbit.services.context import ContextSnapshot

    sections = reports.plain_sections(
        ContextSnapshot.model_validate((report.input_json or {})["snapshot"])
    )
    assert [s.title for s in sections] == ["Meetings"]
    assert "kafe s Paťom" in sections[0].lines[0]
    markdown = reports.to_markdown(report, {"goals": {}, "people": {}})
    assert markdown.startswith("# Report 1 Oct to 7 Oct 2026") and "- Meetings: 1" in markdown
    assert "kafe s Paťom" in markdown


def test_markdown_of_a_claude_report(db: Session) -> None:
    report = pending(db)
    reports.generate(
        db,
        report,
        FakeWriter(answer(questions=["What next?"])),
        tz=TZ,
        budget_chars=400_000,
        now=NOW,
    )
    md = reports.to_markdown(report, reports.lookup_names(db, report.output_json or {}))
    assert "A busy week." in md and "## Highlights" in md and "- Met Paťo" in md
    assert "## Questions" in md and "- What next?" in md


def test_title_and_delete(db: Session) -> None:
    report = pending(db, start=date(2025, 12, 20), end=date(2026, 1, 5))
    assert reports.title(report) == "20 Dec 2025 to 5 Jan 2026"
    reports.delete_report(db, report.id)
    with pytest.raises(NotFoundError):
        reports.get_report(db, report.id)


def test_one_push_per_report(db: Session) -> None:
    report = pending(db)
    reports.generate(db, report, FakeWriter(), tz=TZ, budget_chars=400_000, now=NOW)
    sender = FakeSender()
    assert reports.send_push(db, report, sender, base_url="http://x", quiet=NO_QUIET, now=NOW)
    assert not reports.send_push(db, report, sender, base_url="http://x", quiet=NO_QUIET, now=NOW)
    assert len(sender.sent) == 1 and sender.sent[0].click == f"http://x/reports/{report.id}"
    assert db.scalars(select(Notification.dedupe_key)).one() == f"report:{report.id}"
    failed = pending_id(db, ready=False)
    assert not reports.send_push(
        db, reports.get_report(db, failed), sender, base_url="x", quiet=NO_QUIET
    )


# --- Paths: API client, worker, none ---


def alive(db: Session, auth_ok: bool = True, now: datetime = NOW) -> None:
    claude_jobs.heartbeat(db, auth_ok=auth_ok, now=now)


def test_start_without_claude_makes_a_plain_report_at_once(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    started = report_run.start(db, data(), settings=make_settings(), writer=None, now=NOW)
    assert started.mode == "plain"
    report = reports.get_report(db, started.report_id)
    assert report.status is ReviewStatus.READY and not report.claude_used
    assert "not set up" in (report.plain_reason or "") and report.numbers_json


def test_start_with_the_api_client_leaves_the_row_pending(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    started = report_run.start(db, data(), settings=make_settings(), writer=FakeWriter(), now=NOW)
    assert started.mode == "api"
    assert reports.get_report(db, started.report_id).status is ReviewStatus.PENDING


def test_run_api_makes_the_report_and_pushes(app: FastAPI, make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(
        quiet_hours=NO_QUIET_HOURS
    )  # not held back, whatever time the suite runs
    sessions = app.state.sessionmaker
    with sessions() as db:
        report_id = report_run.start(db, data(), settings=settings, writer=FakeWriter()).report_id
        db.commit()
    sender = FakeSender()
    report_run.run_api(sessions, report_id, FakeWriter(), sender, settings)
    with sessions() as db:
        report = reports.get_report(db, report_id)
        assert report.status is ReviewStatus.READY and report.claude_used
    assert len(sender.sent) == 1
    report_run.run_api(sessions, report_id, FakeWriter(), sender, settings)  # nothing to do
    assert len(sender.sent) == 1


def test_run_api_records_a_bug_instead_of_leaving_it_pending(
    app: FastAPI, settings: Settings
) -> None:
    sessions = app.state.sessionmaker
    with sessions() as db:
        report_id = report_run.start(db, data(), settings=settings, writer=FakeWriter()).report_id
        db.commit()
    report_run.run_api(sessions, report_id, FakeWriter(RuntimeError("boom")), None, settings)  # type: ignore[arg-type]
    with sessions() as db:
        report = reports.get_report(db, report_id)
        assert report.status is ReviewStatus.FAILED and "boom" in (report.error or "")


NO_QUIET_HOURS = (time(0, 0), time(0, 0))  # no quiet hours: a push must not depend on the clock


def worker_settings(make_settings) -> Settings:  # type: ignore[no-untyped-def]
    return make_settings(orbit_review_token="r" * 40, quiet_hours=NO_QUIET_HOURS)


def test_worker_path_queues_a_long_job(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    alive(db)
    started = report_run.start(
        db, data(), settings=worker_settings(make_settings), writer=None, now=NOW
    )
    assert started.mode == "worker"
    job = db.scalars(select(ClaudeJob)).one()
    assert (job.kind, job.status, job.report_id) == ("report", JobStatus.QUEUED, started.report_id)
    assert job.wait_until == NOW + claude_jobs.REPORT_WAIT
    assert reports.get_report(db, started.report_id).input_json  # stored for the check
    claimed = claude_jobs.claim(db, NOW)
    assert claimed is not None and claimed.lease_until == NOW + claude_jobs.REVIEW_LEASE


def test_worker_path_without_a_live_worker_falls_back_to_plain(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    started = report_run.start(
        db, data(), settings=worker_settings(make_settings), writer=None, now=NOW
    )
    assert started.mode == "plain"
    assert "not running" in (reports.get_report(db, started.report_id).plain_reason or "")
    assert db.scalars(select(ClaudeJob)).first() is None
    alive(db, auth_ok=False)
    db.rollback()


def queued_report(db: Session, make_settings) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    alive(db)
    started = report_run.start(
        db, data(), settings=worker_settings(make_settings), writer=None, now=NOW
    )
    job = claude_jobs.claim(db, NOW)
    assert job is not None
    return started.report_id, job.id


def test_a_worker_answer_finishes_the_report(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    report_id, job_id = queued_report(db, make_settings)
    claude_jobs.submit(
        db,
        job_id,
        answer(questions=["q?"]).model_dump(mode="json"),
        model="m",
        input_tokens=5,
        output_tokens=2,
        now=NOW,
    )
    report = reports.get_report(db, report_id)
    assert report.status is ReviewStatus.READY and report.claude_used and report.model == "m"
    assert (report.output_json or {})["questions"] == ["q?"]


def test_a_bad_worker_answer_is_refused_and_the_report_stays_pending(
    db: Session, make_settings
) -> None:  # type: ignore[no-untyped-def]
    report_id, job_id = queued_report(db, make_settings)
    with pytest.raises(claude_jobs.JobOutputError):
        claude_jobs.submit(db, job_id, {"summary": 1}, now=NOW)
    assert reports.get_report(db, report_id).status is ReviewStatus.PENDING


def test_worker_failures_fail_the_report_after_the_last_try(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    report_id, job_id = queued_report(db, make_settings)
    claude_jobs.fail(db, job_id, "crashed", now=NOW)  # one more try
    assert reports.get_report(db, report_id).status is ReviewStatus.PENDING
    job = claude_jobs.claim(db, NOW)
    assert job is not None
    claude_jobs.fail(db, job.id, "crashed again", now=NOW)
    report = reports.get_report(db, report_id)
    assert report.status is ReviewStatus.FAILED and "crashed again" in (report.error or "")


def test_a_rate_limit_keeps_the_report_waiting(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    report_id, job_id = queued_report(db, make_settings)
    claude_jobs.fail(db, job_id, "limit", kind="rate_limit", now=NOW)
    assert reports.get_report(db, report_id).status is ReviewStatus.PENDING


def test_an_untaken_job_expires_and_fails_the_report(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    alive(db)
    started = report_run.start(
        db, data(), settings=worker_settings(make_settings), writer=None, now=NOW
    )
    claude_jobs.housekeeping(db, NOW + claude_jobs.REPORT_WAIT + timedelta(minutes=1))
    report = reports.get_report(db, started.report_id)
    assert report.status is ReviewStatus.FAILED and "in time" in (report.error or "")


def test_a_report_job_is_not_rerun_from_the_queue(db: Session, make_settings) -> None:  # type: ignore[no-untyped-def]
    report_id, job_id = queued_report(db, make_settings)
    claude_jobs.fail(db, job_id, "x", kind="auth", now=NOW)
    with pytest.raises(InvalidInputError, match="own page"):
        claude_jobs.rerun(db, job_id, claude_jobs.QueueLimits())
    assert reports.get_report(db, report_id).status is ReviewStatus.FAILED


# --- API ---


def use_writer(app: FastAPI, writer: Any) -> Any:
    app.state.report_writer = writer
    return writer


def test_api_needs_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/reports").status_code == 401
    assert client.post("/api/v1/reports", json={}).status_code == 401


def test_api_makes_a_report_in_the_background(api: TestClient, app: FastAPI) -> None:
    writer = use_writer(app, FakeWriter())
    made = api.post(
        "/api/v1/reports", json={"start": "2026-10-01", "end": "2026-10-07", "focus": "gym"}
    )
    assert made.status_code == 202
    report_id = made.json()["id"]
    got = api.get(f"/api/v1/reports/{report_id}").json()  # the background task has run
    assert got["status"] == "ready" and got["claude_used"] and got["focus_text"] == "gym"
    assert got["output"]["summary"] == "A busy week." and got["numbers"]
    assert writer.requests[0].focus_text == "gym"
    assert api.get("/api/v1/reports").json()[0]["id"] == report_id
    assert "snapshot" in api.get(f"/api/v1/reports/{report_id}/input").json()


def test_api_without_claude_gives_a_plain_report(api: TestClient) -> None:
    made = api.post("/api/v1/reports", json={"start": "2026-10-01", "end": "2026-10-07"}).json()
    assert made["status"] == "ready" and not made["claude_used"] and made["plain_reason"]


def test_api_refuses_bad_ranges_and_a_second_report(api: TestClient, app: FastAPI) -> None:
    for body in (
        {"start": "2026-10-08", "end": "2026-10-01"},
        {"start": "2026-01-01", "end": "2026-10-01"},
        {"start": "2026-10-01", "end": "2030-01-01"},
        {"start": "2026-10-01", "end": "2026-10-02", "goal_id": 99},
    ):
        assert api.post("/api/v1/reports", json=body).status_code == 422
    with app.state.sessionmaker() as db:
        reports.create(db, data(), TODAY, datetime.now(UTC))  # the API uses the real clock
        db.commit()
    again = api.post("/api/v1/reports", json={"start": "2026-10-01", "end": "2026-10-02"})
    assert again.status_code == 422 and "Another report" in again.text


def test_api_retry_and_delete(api: TestClient, app: FastAPI) -> None:
    use_writer(app, FakeWriter(ClaudeError("Claude API error: Timeout")))
    report_id = api.post(
        "/api/v1/reports", json={"start": "2026-10-01", "end": "2026-10-07"}
    ).json()["id"]
    assert api.get(f"/api/v1/reports/{report_id}").json()["status"] == "failed"
    use_writer(app, FakeWriter())
    assert api.post(f"/api/v1/reports/{report_id}/retry").status_code == 202
    assert api.get(f"/api/v1/reports/{report_id}").json()["status"] == "ready"
    assert (
        api.post(f"/api/v1/reports/{report_id}/retry").status_code == 422
    )  # ready: nothing to retry
    assert api.delete(f"/api/v1/reports/{report_id}").status_code == 204
    assert api.get(f"/api/v1/reports/{report_id}").status_code == 404


def test_worker_submit_pushes_when_the_report_is_ready(
    api: TestClient, app: FastAPI, make_settings
) -> None:  # type: ignore[no-untyped-def]
    sender = FakeSender()
    app.state.push_sender = sender
    with app.state.sessionmaker() as db:
        alive(db, auth_ok=True, now=datetime.now(UTC))  # the API uses the real clock
        db.commit()
    app.state.settings = worker_settings(make_settings)
    report_id = api.post(
        "/api/v1/reports", json={"start": "2026-10-01", "end": "2026-10-07"}
    ).json()["id"]
    assert api.get(f"/api/v1/reports/{report_id}").json()["status"] == "pending"
    token = {"Authorization": "Bearer " + "r" * 40}
    job = api.post("/api/v1/claude-worker/claim", headers=token).json()
    assert job["kind"] == "report"
    sent = api.post(
        f"/api/v1/claude-worker/jobs/{job['id']}/submit",
        headers=token,
        json={"output": answer().model_dump(mode="json"), "model": "m"},
    )
    assert sent.status_code == 200
    assert api.get(f"/api/v1/reports/{report_id}").json()["status"] == "ready"
    assert len(sender.sent) == 1


# --- Web ---


def test_pages_need_login(client: TestClient) -> None:
    for path in ("/reports", "/reports/new", "/reports/1"):
        assert client.get(path, follow_redirects=False).status_code == 303
    assert client.post("/reports", data={}, follow_redirects=False).status_code == 303


def test_form_has_presets_and_prefills_the_dates(web: TestClient) -> None:
    page = web.get("/reports/new?preset=last_month")
    assert page.status_code == 200
    assert 'name="start"' in page.text and 'name="focus"' in page.text
    assert "Last 30 days" in page.text and "92 days" in page.text
    assert "/reports" in web.get("/").text  # in the nav


def test_making_a_report_from_the_page_and_reading_it(web: TestClient, app: FastAPI) -> None:
    use_writer(app, FakeWriter(answer(questions=["Sleep more?"])))
    response = web.post(
        "/reports",
        data={"start": "2026-10-01", "end": "2026-10-07", "focus": "exam prep"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = web.get(response.headers["location"]).text
    assert "A busy week." in page and "Sleep more?" in page and "Met Paťo" in page
    assert "Focus: exam prep" in page and "Numbers" in page and "Copy as Markdown" in page
    assert (
        "Print / save as PDF" in page and "What Claude saw" not in page
    ) or "characters read" in page
    assert "exam prep" in web.get("/reports").text


def test_page_without_claude_says_so(web: TestClient) -> None:
    response = web.post(
        "/reports", data={"start": "2026-10-01", "end": "2026-10-07"}, follow_redirects=True
    )
    assert "not set up" in response.text and "Numbers" in response.text


def test_a_pending_report_refreshes_itself_and_a_failed_one_offers_try_again(
    web: TestClient, app: FastAPI
) -> None:
    with app.state.sessionmaker() as db:
        waiting = pending(db).id
        failed = pending_id(db, ready=False)
        db.get(ReviewReport, failed).status = ReviewStatus.FAILED  # type: ignore[union-attr]
        db.get(ReviewReport, failed).error = "Claude API error: Timeout"  # type: ignore[union-attr]
        db.commit()
    assert 'http-equiv="refresh"' in web.get(f"/reports/{waiting}").text
    page = web.get(f"/reports/{failed}").text
    assert "Timeout" in page and "Try again" in page and 'http-equiv="refresh"' not in page


def test_bad_form_input_shows_the_error(web: TestClient) -> None:
    for form, text in (
        ({"start": "", "end": ""}, "Pick a start and an end date."),
        ({"start": "2026-10-08", "end": "2026-10-01"}, "ends before"),
    ):
        assert text in web.post("/reports", data=form, follow_redirects=True).text


def test_retry_and_delete_from_the_page(web: TestClient, app: FastAPI) -> None:
    use_writer(app, FakeWriter(ClaudeError("Claude API error: Timeout")))
    location = web.post(
        "/reports", data={"start": "2026-10-01", "end": "2026-10-07"}, follow_redirects=False
    ).headers["location"]
    assert "Try again" in web.get(location).text
    use_writer(app, FakeWriter())
    web.post(f"{location}/retry")
    assert "A busy week." in web.get(location).text
    assert "Only a failed" in web.post(f"{location}/retry", follow_redirects=True).text
    deleted = web.post(f"{location}/delete", follow_redirects=True)
    assert "Report deleted." in deleted.text and "No reports yet" in deleted.text
