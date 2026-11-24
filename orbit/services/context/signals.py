"""Things Orbit can count for itself, so Claude does not have to find them in a large snapshot.

A review's snapshot is long; the collisions and the neglect it exists to find are short facts.
Orbit computes them (overlaps, overdue counts, people past their cadence, net money, bunched
deadlines, goals with nothing linked) and hands them over as `signals`. Claude still judges what
they mean; it is told these numbers are exact (SPEC §2.30).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from orbit.domain.enums import PromiseStatus, SchoolEventKind
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting
from orbit.domain.school import Lesson, SchoolEvent
from orbit.integrations.claude import mask_sensitive
from orbit.services import cadence, goals, ledger
from orbit.services.context.base import ContextWindow

MAX_LINES = 8  # lines per signal; the count says how many there were
BUNCH_HOURS = 48  # this many hours hold "bunched" deadlines ...
BUNCH_SIZE = 3  # ... when this many fall inside it


def _clip(value: str, limit: int = 80) -> str:
    one = " ".join(mask_sensitive(value).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


class Signal(BaseModel):
    count: int = 0
    lines: list[str] = Field(default_factory=list)  # the first few, most important first


class Signals(BaseModel):
    """Exact facts about the planned week, computed by Orbit. Read these before the snapshot."""

    clashes: Signal = Signal()  # a meeting overlapping a lesson, an exam or a timed deadline
    overdue: Signal = Signal()  # open promises, debts and goals past their date
    people_past_cadence: Signal = Signal()
    net_money: list[str] = Field(default_factory=list)  # one line per currency
    bunched_deadlines: Signal = Signal()
    goals_with_nothing_linked: Signal = Signal()  # no open or recently kept promise, no person


def _signal(lines: list[str]) -> Signal:
    return Signal(count=len(lines), lines=lines[:MAX_LINES])


def _local(moment: datetime, tz: ZoneInfo) -> str:
    return f"{moment.astimezone(tz):%a %-d %b %H:%M}"


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _clashes(session: Session, plan: ContextWindow, tz: ZoneInfo) -> list[str]:
    meetings = session.scalars(
        select(Meeting)
        .where(
            Meeting.cancelled.is_(False),
            Meeting.start_utc < plan.end_utc,
            Meeting.end_utc > plan.start_utc,
        )
        .order_by(Meeting.start_utc)
    ).all()
    lessons = session.scalars(
        select(Lesson).where(
            Lesson.cancelled.is_(False),
            Lesson.start_utc < plan.end_utc,
            Lesson.end_utc > plan.start_utc,
        )
    ).all()
    events = session.scalars(
        select(SchoolEvent).where(
            SchoolEvent.kind.in_((SchoolEventKind.EXAM, SchoolEventKind.DEADLINE)),
            SchoolEvent.all_day.is_(False),
            SchoolEvent.start_utc >= plan.start_utc,
            SchoolEvent.start_utc < plan.end_utc,
        )
    ).all()
    lines: list[str] = []
    for m in meetings:
        for lesson in lessons:
            if _overlap(m.start_utc, m.end_utc, lesson.start_utc, lesson.end_utc):
                lines.append(
                    f"meeting #{m.id} “{_clip(m.title)}” {_local(m.start_utc, tz)} overlaps "
                    f"the lesson “{_clip(lesson.title)}” {_local(lesson.start_utc, tz)}"
                )
        for event in events:
            end = event.end_utc or event.start_utc + timedelta(hours=1)
            if _overlap(m.start_utc, m.end_utc, event.start_utc, end):
                lines.append(
                    f"meeting #{m.id} “{_clip(m.title)}” {_local(m.start_utc, tz)} overlaps "
                    f"{event.kind.value} “{_clip(event.title)}” {_local(event.start_utc, tz)}"
                )
    return lines


def _overdue(session: Session, plan: ContextWindow) -> list[str]:
    lines: list[str] = []
    promises_ = session.scalars(
        select(Promise)
        .where(
            Promise.status == PromiseStatus.OPEN,
            Promise.due.is_not(None),
            Promise.due < plan.start,
        )
        .options(joinedload(Promise.person))
        .order_by(Promise.due, Promise.id)
    )
    for p in promises_:
        assert p.due is not None
        lines.append(
            f"promise #{p.id} “{_clip(p.text)}” ({_clip(p.person.display_name, 40)}) "
            f"due {p.due}, {(plan.start - p.due).days} days over"
        )
    debts = session.scalars(
        select(Debt)
        .where(Debt.settled_at.is_(None), Debt.due.is_not(None), Debt.due < plan.start)
        .options(joinedload(Debt.person))
        .order_by(Debt.due, Debt.id)
    )
    for d in debts:
        assert d.due is not None
        lines.append(
            f"debt #{d.id} ({_clip(d.person.display_name, 40)}, {d.type.value}) due {d.due}, "
            f"{(plan.start - d.due).days} days over"
        )
    for g in goals.active_goals(session):
        if g.target_date is not None and g.target_date < plan.start:
            lines.append(f"goal #{g.id} “{_clip(g.title)}” target date {g.target_date} has passed")
    return lines


def _past_cadence(session: Session, today: date, tz: ZoneInfo) -> list[str]:
    return [
        f"{_clip(k.person.display_name, 40)} (#{k.person.id}): {k.overdue_days(today)} days past "
        f"their {k.cadence_days}-day cadence"
        for k in cadence.due(session, today, tz)
    ]


def _net_money(session: Session) -> list[str]:
    return [
        f"{t.currency}: owed to you {t.owed_to_me / 100:.2f}, you owe {t.i_owe / 100:.2f}, "
        f"net {t.net / 100:+.2f}"
        for t in ledger.totals(ledger.balances(session))
    ]


def _bunched(session: Session, plan: ContextWindow, tz: ZoneInfo) -> list[str]:
    items: list[tuple[datetime, str]] = []
    for event in session.scalars(
        select(SchoolEvent).where(
            SchoolEvent.kind.in_((SchoolEventKind.EXAM, SchoolEventKind.DEADLINE)),
            SchoolEvent.start_utc >= plan.start_utc,
            SchoolEvent.start_utc < plan.end_utc,
        )
    ):
        items.append((event.start_utc, f"{event.kind.value} “{_clip(event.title, 50)}”"))
    for p in session.scalars(
        select(Promise).where(
            Promise.status == PromiseStatus.OPEN,
            Promise.due.is_not(None),
            Promise.due >= plan.start,
            Promise.due <= plan.end,
        )
    ):
        assert p.due is not None
        noon = datetime.combine(p.due, datetime.min.time(), tz).replace(hour=12)
        items.append((noon, f"promise “{_clip(p.text, 50)}”"))
    items.sort(key=lambda item: item[0])
    lines: list[str] = []
    used: set[int] = set()
    for i, (start, _) in enumerate(items):
        if i in used:
            continue
        cluster = [
            j
            for j, (when, _) in enumerate(items)
            if j >= i and when - start <= timedelta(hours=BUNCH_HOURS)
        ]
        if len(cluster) >= BUNCH_SIZE:
            used.update(cluster)
            lines.append(
                f"{len(cluster)} within {BUNCH_HOURS} h from {_local(start, tz)}: "
                + "; ".join(items[j][1] for j in cluster[:5])
            )
    return lines


def _unlinked_goals(session: Session, review: ContextWindow | None) -> list[str]:
    since = (review.start_utc if review is not None else None) or datetime.min
    lines: list[str] = []
    for g in goals.active_goals(session):
        open_linked = [p for p in g.promises if p.status is PromiseStatus.OPEN]
        kept = session.scalar(
            select(func.count(Promise.id)).where(
                Promise.goal_id == g.id,
                Promise.status == PromiseStatus.DONE,
                Promise.closed_at >= since,
            )
        )
        if not open_linked and not kept and not g.people:
            lines.append(f"#{g.id} (rank {g.rank}) “{_clip(g.title)}”")
    return lines


def compute_signals(
    session: Session, *, review: ContextWindow, plan: ContextWindow, today: date, tz: ZoneInfo
) -> Signals:
    """Signals for the planned week `plan`, looking back over the reviewed week `review`."""
    return Signals(
        clashes=_signal(_clashes(session, plan, tz)),
        overdue=_signal(_overdue(session, plan)),
        people_past_cadence=_signal(_past_cadence(session, today, tz)),
        net_money=_net_money(session),
        bunched_deadlines=_signal(_bunched(session, plan, tz)),
        goals_with_nothing_linked=_signal(_unlinked_goals(session, review)),
    )
