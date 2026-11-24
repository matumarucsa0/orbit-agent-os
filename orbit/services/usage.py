"""Claude token usage: calls are buffered in memory and written by the scheduler.

The Claude client reports each call from whatever thread made it, often in the middle of another
database transaction (a capture request, a calendar sync). Writing the record there, in its own
session, could wait on that transaction's lock (SQLite), so records are queued instead and
`UsageLog.flush` writes them once a minute (and at shutdown). A crash can lose the last minute of
records; these are statistics only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.usage import ClaudeCall
from orbit.integrations.claude import Usage


class UsageLog:
    """Thread-safe buffer of Claude calls; pass `add` to ClaudeClient as `on_usage`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[tuple[Usage, datetime]] = []

    def add(self, usage: Usage) -> None:
        with self._lock:
            self._pending.append((usage, utcnow()))

    def pending_cost_micro_eur(self, since: datetime) -> int:
        """Cost of buffered calls made at or after `since` (not yet in the database)."""
        with self._lock:
            return sum(u.cost_micro_eur for u, at in self._pending if at >= since)

    def flush(self, session: Session) -> int:
        """Add the buffered calls to `session` (the caller commits). Returns how many."""
        with self._lock:
            pending, self._pending = self._pending, []
        for usage, at in pending:
            session.add(
                ClaudeCall(
                    created_at=at,
                    purpose=usage.purpose,
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    ok=usage.ok,
                    latency_ms=usage.latency_ms,
                    prompt_version=usage.prompt_version,
                    cost_micro_eur=usage.cost_micro_eur,
                )
            )
        session.flush()
        return len(pending)


@dataclass(frozen=True)
class UsageTotal:
    month: date  # the first day of the month (local time)
    purpose: str
    calls: int
    failed: int
    input_tokens: int
    output_tokens: int
    cost_micro_eur: int = 0


def month_start(day: date) -> date:
    """The first day of the month containing `day`."""
    return day.replace(day=1)


def _next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def months_back(day: date, count: int) -> date:
    """The first day of the month `count` months before the one `day` is in."""
    index = day.year * 12 + day.month - 1 - count
    return date(index // 12, index % 12 + 1, 1)


def totals(session: Session, first_month: date, last_month: date, tz: ZoneInfo) -> list[UsageTotal]:
    """Calls and tokens per (local) month and purpose, from `first_month` through `last_month`,
    oldest month first, then by purpose."""
    found: list[UsageTotal] = []
    month = month_start(first_month)
    while month <= last_month:
        start = datetime.combine(month, time(), tzinfo=tz)
        end = datetime.combine(_next_month(month), time(), tzinfo=tz)
        rows = session.execute(
            select(
                ClaudeCall.purpose,
                func.count(ClaudeCall.id),
                func.sum(case((ClaudeCall.ok.is_(False), 1), else_=0)),
                func.coalesce(func.sum(ClaudeCall.input_tokens), 0),
                func.coalesce(func.sum(ClaudeCall.output_tokens), 0),
                func.coalesce(func.sum(ClaudeCall.cost_micro_eur), 0),
            )
            .where(ClaudeCall.created_at >= start, ClaudeCall.created_at < end)
            .group_by(ClaudeCall.purpose)
            .order_by(ClaudeCall.purpose)
        ).all()
        found += [
            UsageTotal(
                month, purpose, calls, int(failed or 0), int(tokens_in), int(tokens_out), int(cost)
            )
            for purpose, calls, failed, tokens_in, tokens_out, cost in rows
        ]
        month = _next_month(month)
    return found


@dataclass(frozen=True)
class MonthSummary:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_micro_eur: int = 0


def month_summary(session: Session, day: date, tz: ZoneInfo) -> MonthSummary:
    """All purposes together, for the month `day` is in."""
    rows = totals(session, day, day, tz)
    return MonthSummary(
        calls=sum(r.calls for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cost_micro_eur=sum(r.cost_micro_eur for r in rows),
    )


def month_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the local month containing `day`, as aware datetimes
    in `tz`."""
    first = month_start(day)
    return (
        datetime.combine(first, time(), tzinfo=tz),
        datetime.combine(_next_month(first), time(), tzinfo=tz),
    )


def month_cost_micro_eur(session: Session, day: date, tz: ZoneInfo) -> int:
    """Estimated spend of the API backend in the (local) month `day` is in."""
    start, end = month_bounds(day, tz)
    total = session.scalar(
        select(func.coalesce(func.sum(ClaudeCall.cost_micro_eur), 0)).where(
            ClaudeCall.created_at >= start, ClaudeCall.created_at < end
        )
    )
    return int(total or 0)


def format_tokens(count: int) -> str:
    """1234 -> "1.2k", 2_500_000 -> "2.5M"."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)
