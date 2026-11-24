"""The monthly Claude budget (CLAUDE_MONTHLY_BUDGET_EUR): a guard before each API call and a push.

Spend is the sum of `ClaudeCall.cost_micro_eur` in the current local month, plus calls still in the
usage buffer. The API client calls `BudgetGuard` before each request; at or above the budget it
raises `BudgetExceeded`, which callers handle like any other Claude error (capture saves a note
draft, classification stays manual, ...). The fake backend and the Claude Code worker never go
through the guard. `check_alerts` runs after each usage flush and sends one push at 80 percent
and one at 100 percent per month (the notification dedupe key carries the month).

The figures are estimates (orbit.integrations.pricing), so the stop is approximate: a request
already in flight when the limit is crossed still finishes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from orbit.domain.base import utcnow
from orbit.integrations.claude import BudgetExceeded
from orbit.integrations.ntfy import PushMessage, PushSender
from orbit.integrations.pricing import MICRO, format_eur
from orbit.services import notifications, usage

log = logging.getLogger(__name__)

ALERT_FRACTION = 0.8
KIND = "claude_budget"


@dataclass(frozen=True)
class BudgetState:
    budget_micro_eur: int  # 0 = no limit
    spent_micro_eur: int

    @property
    def enabled(self) -> bool:
        return self.budget_micro_eur > 0

    @property
    def fraction(self) -> float:
        return self.spent_micro_eur / self.budget_micro_eur if self.enabled else 0.0

    @property
    def exceeded(self) -> bool:
        return self.enabled and self.spent_micro_eur >= self.budget_micro_eur


def budget_micro(budget_eur: float) -> int:
    """Euros to micro-euros, the unit stored on `ClaudeCall.cost_micro_eur`."""
    return round(budget_eur * MICRO)


def state(
    session: Session, budget_eur: float, tz: ZoneInfo, now: datetime | None = None
) -> BudgetState:
    """Spend recorded in the database against the budget, for the local month containing `now`."""
    today = (now or utcnow()).astimezone(tz).date()
    return BudgetState(budget_micro(budget_eur), usage.month_cost_micro_eur(session, today, tz))


class BudgetGuard:
    """Callable for `ClaudeClient(budget_guard=...)`. Fails open if the database cannot be read:
    a broken statistics table must not take Claude down."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        usage_log: usage.UsageLog,
        budget_eur: float,
        tz: ZoneInfo,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._log = usage_log
        self._budget = budget_micro(budget_eur)
        self._tz = tz
        self._clock = clock

    def spent_micro_eur(self) -> int:
        now = self._clock()
        today = now.astimezone(self._tz).date()
        start, _ = usage.month_bounds(today, self._tz)
        pending = self._log.pending_cost_micro_eur(start)  # read first: a flush in between is
        with self._sessions() as session:  # then counted twice (safe side), never missed
            return pending + usage.month_cost_micro_eur(session, today, self._tz)

    def __call__(self) -> None:
        if self._budget <= 0:
            return
        try:
            spent = self.spent_micro_eur()
        except Exception:
            log.exception("reading the Claude spend failed; not enforcing the budget this call")
            return
        if spent >= self._budget:
            raise BudgetExceeded(
                f"The monthly Claude budget is used up ({format_eur(spent)} of "
                f"{format_eur(self._budget)})."
            )


def check_alerts(
    session: Session,
    sender: PushSender | None,
    budget_eur: float,
    tz: ZoneInfo,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> list[str]:
    """Push once per month at 80 and at 100 percent. Returns the dedupe keys recorded."""
    moment = now or utcnow()
    current = state(session, budget_eur, tz, moment)
    if not current.enabled:
        return []
    month = moment.astimezone(tz).strftime("%Y-%m")
    made: list[str] = []
    steps = (
        (ALERT_FRACTION, "80", "Claude budget: 80 percent used", None),
        (1.0, "100", "Claude budget used up", "API calls stop until next month."),
    )
    for fraction, label, title, tail in steps:
        if current.fraction < fraction:
            continue
        body = f"{format_eur(current.spent_micro_eur)} of {format_eur(current.budget_micro_eur)}."
        if tail:
            body += f" {tail}"
        key = f"claude-budget-{label}:{month}"
        sent = notifications.notify(
            session,
            sender,
            PushMessage(title=title, body=body, tags=("moneybag",)),
            dedupe_key=key,
            kind=KIND,
            quiet=quiet,
            now=moment,
        )
        if sent is not None:
            made.append(key)
    return made
