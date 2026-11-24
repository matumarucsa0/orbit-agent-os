"""Reminder pushes (birthdays, name days, lent items, promises) and the Sunday digest.
Callers commit.

The scheduler calls `send_reminders` every minute. Each push has a dedupe key, so running it again
(or after a restart) never sends one twice, and a push that's due but not yet sent (the server was
down at 09:00) still goes out later that day. Quiet hours are handled by `notifications.notify`:
a reminder that falls into them waits until they end.

- Birthdays / name days: from 09:00 on the day, plus 5 days before for people tagged `close`.
  Only sent while they can still arrive on the right day (never "happy birthday" the day after).
- Lent items: once, from 09:00 on the day the item's reminder is due (ITEM_REMINDER_DAYS after it
  was lent, or its own `reminder_days`); editing the reminder re-arms it.
- Promises I made: once, from 09:00 on the due date (or later, if it's already overdue);
  changing the due date re-arms it.
- Digest: Sundays from 18:00; also lists who's due a catch-up (keep-in-touch cadence), the
  school week (lessons, exams, deadlines) and this month's Claude usage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from orbit.domain.base import utcnow
from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    PromiseDirection,
    PromiseStatus,
    ProposalStatus,
)
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting
from orbit.domain.money import format_amount
from orbit.domain.people import Person
from orbit.integrations.ntfy import PushMessage, PushSender, view_action
from orbit.services import (
    cadence,
    dates,
    goals,
    interactions,
    ledger,
    meetings,
    notifications,
    school,
    usage,
)
from orbit.services.inbox import pending_count

REMINDER_TIME = time(9, 0)
DIGEST_WEEKDAY = 6  # Sunday
DIGEST_TIME = time(18, 0)
CLOSE_TAG = "close"  # normalized tag name
CLOSE_DAYS_AHEAD = 5
DIGEST_LIST = 5  # entries listed per digest section, the rest are counted
MAX_BODY = 3500  # ntfy caps a message at 4096 bytes

BIRTHDAY_KIND = "birthday"
NAMEDAY_KIND = "nameday"
ITEM_KIND = "lent_item"
PROMISE_KIND = "promise_due"
DIGEST_KIND = "digest"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def short_date(day: date) -> str:
    return f"{day.day} {day:%b}"


def weekday_date(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {short_date(day)}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sentence(text: str) -> str:
    """Upper-case the first letter only ("owed to you 10.00 Kč"; str.capitalize would give "kč")."""
    return text[:1].upper() + text[1:]


def _person_url(base_url: str, person: Person) -> str:
    return f"{base_url}/people/{person.id}"


# --- What's due ---


def is_close(person: Person) -> bool:
    return any(tag.name_norm == CLOSE_TAG for tag in person.tags)


def due_items(session: Session, today: date, default_days: int, tz: ZoneInfo) -> list[Debt]:
    """Open items I lent whose reminder day has come, oldest first."""
    found = session.scalars(
        select(Debt)
        .where(
            Debt.type == DebtType.ITEM,
            Debt.direction == DebtDirection.THEY_OWE_ME,
            Debt.settled_at.is_(None),
        )
        .options(joinedload(Debt.person))
        .order_by(Debt.created_at, Debt.id)
    )
    return [
        d
        for d in found
        if (remind_on := ledger.item_reminder_on(d, default_days, tz)) and remind_on <= today
    ]


def due_promises(session: Session, today: date) -> list[Promise]:
    """Open promises I made that are due today or overdue, most overdue first."""
    return list(
        session.scalars(
            select(Promise)
            .where(
                Promise.status == PromiseStatus.OPEN,
                Promise.direction == PromiseDirection.I_PROMISED,
                Promise.due.is_not(None),
                Promise.due <= today,
            )
            .options(joinedload(Promise.person))
            .order_by(Promise.due, Promise.id)
        )
    )


def overdue_promises(session: Session, today: date) -> list[Promise]:
    """Open promises (either direction) past their due date, most overdue first."""
    return list(
        session.scalars(
            select(Promise)
            .where(
                Promise.status == PromiseStatus.OPEN,
                Promise.due.is_not(None),
                Promise.due < today,
            )
            .options(joinedload(Promise.person))
            .order_by(Promise.due, Promise.id)
        )
    )


# --- Pushes ---


def _last_contact(session: Session, person: Person) -> str | None:
    last = interactions.last_interaction(session, person.id)
    if last is None:
        return None
    summary = f": {_clip(last.summary, 60)}" if last.summary else ""
    return f"🗓 last {last.kind.value} {short_date(last.occurred_on)}{summary}"


def date_push(
    upcoming: dates.UpcomingDate, today: date, base_url: str, last_contact: str | None
) -> PushMessage:
    person = upcoming.person
    days = (upcoming.date - today).days
    when = "today" if days == 0 else f"in {days} days"
    if upcoming.kind == "birthday":
        title = f"🎂 {person.display_name}: birthday {when}"
        facts = [_sentence(upcoming.detail)] if upcoming.detail else []
        tags: tuple[str, ...] = ("birthday",)
    else:
        title = f"🎉 {person.display_name}: name day {when}"
        facts = [f"Name day of {upcoming.detail}"] if upcoming.detail else []
        tags = ("tada",)
    if days:
        facts.insert(0, weekday_date(upcoming.date))
    lines = [" · ".join(facts)] if facts else []
    if last_contact:
        lines.append(last_contact)
    url = _person_url(base_url, person)
    return PushMessage(
        title=_clip(title, 200),
        body="\n".join(lines) or "Wish them well.",
        click=url,
        tags=tags,
        actions=(
            view_action("Open", url),
            view_action("✍️ Draft", f"{url}/draft?occasion={upcoming.kind}"),
        ),
    )


def item_push(debt: Debt, today: date, base_url: str, tz: ZoneInfo) -> PushMessage:
    since = debt.created_at.astimezone(tz).date()
    days = (today - since).days
    url = _person_url(base_url, debt.person)
    return PushMessage(
        title=_clip(f"📦 {debt.person.display_name} still has your {debt.description}", 200),
        body=f"Lent {short_date(since)} ({days} days ago). Time to ask for it back?",
        click=url,
        tags=("package",),
        actions=(view_action("Open", url),),
    )


def promise_push(promise: Promise, today: date, base_url: str, tz: ZoneInfo) -> PushMessage:
    assert promise.due is not None
    if promise.due == today:
        title = f"✅ Due today: {promise.text}"
    else:
        title = f"⏰ Overdue since {short_date(promise.due)}: {promise.text}"
    made = promise.created_at.astimezone(tz).date()
    url = _person_url(base_url, promise.person)
    return PushMessage(
        title=_clip(title, 200),
        body=f"You promised {promise.person.display_name} on {short_date(made)}.",
        click=url,
        tags=("white_check_mark",),
        actions=(view_action("Open", url),),
    )


# --- The Sunday digest ---


@dataclass
class Digest:
    today: date  # the Sunday it's for
    inbox_pending: int
    inbox_new: int  # of those, created in the last 7 days
    totals: list[ledger.CurrencyTotal]
    overdue: list[Promise]
    due_soon: list[Promise]  # open, due in the coming week
    items: list[Debt]  # open items I lent, oldest first
    dates: list[dates.UpcomingDate]  # the coming week
    meetings: list[Meeting]  # the coming week
    keep_in_touch: list[cadence.KeepInTouch]  # due a catch-up, most overdue first
    claude: usage.MonthSummary  # this month so far
    goals_review_days: int | None = None  # days since a goal was reviewed, once it's a week or more
    school_week: school.WeekSummary | None = None  # lessons, exams and deadlines of the week

    @property
    def week(self) -> tuple[date, date]:
        return self.today + timedelta(days=1), self.today + timedelta(days=7)


def build_digest(session: Session, now: datetime, tz: ZoneInfo) -> Digest:
    today = now.astimezone(tz).date()
    first, last = today + timedelta(days=1), today + timedelta(days=7)
    week_start = datetime.combine(first, time(), tzinfo=tz)
    week_end = datetime.combine(last + timedelta(days=1), time(), tzinfo=tz)
    inbox_new = (
        session.scalar(
            select(func.count(Proposal.id)).where(
                Proposal.status == ProposalStatus.PENDING,
                Proposal.created_at >= now - timedelta(days=7),
            )
        )
        or 0
    )
    items = session.scalars(
        select(Debt)
        .where(
            Debt.type == DebtType.ITEM,
            Debt.direction == DebtDirection.THEY_OWE_ME,
            Debt.settled_at.is_(None),
        )
        .options(joinedload(Debt.person))
        .order_by(Debt.created_at, Debt.id)
    )
    due_soon = session.scalars(
        select(Promise)
        .where(Promise.status == PromiseStatus.OPEN, Promise.due >= today, Promise.due <= last)
        .options(joinedload(Promise.person))
        .order_by(Promise.due, Promise.id)
    )
    return Digest(
        today=today,
        inbox_pending=pending_count(session),
        inbox_new=inbox_new,
        totals=ledger.totals(ledger.balances(session)),
        overdue=overdue_promises(session, today),
        due_soon=list(due_soon),
        items=list(items),
        dates=dates.upcoming(session, first, 6),
        meetings=meetings.list_meetings(
            session, week_start.astimezone(now.tzinfo), week_end.astimezone(now.tzinfo)
        ),
        keep_in_touch=cadence.due(session, today, tz),
        claude=usage.month_summary(session, today, tz),
        goals_review_days=goals.review_overdue_days(session, now),
        school_week=school.week_summary(
            session, week_start.astimezone(now.tzinfo), week_end.astimezone(now.tzinfo)
        ),
    )


def _section(head: str, lines: Sequence[str]) -> str:
    """A heading and its entries, one per line; past DIGEST_LIST the rest are only counted."""
    shown = [f"• {line}" for line in lines[:DIGEST_LIST]]
    if len(lines) > DIGEST_LIST:
        shown.append(f"• … and {len(lines) - DIGEST_LIST} more")
    return "\n".join([head, *shown])


def _promise_entry(promise: Promise) -> str:
    who = promise.person.display_name
    what = _clip(promise.text, 60)
    due = f" ({short_date(promise.due)})" if promise.due else ""
    if promise.direction is PromiseDirection.I_PROMISED:
        return f"you → {who}: {what}{due}"
    return f"{who} → you: {what}{due}"


def _date_entry(upcoming: dates.UpcomingDate) -> str:
    if upcoming.kind == "birthday":
        detail = f" ({upcoming.detail})" if upcoming.detail else ""
        return f"🎂 {weekday_date(upcoming.date)} {upcoming.person.display_name}{detail}"
    return f"🎉 {weekday_date(upcoming.date)} {upcoming.person.display_name} (name day)"


def _catch_up_entry(entry: cadence.KeepInTouch) -> str:
    who = entry.person.display_name
    if entry.last_contact is None:
        return f"{who} (nothing logged yet)"
    return f"{who} (last {short_date(entry.last_contact)})"


def digest_sections(digest: Digest, tz: ZoneInfo) -> list[str]:
    """The digest as text paragraphs (the push body), empty sections left out."""
    sections: list[str] = []
    if digest.inbox_pending:
        new = f", {digest.inbox_new} new this week" if digest.inbox_new else ""
        sections.append(f"📥 {digest.inbox_pending} waiting in the Inbox{new}")
    money = []
    for total in digest.totals:
        parts = []
        if total.owed_to_me:
            parts.append(f"owed to you {format_amount(total.owed_to_me, total.currency)}")
        if total.i_owe:
            parts.append(f"you owe {format_amount(total.i_owe, total.currency)}")
        money.append(", ".join(parts))
    if money:
        sections.append("💶 " + _sentence("; ".join(money)))
    if digest.overdue:
        entries = [_promise_entry(p) for p in digest.overdue]
        sections.append(_section(f"⏰ Overdue promises ({len(entries)})", entries))
    if digest.due_soon:
        entries = [_promise_entry(p) for p in digest.due_soon]
        sections.append(_section(f"✅ Promises due this week ({len(entries)})", entries))
    if digest.items:
        entries = [
            f"{d.description} → {d.person.display_name}"
            f" (since {short_date(d.created_at.astimezone(tz).date())})"
            for d in digest.items
        ]
        sections.append(_section(f"📦 Lent out ({len(entries)})", entries))
    if digest.dates:
        sections.append(_section("Birthdays & name days", [_date_entry(u) for u in digest.dates]))
    if digest.meetings:
        entries = []
        for m in digest.meetings:
            start = m.start_utc.astimezone(tz)
            who = f" (with {meetings.names(m)})" if m.links else ""
            entries.append(f"{_WEEKDAYS[start.weekday()]} {start:%H:%M} {m.title}{who}")
        sections.append(_section(f"📅 Meetings ({len(entries)})", entries))
    if digest.school_week is not None and (text := school.digest_section(digest.school_week, tz)):
        sections.append(text)
    if digest.keep_in_touch:
        entries = [_catch_up_entry(k) for k in digest.keep_in_touch]
        sections.append(_section(f"👋 Time to catch up ({len(entries)})", entries))
    if digest.goals_review_days is not None:
        sections.append(f"🎯 Review your goals: last reviewed {digest.goals_review_days} days ago")
    return sections


def claude_line(digest: Digest) -> str | None:
    """This month's Claude usage, as the digest's last line; None when there was none."""
    claude = digest.claude
    if not claude.calls:
        return None
    calls = f"{claude.calls} call{'s' if claude.calls != 1 else ''}"
    tokens = (
        f"{usage.format_tokens(claude.input_tokens)} in / "
        f"{usage.format_tokens(claude.output_tokens)} out"
    )
    return f"🤖 Claude in {digest.today:%B}: {calls}, {tokens} tokens"


def digest_push(digest: Digest, base_url: str, tz: ZoneInfo) -> PushMessage:
    first, last = digest.week
    sections = digest_sections(digest, tz) or ["Nothing open and nothing planned. A quiet week."]
    if line := claude_line(digest):
        sections.append(line)
    body = "\n\n".join(sections)
    if len(body) > MAX_BODY:
        body = body[: MAX_BODY - 1].rstrip() + "…"
    return PushMessage(
        title=f"🗓 Your week: {short_date(first)} to {short_date(last)}",
        body=body,
        click=f"{base_url}/",
        tags=("spiral_calendar",),
        actions=(view_action("Open Orbit", f"{base_url}/"),),
    )


# --- Sending ---


def _arrives_today(now: datetime, quiet: notifications.QuietHours, tz: ZoneInfo) -> bool:
    """Whether a push recorded now goes out on today's (local) date: not held by quiet hours
    that last past midnight."""
    if not quiet.contains(now):
        return True
    return quiet.end_after(now).astimezone(tz).date() == now.astimezone(tz).date()


@dataclass
class _Sender:
    session: Session
    sender: PushSender | None
    quiet: notifications.QuietHours
    now: datetime
    count: int = 0

    def pending(self, dedupe_key: str) -> bool:
        return not notifications.was_sent(self.session, dedupe_key)

    def send(self, message: PushMessage, *, dedupe_key: str, kind: str) -> None:
        record = notifications.notify(
            self.session,
            self.sender,
            message,
            dedupe_key=dedupe_key,
            kind=kind,
            quiet=self.quiet,
            now=self.now,
        )
        self.count += record is not None


def send_date_reminders(
    session: Session,
    out: _Sender,
    *,
    base_url: str,
    tz: ZoneInfo,
    days_ahead: int = CLOSE_DAYS_AHEAD,
) -> None:
    today = out.now.astimezone(tz).date()
    if not _arrives_today(out.now, out.quiet, tz):
        return
    ahead = today + timedelta(days=days_ahead)
    for upcoming in dates.upcoming(session, today, days_ahead):
        if upcoming.date == today:
            when = "day"
        elif upcoming.date == ahead and is_close(upcoming.person):
            when = "ahead"
        else:
            continue
        kind = BIRTHDAY_KIND if upcoming.kind == "birthday" else NAMEDAY_KIND
        key = f"{kind}:{upcoming.person.id}:{upcoming.date.isoformat()}:{when}"
        if not out.pending(key):
            continue
        last = _last_contact(session, upcoming.person)
        out.send(date_push(upcoming, today, base_url, last), dedupe_key=key, kind=kind)


def send_item_reminders(
    session: Session, out: _Sender, *, base_url: str, tz: ZoneInfo, default_days: int
) -> None:
    today = out.now.astimezone(tz).date()
    for debt in due_items(session, today, default_days, tz):
        remind_on = ledger.item_reminder_on(debt, default_days, tz)
        assert remind_on is not None
        key = f"{ITEM_KIND}:{debt.id}:{remind_on.isoformat()}"
        if out.pending(key):
            out.send(item_push(debt, today, base_url, tz), dedupe_key=key, kind=ITEM_KIND)


def send_promise_reminders(session: Session, out: _Sender, *, base_url: str, tz: ZoneInfo) -> None:
    today = out.now.astimezone(tz).date()
    for promise in due_promises(session, today):
        assert promise.due is not None
        key = f"{PROMISE_KIND}:{promise.id}:{promise.due.isoformat()}"
        if out.pending(key):
            out.send(promise_push(promise, today, base_url, tz), dedupe_key=key, kind=PROMISE_KIND)


def send_digest(
    session: Session,
    out: _Sender,
    *,
    base_url: str,
    tz: ZoneInfo,
    weekday: int = DIGEST_WEEKDAY,
    at: time = DIGEST_TIME,
) -> None:
    local = out.now.astimezone(tz)
    if local.weekday() != weekday or local.time() < at:
        return
    key = f"{DIGEST_KIND}:{local.date().isoformat()}"
    if out.pending(key):
        digest = build_digest(session, out.now, tz)
        out.send(digest_push(digest, base_url, tz), dedupe_key=key, kind=DIGEST_KIND)


def send_reminders(
    session: Session,
    sender: PushSender | None,
    *,
    base_url: str,
    tz: ZoneInfo,
    quiet: notifications.QuietHours,
    item_reminder_days: int,
    now: datetime | None = None,
    reminder_time: time = REMINDER_TIME,
    close_days_ahead: int = CLOSE_DAYS_AHEAD,
    digest_weekday: int = DIGEST_WEEKDAY,
    digest_time: time = DIGEST_TIME,
) -> int:
    """Record (and send, or queue for after quiet hours) every reminder that's due and hasn't
    gone out yet. Returns how many were recorded."""
    out = _Sender(session, sender, quiet, now or utcnow())
    if out.now.astimezone(tz).time() >= reminder_time:
        send_date_reminders(session, out, base_url=base_url, tz=tz, days_ahead=close_days_ahead)
        send_item_reminders(session, out, base_url=base_url, tz=tz, default_days=item_reminder_days)
        send_promise_reminders(session, out, base_url=base_url, tz=tz)
    send_digest(session, out, base_url=base_url, tz=tz, weekday=digest_weekday, at=digest_time)
    session.flush()
    return out.count
