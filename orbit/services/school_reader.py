"""The school reader (Phase 18c, SPEC §2.20): Claude reads the text of a new or changed resource
and proposes deadlines (`school_deadline`) and course rules (`course_fact`); links it finds become
suggested resources. L2: nothing is written without my confirm. Callers commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import (
    FactTopic,
    ProposalKind,
    ProposalStatus,
    ResourceKind,
    ResourceOrigin,
    ResourceStatus,
    SchoolEventKind,
    SchoolEventSource,
)
from orbit.domain.inbox import Proposal
from orbit.domain.normalize import normalize_text
from orbit.domain.school import Course, CourseFact, Lesson, SchoolEvent, SchoolResource
from orbit.domain.usage import ClaudeCall
from orbit.integrations.claude import (
    ClaudeError,
    SchoolDeadlineIdea,
    SchoolFactIdea,
    SchoolReader,
    SchoolReadRequest,
)
from orbit.integrations.ntfy import PushMessage, PushSender, view_action
from orbit.integrations.prompts_registry import prompt_version
from orbit.integrations.resources import ResourceFetcher
from orbit.services import notifications, school
from orbit.services.errors import InvalidInputError
from orbit.services.proposal_kinds import Applied, ApplyContext, ProposalHandler
from orbit.services.school_resources import FETCHED, add_url, check, get_resource

log = logging.getLogger(__name__)

SOURCE_TYPE = "school_resource"
CHUNK_CHARS = 60_000
TITLE_CHARS = 300
FACT_CHARS = 1000
QUOTE_CHARS = 600
MAX_IDEAS = 25  # per read
PUSH_KIND = "school_resources"
READ_PURPOSE = "school_read"
DISPLAY_TZ = ZoneInfo("Europe/Prague")  # proposal summaries are text; times shown in Prague


# --- Payloads and what confirming does ---


class SchoolDeadlinePayload(BaseModel):
    course_id: int | None = None
    title: str = Field(min_length=1, max_length=TITLE_CHARS)
    kind: Literal["exam", "deadline", "test", "other"] = "deadline"
    start_utc: datetime
    end_utc: datetime | None = None
    all_day: bool = False
    note: str | None = Field(default=None, max_length=2000)
    source_kind: Literal["site", "announcement", "mail", "dictation"] = "site"
    source_ref: str = Field(default="", max_length=500)
    quote: str = Field(default="", max_length=QUOTE_CHARS)


class CourseFactPayload(BaseModel):
    course_id: int
    topic: FactTopic = FactTopic.OTHER
    text: str = Field(min_length=1, max_length=FACT_CHARS)
    quote: str = Field(default="", max_length=QUOTE_CHARS)
    resource_id: int | None = None


EVENT_KINDS = {
    "exam": SchoolEventKind.EXAM,
    "test": SchoolEventKind.EXAM,  # a test is reminded like an exam
    "deadline": SchoolEventKind.DEADLINE,
    "other": SchoolEventKind.OTHER,
}


def _confirm_deadline(
    session: Session, payload: SchoolDeadlinePayload, ctx: ApplyContext
) -> Applied:
    if payload.course_id is not None and session.get(Course, payload.course_id) is None:
        raise InvalidInputError("That course does not exist any more.")
    session.add(
        SchoolEvent(
            source=SchoolEventSource.PROPOSAL,
            uid=None,
            kind=EVENT_KINDS[payload.kind],
            title=payload.title,
            description=(payload.note or payload.quote or None),
            course_id=payload.course_id,
            start_utc=payload.start_utc,
            end_utc=payload.end_utc,
            all_day=payload.all_day,
        )
    )
    session.flush()
    return Applied(f"Added to your school deadlines: {payload.title}.")


def _confirm_fact(session: Session, payload: CourseFactPayload, ctx: ApplyContext) -> Applied:
    course = session.get(Course, payload.course_id)
    if course is None:
        raise InvalidInputError("That course does not exist any more.")
    session.add(
        CourseFact(
            course_id=course.id,
            topic=payload.topic,
            text=payload.text,
            quote=payload.quote or None,
            resource_id=payload.resource_id,
        )
    )
    session.flush()
    return Applied(f"Saved a rule for {course.code}: {payload.text}")


def _deadline_summary(session: Session, payload: SchoolDeadlinePayload) -> str:
    course = session.get(Course, payload.course_id) if payload.course_id else None
    where = f" ({course.code})" if course else ""
    when = school.when_text(payload.start_utc, DISPLAY_TZ, all_day=payload.all_day)
    return f"{when} {payload.title}{where}"


def _fact_summary(session: Session, payload: CourseFactPayload) -> str:
    course = session.get(Course, payload.course_id)
    return f"{course.code if course else 'Course'}: {payload.text}"


DEADLINE_HANDLER = ProposalHandler(
    ProposalKind.SCHOOL_DEADLINE, SchoolDeadlinePayload, _confirm_deadline
)
FACT_HANDLER = ProposalHandler(ProposalKind.COURSE_FACT, CourseFactPayload, _confirm_fact)


# --- Dedupe ---


def _similar(a: str, b: str) -> bool:
    x, y = normalize_text(a), normalize_text(b)
    return bool(x and y) and (x == y or x in y or y in x)


def _same_day(a: datetime, b: datetime, tz: ZoneInfo) -> bool:
    return abs((a.astimezone(tz).date() - b.astimezone(tz).date()).days) <= 1


def _deadline_known(session: Session, payload: SchoolDeadlinePayload, tz: ZoneInfo) -> bool:
    """An event of the course with a similar title within a day, or a pending or rejected
    proposal for the same (course, title, day)."""
    window = timedelta(days=2)
    for event in session.scalars(
        select(SchoolEvent).where(
            SchoolEvent.course_id == payload.course_id,
            SchoolEvent.start_utc >= payload.start_utc - window,
            SchoolEvent.start_utc <= payload.start_utc + window,
        )
    ):
        if _similar(event.title, payload.title) and _same_day(
            event.start_utc, payload.start_utc, tz
        ):
            return True
    for other in session.scalars(
        select(Proposal).where(
            Proposal.kind == ProposalKind.SCHOOL_DEADLINE,
            Proposal.status.in_((ProposalStatus.PENDING, ProposalStatus.REJECTED)),
        )
    ):
        old = other.payload_json
        if old.get("course_id") != payload.course_id:
            continue
        try:
            start = datetime.fromisoformat(str(old["start_utc"]))
        except (KeyError, ValueError):
            continue
        if start.astimezone(tz).date() == payload.start_utc.astimezone(tz).date() and _similar(
            str(old.get("title", "")), payload.title
        ):
            return True
    return False


def _fact_known(session: Session, payload: CourseFactPayload) -> bool:
    for fact in session.scalars(
        select(CourseFact).where(CourseFact.course_id == payload.course_id)
    ):
        if _similar(fact.text, payload.text):
            return True
    for other in session.scalars(
        select(Proposal).where(
            Proposal.kind == ProposalKind.COURSE_FACT,
            Proposal.status.in_((ProposalStatus.PENDING, ProposalStatus.REJECTED)),
        )
    ):
        old = other.payload_json
        if old.get("course_id") == payload.course_id and _similar(
            str(old.get("text", "")), payload.text
        ):
            return True
    return False


# --- From the reader's ideas to proposals ---


def _due(idea: SchoolDeadlineIdea, tz: ZoneInfo) -> tuple[datetime, datetime | None, bool] | None:
    """The idea's local `due` as UTC (all-day: the whole local day); None when it does not parse."""
    try:
        if "T" in idea.due and not idea.all_day:
            local = datetime.fromisoformat(idea.due).replace(tzinfo=tz)
            return local.astimezone(UTC), None, False
        day = date.fromisoformat(idea.due[:10])
    except ValueError:
        return None
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day, time(23, 59), tz).astimezone(UTC)
    return start, end, True


def _proposal(
    session: Session, kind: ProposalKind, payload: BaseModel, summary: str, resource: SchoolResource
) -> Proposal:
    proposal = Proposal(
        kind=kind,
        status=ProposalStatus.PENDING,
        summary=summary[:2000],
        payload_json=payload.model_dump(mode="json"),
        confidence=None,
        source_type=SOURCE_TYPE,
        source_id=str(resource.id),
        prompt_version=prompt_version("school_read"),
    )
    session.add(proposal)
    session.flush()
    return proposal


@dataclass
class ReadReport:
    reads: int = 0
    deadlines: int = 0
    facts: int = 0
    links: int = 0
    waiting: int = 0  # resources that wait for tomorrow's budget
    failed: int = 0


def _add_deadline(
    session: Session, resource: SchoolResource, idea: SchoolDeadlineIdea, tz: ZoneInfo, today: date
) -> bool:
    parsed = _due(idea, tz)
    title = " ".join(idea.title.split())[:TITLE_CHARS]
    if parsed is None or not title or parsed[0].astimezone(tz).date() < today:
        return False
    start, end, all_day = parsed
    points = f"{idea.points:g} points" if idea.points is not None else None
    payload = SchoolDeadlinePayload(
        course_id=resource.course_id,
        title=title,
        kind=idea.kind,
        start_utc=start,
        end_utc=end,
        all_day=all_day,
        note=points,
        source_kind="site",
        source_ref=resource.url or resource.label,
        quote=idea.quote[:QUOTE_CHARS],
    )
    if _deadline_known(session, payload, tz):
        return False
    summary = _deadline_summary(session, payload)
    quote = f" “{payload.quote}”" if payload.quote else ""
    _proposal(session, ProposalKind.SCHOOL_DEADLINE, payload, f"{summary}.{quote}", resource)
    return True


def propose_dictated_deadline(
    session: Session,
    idea: SchoolDeadlineIdea,
    course: Course | None,
    *,
    text: str,
    tz: ZoneInfo,
    today: date,
) -> Proposal | None:
    """A `school_deadline` proposal from a dictation (SPEC §2.29), or None when the date does not
    parse, is past, or the deadline is already known. Callers add and commit it."""
    parsed = _due(idea, tz)
    title = " ".join(idea.title.split())[:TITLE_CHARS]
    if parsed is None or not title or parsed[0].astimezone(tz).date() < today:
        return None
    start, end, all_day = parsed
    payload = SchoolDeadlinePayload(
        course_id=course.id if course is not None else None,
        title=title,
        kind=idea.kind,
        start_utc=start,
        end_utc=end,
        all_day=all_day,
        source_kind="dictation",
        source_ref=text[:500],
        quote=text[:QUOTE_CHARS],
    )
    if _deadline_known(session, payload, tz):
        return None
    return Proposal(
        kind=ProposalKind.SCHOOL_DEADLINE,
        status=ProposalStatus.PENDING,
        summary=_deadline_summary(session, payload)[:2000],
        payload_json=payload.model_dump(mode="json"),
        confidence=None,
        source_type="capture",
    )


def _add_fact(session: Session, resource: SchoolResource, idea: SchoolFactIdea) -> bool:
    text = " ".join(idea.text.split())[:FACT_CHARS]
    if resource.course_id is None or not text:
        return False  # a rule needs a course to belong to
    payload = CourseFactPayload(
        course_id=resource.course_id,
        topic=FactTopic(idea.topic),
        text=text,
        quote=idea.quote[:QUOTE_CHARS],
        resource_id=resource.id,
    )
    if _fact_known(session, payload):
        return False
    quote = f" “{payload.quote}”" if payload.quote else ""
    _proposal(
        session,
        ProposalKind.COURSE_FACT,
        payload,
        f"{_fact_summary(session, payload)}{quote}",
        resource,
    )
    return True


# --- Reading ---


def _course_request_parts(
    session: Session, course: Course | None, now: datetime, tz: ZoneInfo
) -> dict[str, object]:
    if course is None:
        return {}
    lessons = list(
        session.scalars(
            select(Lesson)
            .where(
                Lesson.course_id == course.id, Lesson.start_utc >= now, Lesson.cancelled.is_(False)
            )
            .order_by(Lesson.start_utc)
            .limit(3)
        )
    )
    deadlines = school.list_events(
        session,
        now - timedelta(days=1),
        now + timedelta(days=200),
        kinds=(SchoolEventKind.EXAM, SchoolEventKind.DEADLINE),
        course_id=course.id,
    )
    facts = session.scalars(select(CourseFact.text).where(CourseFact.course_id == course.id))
    return {
        "course": f"{course.code} {course.name}",
        "teachers": next((ls.teachers_text for ls in lessons if ls.teachers_text), None),
        "lesson_times": tuple(
            f"{school.when_text(ls.start_utc, tz)}-{school.clock(ls.end_utc, tz)}"
            + (f" {ls.room}" if ls.room else "")
            for ls in lessons
        ),
        "known_deadlines": tuple(
            f"{e.start_utc.astimezone(tz):%Y-%m-%d} {e.title}" for e in deadlines
        ),
        "known_facts": tuple(facts),
    }


def _chunks(text: str) -> list[str]:
    """The text in parts of at most CHUNK_CHARS, cut at line ends."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > CHUNK_CHARS and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line[:CHUNK_CHARS])
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def reads_today(session: Session, now: datetime, tz: ZoneInfo) -> int:
    """Claude reads made today (local day), whichever path made them."""
    start = datetime.combine(now.astimezone(tz).date(), time.min, tz).astimezone(UTC)
    return (
        session.scalar(
            select(func.count(ClaudeCall.id)).where(
                ClaudeCall.purpose == READ_PURPOSE, ClaudeCall.created_at >= start
            )
        )
        or 0
    )


def pending_reads(session: Session) -> list[SchoolResource]:
    """Kept resources with text that is new or changed since it was last read."""
    found = []
    for resource in session.scalars(
        select(SchoolResource)
        .where(
            SchoolResource.kept.is_(True),
            SchoolResource.text.is_not(None),
            SchoolResource.changed_at.is_not(None),
            SchoolResource.status.not_in((ResourceStatus.ERROR, ResourceStatus.BROKEN)),
        )
        .order_by(SchoolResource.changed_at, SchoolResource.id)
    ):
        if resource.read_at is None or (
            resource.changed_at is not None and resource.read_at < resource.changed_at
        ):
            found.append(resource)
    return found


def read_pending(
    session: Session,
    reader: SchoolReader,
    now: datetime,
    tz: ZoneInfo,
    per_day: int,
    *,
    only: int | None = None,
) -> ReadReport:
    """Read what is new or changed, within today's budget. A resource is read whole or not at
    all today; one that does not fit waits. A Claude error leaves it for the next run."""
    report = ReadReport()
    used = reads_today(session, now, tz)
    today = now.astimezone(tz).date()
    for resource in pending_reads(session):
        if only is not None and resource.id != only:
            continue
        assert resource.text is not None
        diff = resource.last_diff if resource.prev_text is not None and resource.last_diff else None
        text = diff or resource.text
        parts = _chunks(text)
        if used + report.reads + len(parts) > per_day:
            report.waiting += 1
            continue
        course = session.get(Course, resource.course_id) if resource.course_id else None
        context = _course_request_parts(session, course, now, tz)
        ok = True
        for number, part in enumerate(parts, start=1):
            request = SchoolReadRequest(
                today=today,
                text=part,
                is_diff=diff is not None,
                resource_label=resource.label,
                resource_url=resource.url,
                chunk=f"{number} of {len(parts)}" if len(parts) > 1 else None,
                **context,  # type: ignore[arg-type]
            )
            try:
                answer = reader.read_school(request)
            except ClaudeError as exc:
                log.warning("reading resource %s failed: %s", resource.id, exc)
                resource.error = str(exc)[:500]
                report.failed += 1
                ok = False
                break
            report.reads += 1
            for idea in answer.deadlines[:MAX_IDEAS]:
                report.deadlines += _add_deadline(session, resource, idea, tz, today)
            for fact in answer.facts[:MAX_IDEAS]:
                report.facts += _add_fact(session, resource, fact)
            for link in answer.links[:MAX_IDEAS]:
                try:
                    add_url(
                        session,
                        link.url,
                        resource.course_id,
                        origin=ResourceOrigin.CLAUDE,
                        reason=link.why,
                    )
                    report.links += 1
                except InvalidInputError:
                    continue  # already known, or not a fetchable address
        if ok:
            resource.read_at = now
            resource.error = None
        session.flush()
    return report


# --- The daily push ---


def changed_today(session: Session, now: datetime, tz: ZoneInfo) -> list[SchoolResource]:
    """Kept resources whose page changed since midnight (a first read is not a change)."""
    start = datetime.combine(now.astimezone(tz).date(), time.min, tz).astimezone(UTC)
    return list(
        session.scalars(
            select(SchoolResource)
            .where(
                SchoolResource.kept.is_(True),
                SchoolResource.status == ResourceStatus.CHANGED,
                SchoolResource.changed_at >= start,
                SchoolResource.kind.in_((ResourceKind.PAGE, ResourceKind.PDF, ResourceKind.GITHUB)),
            )
            .order_by(SchoolResource.id)
        )
    )


def send_changed_push(
    session: Session,
    sender: PushSender | None,
    *,
    base_url: str,
    tz: ZoneInfo,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> bool:
    """One push a day for kept pages that changed: "Course pages changed: NMAT101, NMAT202"."""
    moment = now or utcnow()
    changed = changed_today(session, moment, tz)
    if not changed:
        return False
    names = []
    for resource in changed:
        course = session.get(Course, resource.course_id) if resource.course_id else None
        names.append(course.code if course else resource.label)
    unique = list(dict.fromkeys(names))
    url = f"{base_url}/school/map"
    record = notifications.notify(
        session,
        sender,
        PushMessage(
            title="📚 Course pages changed",
            body=", ".join(unique)[:300],
            click=url,
            tags=("books",),
            actions=(view_action("Open", url),),
        ),
        dedupe_key=f"school_resources:{moment.astimezone(tz).date().isoformat()}",
        kind=PUSH_KIND,
        quiet=quiet,
        now=moment,
    )
    return record is not None


# --- Plain text helpers for pages ---


def reader_of(claude: object) -> SchoolReader | None:
    """The Claude client when it can read school text (API client or worker queue)."""
    return claude if hasattr(claude, "read_school") else None


def check_now(
    session: Session,
    resource_id: int,
    fetcher: ResourceFetcher,
    claude: object,
    now: datetime,
    tz: ZoneInfo,
    per_day: int,
) -> str:
    """ "Check now" on one resource: fetch it and, when its text is new or changed, let Claude read
    it (within today's budget). Returns a one-line result for the page."""
    resource = get_resource(session, resource_id)
    if resource.kind in (ResourceKind.LOGIN,):
        return "This page needs a login, so Orbit does not fetch it."
    if resource.kind in FETCHED:
        status = check(session, resource, fetcher, now)
        text = {
            ResourceStatus.OK: "fetched",
            ResourceStatus.CHANGED: "changed since the last check",
            ResourceStatus.UNCHANGED: "no change",
            ResourceStatus.NO_TEXT: "has no text layer (no OCR)",
            ResourceStatus.ERROR: f"failed: {resource.error}",
            ResourceStatus.BROKEN: f"broken: {resource.error}",
        }.get(status, status.value)
    else:
        text = "stored text"
    reader = reader_of(claude)
    if reader is not None and resource.kept:
        report = read_pending(session, reader, now, tz, per_day, only=resource.id)
        if report.reads:
            text += f"; read by Claude: {report.deadlines} deadlines, {report.facts} rules"
        elif report.waiting:
            text += "; Claude's reads for today are used up, it will be read tomorrow"
    return text
