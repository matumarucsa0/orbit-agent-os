"""Calendar meetings: sync from iCloud, linking to people, the pre-meeting brief and the pushes
before (brief) and after (capture prompt) a meeting. Callers commit.

Sync upserts one Meeting per event (per occurrence of a recurring one) by its key; an event that
is cancelled or vanishes from the synced window is marked cancelled, never deleted.

People are linked by attendee email, then by name or alias in the title or location (inflected
and diacritics-insensitive: "kafe s Paťom"). Claude is asked only when a name fits several people;
without a clear answer the meeting stays unlinked: nothing is guessed. Automatic links are worked
out again only when the title, location or attendees change, so manual links and unlinks stick.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.base import utcnow
from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    HandleType,
    MeetingLinkSource,
    PromiseDirection,
    PromiseStatus,
)
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.money import format_amount
from orbit.domain.people import Handle, Interaction, Note, Person
from orbit.integrations.caldav_client import CalendarEvent, CalendarSource
from orbit.integrations.claude import (
    BriefPerson,
    BriefRequest,
    BriefSummarizer,
    CandidatePerson,
    ClaudeError,
    MeetingLinker,
    MeetingLinkRequest,
)
from orbit.integrations.ntfy import PushMessage, PushSender, view_action
from orbit.services import dates, interactions, ledger, matching, notifications, people, promises
from orbit.services.errors import InvalidInputError, NotFoundError

log = logging.getLogger(__name__)

SYNC_DAYS = 14
SYNC_PAST = timedelta(days=1)  # meetings in progress or just over are still kept current
BRIEF_DATE_DAYS = 14  # birthdays / name days this close to the meeting are in the brief
BRIEF_NOTES = 3
# A capture prompt more than this late (the server was down) isn't sent any more.
CAPTURE_GRACE = timedelta(hours=3)
# A payment this long before/after a meeting counts as made at it (the bill comes at the end).
OVERLAP_SLACK = timedelta(hours=1)
LINK_CANDIDATES = 10
MIN_WORD = 2
BRIEF_KIND = "meeting_brief"
CAPTURE_KIND = "meeting_capture"
SHORTCUT_URL = "shortcuts://run-shortcut?name=Orbit%20Capture&input=text&text={meeting_id}"


# --- Queries ---


def get_meeting(session: Session, meeting_id: int) -> Meeting:
    meeting = session.get(Meeting, meeting_id)
    if meeting is None:
        raise NotFoundError(f"Meeting {meeting_id} not found.")
    return meeting


def list_meetings(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    include_cancelled: bool = False,
    person_id: int | None = None,
) -> list[Meeting]:
    """Meetings overlapping [start, end), earliest first."""
    query = (
        select(Meeting)
        .where(Meeting.start_utc < end, Meeting.end_utc > start)
        .options(selectinload(Meeting.links).selectinload(MeetingPerson.person))
        .order_by(Meeting.start_utc, Meeting.id)
    )
    if not include_cancelled:
        query = query.where(Meeting.cancelled.is_(False))
    if person_id is not None:
        query = query.where(Meeting.links.any(MeetingPerson.person_id == person_id))
    return list(session.scalars(query))


def overlapping(session: Session, moment: datetime) -> list[Meeting]:
    """Linked, not cancelled meetings going on around `moment` (with some slack either side)."""
    return [
        m for m in list_meetings(session, moment - OVERLAP_SLACK, moment + OVERLAP_SLACK) if m.links
    ]


def names(meeting: Meeting) -> str:
    """ "Patrik", "Patrik and Terka", "Patrik, Terka and Jano"."""
    found = [p.display_name for p in meeting.people]
    if len(found) <= 1:
        return "".join(found)
    return ", ".join(found[:-1]) + " and " + found[-1]


def overlap_text(session: Session, moment: datetime | None) -> str | None:
    """ "kafe s Paťom (Patrik Veselý)" for the meetings around a payment, for Claude."""
    if moment is None:
        return None
    found = overlapping(session, moment)
    return "; ".join(f"{m.title} ({names(m)})" for m in found) or None


def context_text(meeting: Meeting, tz: ZoneInfo) -> str:
    """The meeting a dictation follows, for Claude's capture prompt."""
    start = meeting.start_utc.astimezone(tz)
    who = ", ".join(f"{p.display_name} (id {p.id})" for p in meeting.people) or "nobody linked"
    return (
        f"Dictated right after the meeting “{meeting.title}” on {start:%Y-%m-%d %H:%M} with "
        f"{who}. Unless the text says otherwise, it's about them."
    )


# --- Appointments made by capture (SPEC §2.23) ---

ORBIT_CALENDAR = "orbit"  # `Meeting.calendar_name` of appointments Orbit made itself
DEFAULT_APPOINTMENT = timedelta(hours=1)


def appointment_exists(
    session: Session, start_utc: datetime, person_id: int | None, title: str
) -> bool:
    """An appointment of Orbit at this start with this person (or, without one, this title)."""
    query = select(Meeting).where(
        Meeting.calendar_name == ORBIT_CALENDAR,
        Meeting.cancelled.is_(False),
        Meeting.start_utc == start_utc,
    )
    if person_id is not None:
        query = query.where(Meeting.links.any(MeetingPerson.person_id == person_id))
    else:
        query = query.where(Meeting.title == title)
    return session.scalars(query.limit(1)).first() is not None


def create_appointment(
    session: Session,
    *,
    title: str,
    start_utc: datetime,
    end_utc: datetime | None = None,
    location: str | None = None,
    person_id: int | None = None,
) -> Meeting:
    """A meeting that exists only in Orbit; a calendar poll never cancels it."""
    meeting = Meeting(
        calendar_uid=f"orbit-appt:{uuid.uuid4()}",
        calendar_name=ORBIT_CALENDAR,
        title=title,
        location=location,
        start_utc=start_utc,
        end_utc=end_utc or start_utc + DEFAULT_APPOINTMENT,
    )
    if person_id is not None:
        meeting.links.append(
            MeetingPerson(person_id=person_id, link_source=MeetingLinkSource.MANUAL)
        )
    session.add(meeting)
    session.flush()
    return meeting


def cancel_appointment(session: Session, meeting: Meeting) -> Meeting:
    """Cancel an appointment made in Orbit (it leaves the ICS feed)."""
    if meeting.calendar_name != ORBIT_CALENDAR:
        raise InvalidInputError("Only appointments made in Orbit can be cancelled here.")
    meeting.cancelled = True
    session.flush()
    return meeting


# --- Linking people ---


def link_key(event: CalendarEvent) -> str:
    """What automatic links depend on: redone only when this changes."""
    parts = [event.title, event.location or "", *sorted(event.emails)]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class PeopleIndex:
    """Non-archived people with their name tokens, loaded once per sync."""

    def __init__(self, session: Session) -> None:
        self.everyone = list(
            session.scalars(
                select(Person)
                .where(Person.archived.is_(False))
                .options(selectinload(Person.aliases))
            )
        )
        self.tokens = {p.id: matching.name_tokens(p) for p in self.everyone}


def title_matches(index: PeopleIndex, text: str) -> tuple[list[Person], list[Person]]:
    """(people the text clearly names, people it names ambiguously).

    Each word goes to the people it matches best; a word only one person matches that well names
    them. A word several people match is ambiguous unless another word already settled it
    ("Paťo Veselý": two Paťos, but one Veselý).
    """
    words = {w.lstrip("@") for w in matching.words(text) if len(w.lstrip("@")) >= MIN_WORD}
    tops: list[set[int]] = []
    for word in words:
        scores = {}
        for person in index.everyone:
            best = max(
                (matching.word_score(word, token) for token in index.tokens[person.id]), default=0
            )
            if best:
                scores[person.id] = best
        if scores:
            high = max(scores.values())
            tops.append({pid for pid, score in scores.items() if score == high})
    sure = {next(iter(top)) for top in tops if len(top) == 1}
    unsure: set[int] = set()
    for top in tops:
        if len(top) > 1 and not top & sure:
            unsure |= top
    by_id = {p.id: p for p in index.everyone}

    def ordered(ids: set[int]) -> list[Person]:
        return sorted((by_id[i] for i in ids), key=lambda p: (p.search_name_norm, p.id))

    return ordered(sure), ordered(unsure)


def _attendees(session: Session, emails: Sequence[str]) -> list[Person]:
    if not emails:
        return []
    handles = session.scalars(
        select(Handle).where(Handle.type == HandleType.EMAIL, Handle.value_norm.in_(emails))
    )
    return [h.person for h in handles if not h.person.archived]


def _ask_claude(linker: MeetingLinker, meeting: Meeting, unsure: list[Person]) -> set[int]:
    shown = unsure[:LINK_CANDIDATES]
    request = MeetingLinkRequest(
        title=meeting.title,
        location=meeting.location,
        candidates=tuple(
            CandidatePerson(p.id, p.display_name, tuple(a.value for a in p.aliases)) for p in shown
        ),
    )
    try:
        answer = linker.link_meeting(request)
    except ClaudeError as exc:
        log.warning("meeting %s stays unlinked: %s", meeting.id, exc)
        return set()
    allowed = {p.id for p in shown}
    return {pid for pid in answer.person_ids if pid in allowed}


def link_people(
    session: Session,
    meeting: Meeting,
    emails: Sequence[str],
    index: PeopleIndex,
    linker: MeetingLinker | None,
) -> None:
    """Redo the automatic links (manual ones stay): attendees, names in the title/location,
    and Claude for names that fit several people."""
    found: dict[int, MeetingLinkSource] = {}
    for person in _attendees(session, emails):
        found.setdefault(person.id, MeetingLinkSource.ATTENDEE)
    sure, unsure = title_matches(index, f"{meeting.title} {meeting.location or ''}")
    for person in sure:
        found.setdefault(person.id, MeetingLinkSource.TITLE)
    unsure = [p for p in unsure if p.id not in found]
    if unsure and linker is not None:
        for pid in _ask_claude(linker, meeting, unsure):
            found.setdefault(pid, MeetingLinkSource.CLAUDE)

    current = {link.person_id: link for link in meeting.links}
    for person_id, link in current.items():
        if link.link_source is MeetingLinkSource.MANUAL:
            continue
        if person_id in found:
            link.link_source = found[person_id]
        else:
            meeting.links.remove(link)
    for person_id, source in found.items():
        if person_id not in current:
            meeting.links.append(MeetingPerson(person_id=person_id, link_source=source))


def link_person(session: Session, meeting: Meeting, person_id: int) -> Meeting:
    """Link someone by hand (kept on every sync)."""
    person = people.get_person(session, person_id)
    for link in meeting.links:
        if link.person_id == person.id:
            link.link_source = MeetingLinkSource.MANUAL
            break
    else:
        meeting.links.append(
            MeetingPerson(person_id=person.id, person=person, link_source=MeetingLinkSource.MANUAL)
        )
    session.flush()
    return meeting


def unlink_person(session: Session, meeting: Meeting, person_id: int) -> Meeting:
    """Unlink someone; a later sync doesn't link them again unless the event itself changes."""
    link = next((link for link in meeting.links if link.person_id == person_id), None)
    if link is None:
        raise InvalidInputError("That person isn't linked to this meeting.")
    meeting.links.remove(link)
    session.flush()
    return meeting


# --- Sync ---


@dataclass
class SyncReport:
    new: int = 0
    updated: int = 0
    cancelled: int = 0
    calendars: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = f"{self.new} new, {self.updated} updated, {self.cancelled} cancelled"
        if self.calendars:
            text += f" ({', '.join(self.calendars)})"
        if self.missing:
            text += f". Calendars not found: {', '.join(self.missing)}"
        return text


def _apply(meeting: Meeting, event: CalendarEvent, now: datetime) -> bool:
    """Copy the event over the meeting; True when something besides cancellation changed."""
    moved = meeting.start_utc != event.start or meeting.end_utc != event.end
    changed = moved or (
        (meeting.title, meeting.location, meeting.calendar_name)
        != (event.title, event.location, event.calendar_name)
    )
    if moved and event.start > now:
        # Rescheduled: it gets a brief (and a capture prompt) for the new time.
        meeting.brief_sent_at = meeting.capture_sent_at = None
        meeting.brief_summary = None
    meeting.title = event.title
    meeting.location = event.location
    meeting.calendar_name = event.calendar_name
    meeting.start_utc = event.start
    meeting.end_utc = event.end
    return changed


def sync_meetings(
    session: Session,
    source: CalendarSource,
    *,
    linker: MeetingLinker | None = None,
    now: datetime | None = None,
    days: int = SYNC_DAYS,
) -> SyncReport:
    """Upsert the meetings of the next `days` (and the last day) from the calendar. Idempotent.
    Raises CalDavError when the calendar can't be read (nothing changes then)."""
    now = now or utcnow()
    start, end = now - SYNC_PAST, now + timedelta(days=days)
    fetch = source.fetch_events(start, end)
    report = SyncReport(calendars=list(fetch.calendars), missing=list(fetch.missing))
    events = {event.key: event for event in fetch.events}
    existing = {
        m.calendar_uid: m
        for m in session.scalars(
            select(Meeting)
            .where(Meeting.calendar_uid.in_(list(events)))
            .options(selectinload(Meeting.links))
        )
    }
    index: PeopleIndex | None = None
    for key, event in events.items():
        meeting = existing.get(key)
        if meeting is None:
            meeting = Meeting(
                calendar_uid=key,
                calendar_name=event.calendar_name,
                title=event.title,
                location=event.location,
                start_utc=event.start,
                end_utc=event.end,
                cancelled=event.cancelled,
            )
            session.add(meeting)
            report.new += 1
        else:
            changed = _apply(meeting, event, now)
            if event.cancelled and not meeting.cancelled:
                report.cancelled += 1
            elif changed or meeting.cancelled != event.cancelled:
                report.updated += 1
            meeting.cancelled = event.cancelled
        key_now = link_key(event)
        if meeting.link_key != key_now:
            index = index or PeopleIndex(session)
            link_people(session, meeting, event.emails, index, linker)
            meeting.link_key = key_now
    session.flush()

    if fetch.calendars:  # gone from a calendar that was read in full: cancelled
        stale = session.scalars(
            select(Meeting).where(
                Meeting.calendar_name.in_(fetch.calendars),
                Meeting.calendar_name != ORBIT_CALENDAR,
                Meeting.cancelled.is_(False),
                Meeting.start_utc >= start,
                Meeting.start_utc < end,
                Meeting.calendar_uid.not_in(list(events)),
            )
        )
        for meeting in stale:
            meeting.cancelled = True
            report.cancelled += 1
    session.flush()
    return report


# --- The brief ---


@dataclass
class PersonBrief:
    person: Person
    last: Interaction | None
    notes: list[Note]
    balances: list[ledger.PersonBalance]
    debts: list[Debt]  # open: money, items and favors, both directions
    promises: list[Promise]  # open, both directions
    dates: list[dates.UpcomingDate]

    @property
    def things(self) -> list[Debt]:
        return [d for d in self.debts if d.type is not DebtType.MONEY]


@dataclass
class Brief:
    meeting: Meeting
    day: date  # the meeting's local date
    people: list[PersonBrief]


def build_brief(session: Session, meeting: Meeting, tz: ZoneInfo) -> Brief:
    day = meeting.start_utc.astimezone(tz).date()
    found = []
    for person in meeting.people:
        found.append(
            PersonBrief(
                person=person,
                last=interactions.last_interaction(session, person.id),
                notes=list(person.notes[:BRIEF_NOTES]),
                balances=ledger.balances(session, person.id),
                debts=ledger.list_debts(session, person_id=person.id),
                promises=promises.list_promises(
                    session, person_id=person.id, status=PromiseStatus.OPEN
                ),
                dates=dates.person_upcoming(person, day, BRIEF_DATE_DAYS),
            )
        )
    return Brief(meeting, day, found)


def _short(day: date) -> str:
    return f"{day.day} {day:%b}"


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def thing_line(debt: Debt) -> str:
    """ "has your charger" / "you have their book" / "owes you a favor (moving)"."""
    what = debt.description or debt.type.value
    theirs = debt.direction is DebtDirection.THEY_OWE_ME
    if debt.type is DebtType.ITEM:
        return f"has your {what}" if theirs else f"you have their {what}"
    return f"owes you a favor ({what})" if theirs else f"you owe a favor ({what})"


def promise_line(promise: Promise) -> str:
    who = "you promised" if promise.direction is PromiseDirection.I_PROMISED else "they promised"
    due = f" (due {_short(promise.due)})" if promise.due else ""
    return f"{who}: {promise.text}{due}"


def date_line(upcoming: dates.UpcomingDate, day: date) -> str:
    offset = (upcoming.date - day).days
    when = "on the day" if offset == 0 else "tomorrow" if offset == 1 else f"in {offset} days"
    if upcoming.kind == "birthday":
        detail = f", {upcoming.detail}" if upcoming.detail else ""
        return f"🎂 birthday {when} ({_short(upcoming.date)}{detail})"
    return f"🎉 name day {when} ({_short(upcoming.date)})"


def person_lines(item: PersonBrief, day: date) -> list[str]:
    """The brief for one person, a line per fact, for the push."""
    lines = []
    if item.last is not None:
        summary = f": {_clip(item.last.summary, 60)}" if item.last.summary else ""
        lines.append(f"🗓 last {item.last.kind.value} {_short(item.last.occurred_on)}{summary}")
    for balance in item.balances:
        amount = format_amount(abs(balance.amount_minor), balance.currency)
        lines.append(
            f"💶 owes you {amount}" if balance.amount_minor > 0 else f"💶 you owe {amount}"
        )
    lines += [f"📦 {thing_line(d)}" for d in item.things]
    lines += [f"✅ {promise_line(p)}" for p in item.promises]
    lines += [date_line(u, day) for u in item.dates]
    if item.notes:
        lines.append(f"📝 {_clip(item.notes[0].text)}")
    return lines


def brief_push(brief: Brief, base_url: str, tz: ZoneInfo) -> PushMessage:
    meeting = brief.meeting
    start = meeting.start_utc.astimezone(tz)
    parts = []
    if meeting.brief_summary:
        parts.append(f"💬 {meeting.brief_summary}")
    for item in brief.people:
        lines = person_lines(item, brief.day) or ["nothing open, nothing logged yet"]
        head = f"{item.person.display_name}:\n" if len(brief.people) > 1 else ""
        parts.append(head + "\n".join(lines))
    url = f"{base_url}/meetings/{meeting.id}"
    return PushMessage(
        title=f"📅 {start:%H:%M} {meeting.title}",
        body="\n\n".join(parts),
        click=url,
        tags=("calendar",),
        actions=(view_action("Brief", url),),
    )


def _last_text(last: Interaction | None) -> str | None:
    if last is None:
        return None
    return f"{last.occurred_on.isoformat()}: {last.summary or last.kind.value}"


def summarize(brief: Brief, summarizer: BriefSummarizer, today: date) -> str | None:
    """Claude's "what's new with them" line from the last notes; None without notes or on error."""
    if not any(item.notes for item in brief.people):
        return None
    request = BriefRequest(
        meeting=brief.meeting.title,
        today=today,
        people=tuple(
            BriefPerson(
                name=item.person.display_name,
                notes=tuple(n.text for n in item.notes),
                last_interaction=_last_text(item.last),
            )
            for item in brief.people
        ),
    )
    try:
        line = summarizer.summarize_brief(request).summary.strip()
    except ClaudeError as exc:
        log.warning("no brief summary for meeting %s: %s", brief.meeting.id, exc)
        return None
    return line or None


# --- Pushes ---


def capture_push(meeting: Meeting, base_url: str, tz: ZoneInfo) -> PushMessage:
    start = meeting.start_utc.astimezone(tz)
    dictate = SHORTCUT_URL.format(meeting_id=meeting.id)
    return PushMessage(
        title=f"📝 Anything to note about {names(meeting)}?",
        body=f"{meeting.title} · {start:%H:%M}-{meeting.end_utc.astimezone(tz):%H:%M}",
        click=dictate,
        tags=("memo",),
        actions=(
            view_action("Dictate", dictate),
            view_action("Open", f"{base_url}/meetings/{meeting.id}"),
        ),
    )


def _linked_meetings() -> Select[tuple[Meeting]]:
    return (
        select(Meeting)
        .where(Meeting.cancelled.is_(False), Meeting.links.any())
        .options(selectinload(Meeting.links).selectinload(MeetingPerson.person))
        .order_by(Meeting.start_utc, Meeting.id)
    )


def due_briefs(session: Session, now: datetime, minutes_before: int) -> list[Meeting]:
    """Linked meetings starting within the next `minutes_before` whose brief wasn't sent."""
    lead = timedelta(minutes=minutes_before)
    return list(
        session.scalars(
            _linked_meetings().where(
                Meeting.brief_sent_at.is_(None),
                Meeting.start_utc > now,
                Meeting.start_utc <= now + lead,
            )
        )
    )


def due_captures(session: Session, now: datetime, minutes_after: int) -> list[Meeting]:
    """Linked meetings that ended `minutes_after` ago (not more than CAPTURE_GRACE later)."""
    ready = now - timedelta(minutes=minutes_after)
    return list(
        session.scalars(
            _linked_meetings().where(
                Meeting.capture_sent_at.is_(None),
                Meeting.end_utc <= ready,
                Meeting.end_utc > ready - CAPTURE_GRACE,
            )
        )
    )


def send_meeting_pushes(
    session: Session,
    sender: PushSender | None,
    *,
    base_url: str,
    tz: ZoneInfo,
    quiet: notifications.QuietHours,
    minutes_before: int,
    minutes_after: int,
    summarizer: BriefSummarizer | None = None,
    now: datetime | None = None,
) -> int:
    """Briefs before linked meetings and capture prompts after them; each goes out once
    (also during quiet hours). Returns how many were recorded."""
    now = now or utcnow()
    today = now.astimezone(tz).date()
    count = 0
    for meeting in due_briefs(session, now, minutes_before):
        brief = build_brief(session, meeting, tz)
        if summarizer is not None:
            meeting.brief_summary = summarize(brief, summarizer, today)
        record = notifications.notify(
            session,
            sender,
            brief_push(brief, base_url, tz),
            dedupe_key=f"{BRIEF_KIND}:{meeting.id}:{meeting.start_utc:%Y%m%dT%H%M}",
            kind=BRIEF_KIND,
            quiet=quiet,
            now=now,
        )
        meeting.brief_sent_at = now
        count += record is not None
    for meeting in due_captures(session, now, minutes_after):
        record = notifications.notify(
            session,
            sender,
            capture_push(meeting, base_url, tz),
            dedupe_key=f"{CAPTURE_KIND}:{meeting.id}:{meeting.end_utc:%Y%m%dT%H%M}",
            kind=CAPTURE_KIND,
            quiet=quiet,
            now=now,
        )
        meeting.capture_sent_at = now
        count += record is not None
    session.flush()
    return count
