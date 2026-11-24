"""Custom review reports over a date range (SPEC §2.22). Callers commit, except where noted.

A report is one context snapshot for the range, read by Claude (API client or the Claude worker)
into a read-only `ReportOutput`. Without Claude Orbit still makes the counted numbers and a plain
list of what the range held, so the page is never empty. Reports write no proposals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import ReviewStatus
from orbit.domain.goals import Goal
from orbit.domain.people import Person
from orbit.domain.report import ReviewReport
from orbit.domain.review import WeeklyReview
from orbit.domain.school import Course
from orbit.integrations.claude import (
    ClaudeError,
    ReportOutput,
    ReportRequest,
    ReportWriter,
    ReviewMemory,
)
from orbit.integrations.ntfy import PushMessage, PushSender, view_action
from orbit.services import notifications, review
from orbit.services.context import (
    ContextFocus,
    ContextSnapshot,
    ContextWindow,
    build_context,
)
from orbit.services.context.signals import compute_signals
from orbit.services.context.windows import MAX_WINDOW_DAYS
from orbit.services.errors import InvalidInputError, NotFoundError

log = logging.getLogger(__name__)

PUSH_KIND = "report"
MAX_DAYS = MAX_WINDOW_DAYS  # the longest range a report covers
MAX_AHEAD_DAYS = 30  # a range may end this far after today
FOCUS_CHARS = 500
STALE_AFTER = timedelta(minutes=15)  # a "pending" row older than this was left by a stopped server
MAX_ITEMS = 12  # per section
MAX_QUESTIONS = 3
PRESETS = ("last_7", "last_30", "this_month", "last_month", "this_semester")
PRESET_LABELS = {
    "last_7": "Last 7 days",
    "last_30": "Last 30 days",
    "this_month": "This month so far",
    "last_month": "Last month",
    "this_semester": "This semester so far",
}


# --- The range ---


@dataclass(frozen=True)
class ReportInput:
    start: date
    end: date
    focus_text: str | None = None
    goal_id: int | None = None
    person_id: int | None = None
    course_id: int | None = None


def preset_range(name: str, today: date) -> tuple[date, date]:
    """The dates of a preset. A semester is Sept to Jan (winter) or Feb to Aug (summer), cut to the
    longest range a report may cover."""
    if name == "last_7":
        return today - timedelta(days=6), today
    if name == "last_30":
        return today - timedelta(days=29), today
    if name == "this_month":
        return today.replace(day=1), today
    if name == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if name == "this_semester":
        if today.month >= 9:
            start = date(today.year, 9, 1)
        elif today.month == 1:
            start = date(today.year - 1, 9, 1)
        else:
            start = date(today.year, 2, 1)
        return max(start, today - timedelta(days=MAX_DAYS - 1)), today
    raise InvalidInputError(f"Unknown preset: {name}. Use one of {', '.join(PRESETS)}.")


def validate(data: ReportInput, today: date) -> ReportInput:
    """The input with trimmed focus text; InvalidInputError when the range is not allowed."""
    if data.end < data.start:
        raise InvalidInputError("The range ends before it starts.")
    if (data.end - data.start).days + 1 > MAX_DAYS:
        raise InvalidInputError(f"A report can cover at most {MAX_DAYS} days.")
    if data.end > today + timedelta(days=MAX_AHEAD_DAYS):
        raise InvalidInputError(f"A report can look at most {MAX_AHEAD_DAYS} days ahead.")
    focus = " ".join((data.focus_text or "").split())
    if len(focus) > FOCUS_CHARS:
        raise InvalidInputError(f"The focus can be at most {FOCUS_CHARS} characters.")
    return ReportInput(
        data.start, data.end, focus or None, data.goal_id, data.person_id, data.course_id
    )


# --- Queries ---


def get_report(session: Session, report_id: int) -> ReviewReport:
    report = session.get(ReviewReport, report_id)
    if report is None:
        raise NotFoundError(f"Report {report_id} not found.")
    return report


def list_reports(session: Session, limit: int = 50) -> list[ReviewReport]:
    return list(session.scalars(select(ReviewReport).order_by(ReviewReport.id.desc()).limit(limit)))


def delete_report(session: Session, report_id: int) -> None:
    """Remove a report and the personal context it kept. A queued job for it is left to fail."""
    session.delete(get_report(session, report_id))
    session.flush()


def running(session: Session, now: datetime) -> ReviewReport | None:
    """A report being made right now (a pending one older than STALE_AFTER does not count)."""
    return session.scalars(
        select(ReviewReport).where(
            ReviewReport.status == ReviewStatus.PENDING,
            ReviewReport.updated_at > now - STALE_AFTER,
        )
    ).first()


# --- Making the row ---


def _check_links(session: Session, data: ReportInput) -> None:
    if data.goal_id is not None and session.get(Goal, data.goal_id) is None:
        raise InvalidInputError("That goal does not exist.")
    if data.person_id is not None and session.get(Person, data.person_id) is None:
        raise InvalidInputError("That person does not exist.")
    if data.course_id is not None and session.get(Course, data.course_id) is None:
        raise InvalidInputError("That course does not exist.")


def create(
    session: Session, data: ReportInput, today: date, now: datetime | None = None
) -> ReviewReport:
    """Insert a pending report. One report is made at a time: another one running is refused."""
    moment = now or utcnow()
    data = validate(data, today)
    _check_links(session, data)
    if running(session, moment) is not None:
        raise InvalidInputError("Another report is being made. Wait for it to finish.")
    report = ReviewReport(
        range_start=data.start,
        range_end=data.end,
        focus_text=data.focus_text,
        focus_goal_id=data.goal_id,
        focus_person_id=data.person_id,
        focus_course_id=data.course_id,
        status=ReviewStatus.PENDING,
        attempts=1,
        input_tokens=0,
        output_tokens=0,
        claude_used=False,
        updated_at=moment,
    )
    session.add(report)
    session.flush()
    return report


def reopen(session: Session, report_id: int, now: datetime | None = None) -> ReviewReport:
    """Try again: a failed report, or one left pending by a stopped server, is made from zero."""
    moment = now or utcnow()
    report = get_report(session, report_id)
    stale = report.status is ReviewStatus.PENDING and report.updated_at <= moment - STALE_AFTER
    if report.status is not ReviewStatus.FAILED and not stale:
        raise InvalidInputError("Only a failed report can be tried again.")
    if running(session, moment) is not None:
        raise InvalidInputError("Another report is being made. Wait for it to finish.")
    report.status = ReviewStatus.PENDING
    report.attempts = 1
    report.error = None
    report.updated_at = moment
    session.flush()
    return report


# --- What Claude is given ---


def _labels(session: Session, report: ReviewReport) -> tuple[str | None, str | None, str | None]:
    goal = session.get(Goal, report.focus_goal_id) if report.focus_goal_id else None
    person = session.get(Person, report.focus_person_id) if report.focus_person_id else None
    course = session.get(Course, report.focus_course_id) if report.focus_course_id else None
    return (
        goal.title if goal else None,
        person.display_name if person else None,
        f"{course.code} {course.name}" if course else None,
    )


def _memory(session: Session, report: ReviewReport) -> ReviewMemory | None:
    """The latest ready weekly review whose reviewed or planned week overlaps the range."""
    row = session.scalars(
        select(WeeklyReview)
        .where(
            WeeklyReview.status == ReviewStatus.READY,
            WeeklyReview.review_start <= report.range_end,
            WeeklyReview.plan_end >= report.range_start,
        )
        .order_by(WeeklyReview.plan_start.desc())
        .limit(1)
    ).first()
    if row is None or not row.output_json:
        return None
    return ReviewMemory(
        week=f"{row.review_start} to {row.plan_end}",
        summary=str(row.output_json.get("summary", "")),
        priorities=(),
    )


def build_request(
    session: Session, report: ReviewReport, *, tz: ZoneInfo, budget_chars: int, now: datetime
) -> tuple[ReportRequest, dict[str, Any], ContextSnapshot]:
    """The request for Claude, the same data as JSON to keep, and the snapshot."""
    focus = ContextFocus(person_id=report.focus_person_id) if report.focus_person_id else None
    window = ContextWindow(report.range_start, report.range_end, tz)
    snapshot = build_context(
        session,
        window,
        focus,
        budget_chars=budget_chars,
        now=now,
    )
    goal, person, course = _labels(session, report)
    memory = _memory(session, report)
    request = ReportRequest(
        today=now.astimezone(tz).date(),
        start=report.range_start,
        end=report.range_end,
        snapshot_json=snapshot.prompt_json(),
        signals_json=compute_signals(
            session,
            review=window,
            plan=window,
            today=now.astimezone(tz).date(),
            tz=tz,
        ).model_dump_json(),
        focus_text=report.focus_text,
        focus_goal=goal,
        focus_person=person,
        focus_course=course,
        previous=memory,
    )
    kept = {
        "today": request.today.isoformat(),
        "snapshot": snapshot.model_dump(mode="json"),
        "focus": {
            "text": report.focus_text,
            "goal": goal,
            "person": person,
            "course": course,
        },
        "weekly_review": None
        if memory is None
        else {"week": memory.week, "summary": memory.summary},
    }
    return request, kept, snapshot


def prepare(
    session: Session,
    report: ReviewReport,
    *,
    tz: ZoneInfo,
    budget_chars: int,
    now: datetime | None = None,
) -> ReportRequest:
    """Build what Claude is given and keep it, with the counted numbers, on the report. The
    caller commits."""
    request, kept, snapshot = build_request(
        session, report, tz=tz, budget_chars=budget_chars, now=now or utcnow()
    )
    report.input_json = kept
    report.input_chars = len(request.snapshot_json)
    report.numbers_json = numbers(snapshot)
    session.flush()
    return request


# --- Numbers and the plain report (counted by Orbit, never by Claude) ---


def numbers(snapshot: ContextSnapshot) -> list[dict[str, Any]]:
    """A few facts the providers hold for the range, counted from the snapshot."""
    window = ContextWindow(snapshot.window.start, snapshot.window.end, ZoneInfo(snapshot.window.tz))
    people = snapshot.people.people
    notes = sum(1 for p in people for n in p.notes if window.contains(n.on))
    contacted = {p.id for p in people if p.interactions}
    promises = snapshot.promises
    school = snapshot.school
    rows: list[dict[str, Any]] = [
        {"label": "Meetings", "value": len(snapshot.meetings.meetings)},
        {"label": "Notes written", "value": notes},
        {"label": "Interactions logged", "value": sum(len(p.interactions) for p in people)},
        {"label": "People contacted", "value": len(contacted)},
        {"label": "Active goals now", "value": len(snapshot.goals.goals)},
        {"label": "Open promises now", "value": len(promises.promises)},
        {"label": "Overdue promises now", "value": sum(p.overdue for p in promises.promises)},
        {"label": "Promises closed (last 30 days)", "value": len(promises.recently_closed)},
    ]
    exams = [i for i in school.exams_and_deadlines if i.in_window and i.kind.value == "exam"]
    deadlines = [i for i in school.exams_and_deadlines if i.in_window and i.kind.value != "exam"]
    if school.lessons or school.exams_and_deadlines or school.courses:
        hours = school.workload.lesson_hours_in_window if school.workload else 0
        rows += [
            {"label": "Lessons", "value": len(school.lessons), "note": f"{hours:g} hours"},
            {"label": "Exams in the range", "value": len(exams)},
            {"label": "Deadlines in the range", "value": len(deadlines)},
        ]
    trimmed = sum(snapshot.budget.trimmed.values())
    if trimmed:
        rows.append({"label": "Cut to fit", "value": trimmed, "note": "counts above may be low"})
    return rows


@dataclass(frozen=True)
class PlainSection:
    title: str
    lines: list[str]


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plain_sections(snapshot: ContextSnapshot) -> list[PlainSection]:
    """What the range held, as plain lines (the report without Claude)."""
    sections = [
        PlainSection(
            "Meetings",
            [
                f"{m.start:%a %-d %b %H:%M} {m.title}"
                + (f" (with {', '.join(p.name for p in m.people)})" if m.people else "")
                for m in snapshot.meetings.meetings
            ],
        ),
        PlainSection(
            "Notes",
            [
                f"{n.on:%-d %b} {p.name}: {_clip(n.text)}"
                for p in snapshot.people.people
                for n in p.notes
                if snapshot.window.start <= n.on <= snapshot.window.end
            ],
        ),
        PlainSection(
            "Exams and deadlines",
            [
                f"{i.start:%a %-d %b} {i.title}" + (f" ({i.course.code})" if i.course else "")
                for i in snapshot.school.exams_and_deadlines
                if i.in_window
            ],
        ),
        PlainSection(
            "Open promises now",
            [
                f"{p.person}: {_clip(p.text)}"
                + (f" (due {p.due:%-d %b}{', overdue' if p.overdue else ''})" if p.due else "")
                for p in snapshot.promises.promises
            ],
        ),
        PlainSection("Goals now", [f"{g.rank}. {g.title}" for g in snapshot.goals.goals]),
    ]
    return [s for s in sections if s.lines]


# --- Checking the answer against what Claude was shown ---


def sanitize(output: ReportOutput, snapshot: ContextSnapshot) -> ReportOutput:
    """Drop ids Claude was not shown, empty items, and what is past the caps."""
    known = review.known_ids(snapshot)
    for section in (output.highlights, output.slips, output.patterns):
        section[:] = [item for item in section if item.text.strip()][:MAX_ITEMS]
        for item in section:
            item.goal_ids = list(dict.fromkeys(i for i in item.goal_ids if i in known.goals))
            item.person_ids = list(dict.fromkeys(i for i in item.person_ids if i in known.people))
    output.questions = [q.strip() for q in output.questions if q.strip()][:MAX_QUESTIONS]
    return output


def finish(
    session: Session,
    report: ReviewReport,
    output: ReportOutput,
    *,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> ReviewReport:
    """Check Claude's answer against the stored snapshot and keep it. The caller commits."""
    kept = report.input_json
    if not kept:
        raise InvalidInputError("This report has no stored input to check the answer against.")
    snapshot = ContextSnapshot.model_validate(kept["snapshot"])
    report.output_json = sanitize(output, snapshot).model_dump(mode="json")
    report.model = model
    report.input_tokens = input_tokens
    report.output_tokens = output_tokens
    report.status = ReviewStatus.READY
    report.claude_used = True
    report.error = None
    report.plain_reason = None
    session.flush()
    return report


def finish_plain(session: Session, report: ReviewReport, reason: str) -> ReviewReport:
    """Ready without Claude text: the numbers and the plain list are shown. The caller commits."""
    report.status = ReviewStatus.READY
    report.claude_used = False
    report.output_json = None
    report.plain_reason = reason[:300]
    report.error = None
    session.flush()
    return report


def fail(session: Session, report_id: int, message: str) -> None:
    """Mark a report failed (a Claude error, or a bug). A ready report stays ready."""
    report = session.get(ReviewReport, report_id)
    if report is not None and report.status is not ReviewStatus.READY:
        report.status = ReviewStatus.FAILED
        report.error = message[:1000]
        session.flush()


def generate(
    session: Session,
    report: ReviewReport,
    writer: ReportWriter,
    *,
    tz: ZoneInfo,
    budget_chars: int,
    now: datetime | None = None,
) -> ReviewReport:
    """Ask Claude through the API and store the answer, or mark the report failed. Never raises
    for a Claude error. The caller commits."""
    request = prepare(session, report, tz=tz, budget_chars=budget_chars, now=now)
    try:
        answer = writer.make_report(request)
    except ClaudeError as exc:
        report.status = ReviewStatus.FAILED
        report.error = str(exc)
        session.flush()
        log.warning("report %s failed: %s", report.id, exc)
        return report
    return finish(
        session,
        report,
        answer.output,
        model=answer.model,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
    )


# --- Names, the push and Markdown ---


def title(report: ReviewReport) -> str:
    start, end = report.range_start, report.range_end
    if start == end:
        return f"{start:%-d %b %Y}"
    if start.year == end.year:
        return f"{start:%-d %b} to {end:%-d %b %Y}"
    return f"{start:%-d %b %Y} to {end:%-d %b %Y}"


def lookup_names(session: Session, output: dict[str, Any]) -> dict[str, dict[int, str]]:
    """Titles and names for the ids in a report's items."""
    goal_ids: set[int] = set()
    person_ids: set[int] = set()
    for key in ("highlights", "slips", "patterns"):
        for item in output.get(key, []):
            goal_ids.update(item.get("goal_ids", []))
            person_ids.update(item.get("person_ids", []))
    goals = {g.id: g.title for g in session.scalars(select(Goal).where(Goal.id.in_(goal_ids)))}
    people = {
        p.id: p.display_name
        for p in session.scalars(select(Person).where(Person.id.in_(person_ids)))
    }
    return {"goals": goals, "people": people}


def to_markdown(report: ReviewReport, names: dict[str, dict[int, str]]) -> str:
    """The report as Markdown, to copy."""
    lines = [f"# Report {title(report)}", ""]
    if report.focus_text:
        lines += [f"Focus: {report.focus_text}", ""]
    out = report.output_json
    if out:
        lines += [str(out.get("summary", "")), ""]
        for heading, key in (
            ("Highlights", "highlights"),
            ("Slips", "slips"),
            ("Patterns", "patterns"),
        ):
            items = out.get(key, [])
            if not items:
                continue
            lines.append(f"## {heading}")
            for item in items:
                refs = [names["goals"][g] for g in item.get("goal_ids", []) if g in names["goals"]]
                refs += [
                    names["people"][p] for p in item.get("person_ids", []) if p in names["people"]
                ]
                lines.append(f"- {item['text']}" + (f" ({', '.join(refs)})" if refs else ""))
            lines.append("")
        if out.get("questions"):
            lines.append("## Questions")
            lines += [f"- {q}" for q in out["questions"]]
            lines.append("")
    elif report.input_json:
        snapshot = ContextSnapshot.model_validate(report.input_json["snapshot"])
        for section in plain_sections(snapshot):
            lines.append(f"## {section.title}")
            lines += [f"- {line}" for line in section.lines]
            lines.append("")
    if report.numbers_json:
        lines.append("## Numbers")
        for row in report.numbers_json:
            note = f" ({row['note']})" if row.get("note") else ""
            lines.append(f"- {row['label']}: {row['value']}{note}")
    return "\n".join(lines).rstrip() + "\n"


def push_message(report: ReviewReport, base_url: str) -> PushMessage:
    summary = str((report.output_json or {}).get("summary", "")).strip()
    url = f"{base_url}/reports/{report.id}"
    return PushMessage(
        title=f"📊 Report ready: {title(report)}",
        body=summary or "Your report is ready.",
        click=url,
        tags=("bar_chart",),
        actions=(view_action("Open", url),),
    )


def send_push(
    session: Session,
    report: ReviewReport,
    sender: PushSender | None,
    *,
    base_url: str,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> bool:
    """One push per report (the dedupe key). True when it was recorded now."""
    if report.status is not ReviewStatus.READY:
        return False
    record = notifications.notify(
        session,
        sender,
        push_message(report, base_url),
        dedupe_key=f"report:{report.id}",
        kind=PUSH_KIND,
        quiet=quiet,
        now=now,
    )
    return record is not None
