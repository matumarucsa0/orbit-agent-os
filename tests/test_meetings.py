from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    HandleType,
    InteractionKind,
    MeetingLinkSource,
    PromiseDirection,
)
from orbit.domain.inbox import Notification
from orbit.domain.meetings import Meeting
from orbit.domain.people import Person
from orbit.integrations.caldav_client import CalDavError, CalendarEvent, CalendarFetch
from orbit.integrations.claude import (
    BriefLine,
    BriefRequest,
    ClaudeError,
    MeetingLinkRequest,
    MeetingPeople,
)
from orbit.services import duplicates, interactions, ledger, meetings, people, promises
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.notifications import QuietHours
from tests.test_notifications import FakeSender

PRAGUE = ZoneInfo("Europe/Prague")
NOW = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)  # 10:00 in Prague
QUIET = QuietHours(time(22, 0), time(8, 0), PRAGUE)
URL = "https://orbit.example.com"


def event(
    key: str = "e1", title: str = "kafe s Paťom", hours: float = 28, **fields: Any
) -> CalendarEvent:
    start = NOW + timedelta(hours=hours)
    values: dict[str, Any] = {
        "key": key,
        "calendar_name": "Personal",
        "title": title,
        "location": None,
        "start": start,
        "end": start + timedelta(hours=1),
    }
    values.update(fields)
    return CalendarEvent(**values)


class FakeSource:
    def __init__(self, *found: CalendarEvent, calendars: tuple[str, ...] = ("Personal",)) -> None:
        self.events = list(found)
        self.calendars = list(calendars)
        self.error: Exception | None = None
        self.windows: list[tuple[datetime, datetime]] = []

    def fetch_events(self, start: datetime, end: datetime) -> CalendarFetch:
        self.windows.append((start, end))
        if self.error is not None:
            raise self.error
        return CalendarFetch(events=list(self.events), calendars=list(self.calendars))


class FakeLinker:
    def __init__(self, answer: list[int] | Exception) -> None:
        self.answer = answer
        self.requests: list[MeetingLinkRequest] = []

    def link_meeting(self, request: MeetingLinkRequest) -> MeetingPeople:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return MeetingPeople(person_ids=self.answer)


class FakeSummarizer:
    def __init__(self, answer: str | Exception = "Paťo má novú prácu.") -> None:
        self.answer = answer
        self.requests: list[BriefRequest] = []

    def summarize_brief(self, request: BriefRequest) -> BriefLine:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return BriefLine(summary=self.answer)


def person(db: Session, name: str, *aliases: str, **fields: Any) -> Person:
    created = people.create_person(db, people.PersonCreate(display_name=name, **fields))
    for alias in aliases:
        people.add_alias(db, created, alias)
    return created


def sync(
    db: Session, source: FakeSource, linker: FakeLinker | None = None, now: datetime = NOW
) -> meetings.SyncReport:
    report = meetings.sync_meetings(db, source, linker=linker, now=now)
    db.commit()
    return report


def only(db: Session) -> Meeting:
    return db.scalars(select(Meeting)).one()


def linked(meeting: Meeting) -> dict[str, MeetingLinkSource]:
    return {link.person.display_name: link.link_source for link in meeting.links}


@pytest.fixture
def pato(db: Session) -> Person:
    return person(db, "Patrik Veselý", "Paťo", first_name="Patrik", last_name="Veselý")


# --- Sync ---


def test_sync_creates_meetings_and_is_idempotent(db: Session, pato: Person) -> None:
    source = FakeSource(event(location="Kaviareň"), event("e2", "Gym", hours=50))
    report = sync(db, source)
    assert (report.new, report.updated, report.cancelled) == (2, 0, 0)
    assert report.summary == "2 new, 0 updated, 0 cancelled (Personal)"
    assert source.windows == [(NOW - timedelta(days=1), NOW + timedelta(days=14))]
    again = sync(db, source)
    assert (again.new, again.updated, again.cancelled) == (0, 0, 0)
    found = {m.calendar_uid: m for m in db.scalars(select(Meeting))}
    assert set(found) == {"e1", "e2"}
    coffee = found["e1"]
    assert (coffee.title, coffee.location, coffee.calendar_name) == (
        "kafe s Paťom",
        "Kaviareň",
        "Personal",
    )
    assert coffee.start_utc == NOW + timedelta(hours=28)
    assert linked(coffee) == {"Patrik Veselý": MeetingLinkSource.TITLE}
    assert linked(found["e2"]) == {}


def test_a_moved_meeting_is_updated_and_gets_a_new_brief(db: Session, pato: Person) -> None:
    source = FakeSource(event())
    sync(db, source)
    meeting = only(db)
    meeting.brief_sent_at = meeting.capture_sent_at = NOW
    meeting.brief_summary = "old"
    db.commit()
    source.events = [event(hours=54)]
    report = sync(db, source)
    assert (report.new, report.updated) == (0, 1)
    meeting = only(db)
    assert meeting.start_utc == NOW + timedelta(hours=54)
    assert (meeting.brief_sent_at, meeting.capture_sent_at, meeting.brief_summary) == (
        None,
        None,
        None,
    )


def test_cancelled_and_vanished_meetings_are_kept_as_cancelled(db: Session) -> None:
    source = FakeSource(event("a"), event("b", "Pivo"), event("c", "Obed", hours=-5))
    sync(db, source)
    source.events = [event("a", cancelled=True), event("c", "Obed", hours=-5)]
    report = sync(db, source)
    assert report.cancelled == 2
    found = {m.calendar_uid: m.cancelled for m in db.scalars(select(Meeting))}
    assert found == {"a": True, "b": True, "c": False}
    source.events = [event("a"), event("b", "Pivo"), event("c", "Obed", hours=-5)]
    back = sync(db, source)
    assert (back.updated, back.cancelled) == (2, 0)
    assert not any(m.cancelled for m in db.scalars(select(Meeting)))


def test_only_meetings_of_calendars_that_were_read_can_vanish(db: Session) -> None:
    sync(db, FakeSource(event("w", calendar_name="Work"), calendars=("Work",)))
    sync(db, FakeSource(event("p"), calendars=("Personal",)))  # Work wasn't read this time
    assert not only_uid(db, "w").cancelled


def test_meetings_outside_the_window_stay_as_they_were(db: Session) -> None:
    source = FakeSource(event("old", hours=-30))
    sync(db, source, now=NOW - timedelta(days=2))
    source.events = []
    sync(db, source)  # started before the window: not looked at
    assert not only_uid(db, "old").cancelled


def only_uid(db: Session, uid: str) -> Meeting:
    return db.scalars(select(Meeting).where(Meeting.calendar_uid == uid)).one()


def test_sync_error_changes_nothing(db: Session) -> None:
    source = FakeSource(event())
    source.error = CalDavError("down")
    with pytest.raises(CalDavError):
        sync(db, source)
    assert db.scalars(select(Meeting)).all() == []


def test_missing_calendars_are_reported(db: Session) -> None:
    source = FakeSource()
    source.fetch_events = lambda start, end: CalendarFetch([], ["Personal"], ["Uni"])  # type: ignore[method-assign]
    assert (
        sync(db, source).summary
        == "0 new, 0 updated, 0 cancelled (Personal). Calendars not found: Uni"
    )


# --- Linking ---


def test_attendee_email_links_the_person(db: Session) -> None:
    terka = person(db, "Tereza Nováková")
    people.add_handle(db, terka, HandleType.EMAIL, "tereza.novakova@example.com")
    sync(
        db,
        FakeSource(event(title="Obed", emails=("tereza.novakova@example.com", "me@example.com"))),
    )
    assert linked(only(db)) == {"Tereza Nováková": MeetingLinkSource.ATTENDEE}


@pytest.mark.parametrize(
    "title",
    ["kafe s Paťom", "KAFE S PATOM", "Pivo s Patrikom", "obed s Paťom a Terkou", "Patrik Vesely"],
)
def test_names_and_aliases_in_the_title_link_inflected(
    db: Session, pato: Person, title: str
) -> None:
    person(db, "Tereza Nováková", "Terka", first_name="Tereza")
    sync(db, FakeSource(event(title=title)))
    found = linked(only(db))
    assert found["Patrik Veselý"] is MeetingLinkSource.TITLE
    assert ("Tereza Nováková" in found) == ("Terkou" in title)


def test_location_counts_too(db: Session, pato: Person) -> None:
    sync(db, FakeSource(event(title="Kafe", location="u Paťa doma")))
    assert linked(only(db)) == {"Patrik Veselý": MeetingLinkSource.TITLE}


@pytest.mark.parametrize("title", ["Gym", "Zubár", "Porada tímu", "Obed"])
def test_titles_without_names_stay_unlinked(db: Session, pato: Person, title: str) -> None:
    sync(db, FakeSource(event(title=title)))
    assert linked(only(db)) == {}


def test_archived_people_are_not_linked(db: Session, pato: Person) -> None:
    pato.archived = True
    sync(db, FakeSource(event()))
    assert linked(only(db)) == {}


def test_an_ambiguous_name_asks_claude(db: Session, pato: Person) -> None:
    other = person(db, "Patrik Horváth", "Paťo")
    linker = FakeLinker([other.id, 999])  # 999 wasn't offered: ignored
    sync(db, FakeSource(event(location="Bratislava")), linker)
    assert linked(only(db)) == {"Patrik Horváth": MeetingLinkSource.CLAUDE}
    (request,) = linker.requests
    assert (request.title, request.location) == ("kafe s Paťom", "Bratislava")
    assert {c.id for c in request.candidates} == {pato.id, other.id}
    assert all(c.aliases == ("Paťo",) for c in request.candidates)


def test_an_ambiguous_name_without_claude_or_a_clear_answer_stays_unlinked(
    db: Session, pato: Person
) -> None:
    person(db, "Patrik Horváth", "Paťo")
    sync(db, FakeSource(event("a")))
    sync(db, FakeSource(event("b")), FakeLinker([]))
    sync(db, FakeSource(event("c")), FakeLinker(ClaudeError("down")))
    assert all(linked(m) == {} for m in db.scalars(select(Meeting)))


def test_a_second_word_settles_an_ambiguous_name(db: Session, pato: Person) -> None:
    person(db, "Patrik Horváth", "Paťo", last_name="Horváth")
    linker = FakeLinker([])
    sync(db, FakeSource(event(title="Paťo Veselý")), linker)
    assert linked(only(db)) == {"Patrik Veselý": MeetingLinkSource.TITLE}
    assert linker.requests == []


def test_claude_is_asked_only_when_the_event_changes(db: Session, pato: Person) -> None:
    person(db, "Patrik Horváth", "Paťo")
    linker = FakeLinker([pato.id])
    source = FakeSource(event())
    sync(db, source, linker)
    sync(db, source, linker)
    assert len(linker.requests) == 1
    source.events = [event(title="kafe s Paťom a pivo")]
    sync(db, source, linker)
    assert len(linker.requests) == 2


def test_manual_links_and_unlinks_stick_until_the_event_changes(db: Session, pato: Person) -> None:
    jana = person(db, "Jana Kováčová")
    source = FakeSource(event())
    sync(db, source)
    meeting = only(db)
    meetings.link_person(db, meeting, jana.id)
    meetings.unlink_person(db, meeting, pato.id)
    db.commit()
    sync(db, source)
    assert linked(only(db)) == {"Jana Kováčová": MeetingLinkSource.MANUAL}
    source.events = [event(title="kafe s Paťom!", location="Kaviareň")]
    sync(db, source)
    assert linked(only(db)) == {
        "Jana Kováčová": MeetingLinkSource.MANUAL,
        "Patrik Veselý": MeetingLinkSource.TITLE,
    }


def test_linking_by_hand_turns_an_automatic_link_manual(db: Session, pato: Person) -> None:
    sync(db, FakeSource(event()))
    meeting = meetings.link_person(db, only(db), pato.id)
    assert linked(meeting) == {"Patrik Veselý": MeetingLinkSource.MANUAL}
    with pytest.raises(NotFoundError):
        meetings.link_person(db, meeting, 999)
    meetings.unlink_person(db, meeting, pato.id)
    with pytest.raises(InvalidInputError):
        meetings.unlink_person(db, meeting, pato.id)


def test_merging_people_moves_their_meetings(db: Session, pato: Person) -> None:
    dupe = person(db, "P. Veselý")
    sync(db, FakeSource(event("a"), event("b", "Gym")))
    a, b = (only_uid(db, "a"), only_uid(db, "b"))
    meetings.link_person(db, a, dupe.id)  # both linked
    meetings.link_person(db, b, dupe.id)
    db.commit()
    duplicates.merge(db, pato.id, dupe.id)
    db.commit()
    db.expire_all()
    assert linked(only_uid(db, "a")) == {"Patrik Veselý": MeetingLinkSource.TITLE}
    assert linked(only_uid(db, "b")) == {"Patrik Veselý": MeetingLinkSource.MANUAL}


def test_deleting_a_person_drops_their_links(db: Session, pato: Person) -> None:
    sync(db, FakeSource(event()))
    people.delete_person(db, pato)
    db.commit()
    db.expire_all()
    assert only(db).links == []


# --- Queries ---


def test_overlapping_meetings_around_a_payment(db: Session, pato: Person) -> None:
    sync(
        db,
        FakeSource(
            event("a", hours=2), event("b", "Gym", hours=2), event("c", hours=8, cancelled=True)
        ),
    )
    paid = NOW + timedelta(hours=3, minutes=50)  # 50 minutes after it ended
    assert [m.calendar_uid for m in meetings.overlapping(db, paid)] == ["a"]  # only linked ones
    assert meetings.overlap_text(db, paid) == "kafe s Paťom (Patrik Veselý)"
    assert meetings.overlapping(db, NOW + timedelta(hours=5)) == []
    assert meetings.overlap_text(db, None) is None


def test_names() -> None:
    meeting = Meeting()
    assert meetings.names(meeting) == ""


# --- The brief ---


def fill_pato(db: Session, pato: Person) -> None:
    pato.birthday_month, pato.birthday_day, pato.birthday_year = 9, 30, 2000
    interactions.create_interaction(
        db,
        interactions.InteractionCreate(
            occurred_on=date(2026, 9, 12),
            kind=InteractionKind.MEET,
            summary="pizza po gyme",
            person_ids=[pato.id],
        ),
    )
    people.add_note(db, pato, "Hľadá novú prácu")
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=pato.id,
            direction=DebtDirection.THEY_OWE_ME,
            type=DebtType.MONEY,
            amount_minor=2400,
            description="pizza",
        ),
        "EUR",
    )
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=pato.id,
            direction=DebtDirection.THEY_OWE_ME,
            type=DebtType.ITEM,
            description="nabíjačka",
        ),
        "EUR",
    )
    ledger.create_debt(
        db,
        ledger.DebtCreate(
            person_id=pato.id,
            direction=DebtDirection.I_OWE_THEM,
            type=DebtType.FAVOR,
            description="sťahovanie",
        ),
        "EUR",
    )
    promises.create_promise(
        db,
        promises.PromiseCreate(
            person_id=pato.id,
            direction=PromiseDirection.I_PROMISED,
            text="poslať rozvrh",
            due=date(2026, 9, 26),
        ),
        date(2026, 9, 20),
    )
    db.commit()


def test_brief_has_everything_open_between_us(db: Session, pato: Person) -> None:
    fill_pato(db, pato)
    sync(db, FakeSource(event()))
    brief = meetings.build_brief(db, only(db), PRAGUE)
    assert brief.day == date(2026, 9, 25)
    (item,) = brief.people
    assert meetings.person_lines(item, brief.day) == [
        "🗓 last meet 12 Sep: pizza po gyme",
        "💶 owes you 24.00 €",
        "📦 has your nabíjačka",
        "📦 you owe a favor (sťahovanie)",
        "✅ you promised: poslať rozvrh (due 26 Sep)",
        "🎂 birthday in 5 days (30 Sep, turns 26)",
        "📝 Hľadá novú prácu",
    ]
    push = meetings.brief_push(brief, URL, PRAGUE)
    assert push.title == "📅 14:00 kafe s Paťom"
    assert push.body.startswith("🗓 last meet 12 Sep")
    assert push.click == f"{URL}/meetings/{only(db).id}"
    assert [(a.label, a.kind) for a in push.actions] == [("Brief", "view")]


def test_brief_for_several_people_names_them(db: Session, pato: Person) -> None:
    person(db, "Tereza Nováková", "Terka")
    sync(db, FakeSource(event(title="obed s Paťom a Terkou")))
    push = meetings.brief_push(meetings.build_brief(db, only(db), PRAGUE), URL, PRAGUE)
    assert push.body == (
        "Patrik Veselý:\nnothing open, nothing logged yet\n\n"
        "Tereza Nováková:\nnothing open, nothing logged yet"
    )


# --- Pushes ---


def send(
    db: Session, sender: FakeSender, now: datetime, summarizer: FakeSummarizer | None = None
) -> int:
    count = meetings.send_meeting_pushes(
        db,
        sender,
        base_url=URL,
        tz=PRAGUE,
        quiet=QUIET,
        minutes_before=30,
        minutes_after=15,
        summarizer=summarizer,
        now=now,
    )
    db.commit()
    return count


def test_brief_goes_out_30_minutes_before_once(db: Session, pato: Person) -> None:
    fill_pato(db, pato)
    sync(db, FakeSource(event()))
    start = only(db).start_utc
    sender = FakeSender()
    summarizer = FakeSummarizer()
    assert send(db, sender, start - timedelta(minutes=31), summarizer) == 0
    assert send(db, sender, start - timedelta(minutes=30), summarizer) == 1
    assert send(db, sender, start - timedelta(minutes=10), summarizer) == 0
    (push,) = sender.sent
    assert push.title == "📅 14:00 kafe s Paťom"
    assert push.body.startswith("💬 Paťo má novú prácu.\n\n🗓 last meet")
    meeting = only(db)
    assert meeting.brief_sent_at == start - timedelta(minutes=30)
    assert meeting.brief_summary == "Paťo má novú prácu."
    (request,) = summarizer.requests
    assert request.meeting == "kafe s Paťom"
    assert request.people[0].notes == ("Hľadá novú prácu",)
    assert request.people[0].last_interaction == "2026-09-12: pizza po gyme"


def test_brief_without_notes_or_claude_has_no_summary(db: Session, pato: Person) -> None:
    sync(db, FakeSource(event()))
    summarizer = FakeSummarizer()
    send(db, FakeSender(), only(db).start_utc - timedelta(minutes=5), summarizer)
    assert summarizer.requests == []  # nothing to summarize
    assert only(db).brief_summary is None


def test_claude_failing_doesnt_stop_the_brief(db: Session, pato: Person) -> None:
    fill_pato(db, pato)
    sync(db, FakeSource(event()))
    sender = FakeSender()
    send(db, sender, only(db).start_utc - timedelta(minutes=5), FakeSummarizer(ClaudeError("x")))
    assert len(sender.sent) == 1
    assert not sender.sent[0].body.startswith("💬")


def test_no_pushes_for_unlinked_cancelled_or_past_meetings(db: Session, pato: Person) -> None:
    sync(
        db,
        FakeSource(
            event("unlinked", "Gym", hours=0.25), event("cancelled", hours=0.25, cancelled=True)
        ),
    )
    sender = FakeSender()
    assert send(db, sender, NOW) == 0
    assert send(db, sender, NOW + timedelta(hours=2)) == 0  # after the end: no capture either
    assert sender.sent == []


def test_brief_and_capture_ignore_quiet_hours(db: Session, pato: Person) -> None:
    late = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)  # 23:00 in Prague
    sync(db, FakeSource(event(hours=13.25)))  # 23:15-00:15 local
    sender = FakeSender()
    assert send(db, sender, late) == 1
    assert send(db, sender, only(db).end_utc + timedelta(minutes=15)) == 1
    assert [p.title for p in sender.sent] == [
        "📅 23:15 kafe s Paťom",
        "📝 Anything to note about Patrik Veselý?",
    ]


def test_capture_prompt_15_minutes_after_the_end(db: Session, pato: Person) -> None:
    sync(db, FakeSource(event(hours=-2)))  # 08:00-09:00 local, ended an hour ago
    meeting = only(db)
    sender = FakeSender()
    assert send(db, sender, meeting.end_utc + timedelta(minutes=14)) == 0
    assert send(db, sender, meeting.end_utc + timedelta(minutes=15)) == 1
    assert send(db, sender, meeting.end_utc + timedelta(minutes=20)) == 0
    (push,) = sender.sent
    dictate = f"shortcuts://run-shortcut?name=Orbit%20Capture&input=text&text={meeting.id}"
    assert push.title == "📝 Anything to note about Patrik Veselý?"
    assert push.body == "kafe s Paťom · 08:00-09:00"
    assert push.click == dictate
    assert [(a.label, a.url) for a in push.actions] == [
        ("Dictate", dictate),
        ("Open", f"{URL}/meetings/{meeting.id}"),
    ]
    assert only(db).capture_sent_at is not None
    kinds = [n.kind for n in db.scalars(select(Notification))]
    assert kinds == ["meeting_capture"]


def test_a_late_capture_prompt_is_skipped(db: Session, pato: Person) -> None:
    sync(db, FakeSource(event(hours=-2)))
    sender = FakeSender()
    assert send(db, sender, only(db).end_utc + meetings.CAPTURE_GRACE + timedelta(minutes=16)) == 0


def test_a_rescheduled_meeting_gets_a_second_brief(db: Session, pato: Person) -> None:
    source = FakeSource(event(hours=0.25))
    sync(db, source)
    sender = FakeSender()
    assert send(db, sender, NOW) == 1
    source.events = [event(hours=24.25)]
    sync(db, source)
    assert send(db, sender, NOW + timedelta(hours=24)) == 1
    assert len(sender.sent) == 2
    keys = {n.dedupe_key for n in db.scalars(select(Notification))}
    assert len(keys) == 2


def test_an_orbit_appointment_is_never_cancelled_by_a_sync(db: Session) -> None:
    from orbit.services.meetings import ORBIT_CALENDAR, create_appointment

    start = NOW + timedelta(days=2)
    appointment = create_appointment(db, title="Haircut", start_utc=start)
    # Even a calendar that happens to be named like Orbit's own, read in full without it.
    sync(db, FakeSource(event("x", calendar_name=ORBIT_CALENDAR), calendars=(ORBIT_CALENDAR,)))
    assert not appointment.cancelled
