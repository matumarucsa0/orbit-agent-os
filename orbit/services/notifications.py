"""Recording and sending pushes: dedupe keys, the quiet-hours queue, retries. Callers commit.

Every push is written to the Notification table first, keyed by `dedupe_key`, so the same push is
never sent twice. During quiet hours it waits until they end, unless its kind is exempt
(pre-meeting briefs and post-meeting capture prompts, which are only useful right away).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.inbox import Notification
from orbit.integrations.ntfy import NtfyError, PushMessage, PushSender
from orbit.settings import Settings

log = logging.getLogger(__name__)

# Kinds that are sent even during quiet hours.
QUIET_EXEMPT_KINDS = frozenset({"meeting_brief", "meeting_capture"})
MAX_ATTEMPTS = 5
RETRY_DELAY = timedelta(minutes=5)
# A push that couldn't go out within a day is stale (yesterday's birthday); stop trying.
MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time
    tz: ZoneInfo
    # The delivery policy travels with the quiet hours: every sender already receives this object.
    retry_delay: timedelta = RETRY_DELAY
    max_attempts: int = MAX_ATTEMPTS
    max_age: timedelta = MAX_AGE

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(self.tz).time()
        if self.start == self.end:
            return False  # "22:00-22:00": no quiet hours
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end  # wraps past midnight

    def end_after(self, moment: datetime) -> datetime:
        """The next moment (UTC-aware) the clock reads `end`, strictly after `moment`."""
        local = moment.astimezone(self.tz)
        candidate = datetime.combine(local.date(), self.end, tzinfo=self.tz)
        if candidate <= local:
            candidate = datetime.combine(local.date() + timedelta(days=1), self.end, tzinfo=self.tz)
        return candidate.astimezone(moment.tzinfo)


def quiet_hours(settings: Settings) -> QuietHours:
    start, end = settings.quiet_hours
    return QuietHours(
        start,
        end,
        settings.timezone,
        retry_delay=timedelta(minutes=settings.notification_retry_minutes),
        max_attempts=settings.notification_max_attempts,
        max_age=timedelta(hours=settings.notification_max_age_hours),
    )


def was_sent(session: Session, dedupe_key: str) -> bool:
    return (
        session.scalar(select(Notification.id).where(Notification.dedupe_key == dedupe_key))
        is not None
    )


def _deliver(
    sender: PushSender | None,
    notification: Notification,
    now: datetime,
    retry_delay: timedelta = RETRY_DELAY,
) -> bool:
    if sender is None:
        notification.last_error = "ntfy isn't configured (NTFY_TOPIC)"
        return False
    notification.attempts += 1
    try:
        sender.send(PushMessage.from_dict(notification.payload_json))
    except NtfyError as exc:
        log.warning("push %s failed: %s", notification.dedupe_key, exc)
        notification.last_error = str(exc)
        notification.not_before = now + retry_delay
        return False
    notification.delivered = True
    notification.sent_at = now
    notification.last_error = None
    return True


def notify(
    session: Session,
    sender: PushSender | None,
    message: PushMessage,
    *,
    dedupe_key: str,
    kind: str,
    quiet: QuietHours,
    now: datetime | None = None,
) -> Notification | None:
    """Record a push and send it now, or queue it until quiet hours end.

    Returns None when a push with this dedupe key already exists (sent or queued). A failed send
    stays queued and is retried by `flush_due`.
    """
    if was_sent(session, dedupe_key):
        return None
    now = now or utcnow()
    held = kind not in QUIET_EXEMPT_KINDS and quiet.contains(now)
    notification = Notification(
        dedupe_key=dedupe_key,
        kind=kind,
        title=message.title,
        body=message.body,
        payload_json=message.to_dict(),
        created_at=now,
        not_before=quiet.end_after(now) if held else now,
    )
    session.add(notification)
    session.flush()  # the unique dedupe key is claimed before anything goes out
    if not held:
        _deliver(sender, notification, now, quiet.retry_delay)
        session.flush()
    return notification


def flush_due(
    session: Session,
    sender: PushSender | None,
    *,
    quiet: QuietHours,
    now: datetime | None = None,
    ignore_not_before: bool = False,
) -> int:
    """Send queued pushes whose time has come; returns how many were delivered.

    `ignore_not_before` (Run now) also takes pushes waiting for a retry; quiet hours still hold."""
    if sender is None:
        return 0
    now = now or utcnow()
    query = select(Notification).where(
        Notification.delivered.is_(False),
        Notification.attempts < quiet.max_attempts,
        Notification.created_at >= now - quiet.max_age,
    )
    if not ignore_not_before:
        query = query.where(Notification.not_before <= now)
    due = session.scalars(query.order_by(Notification.not_before, Notification.id))
    sent = 0
    for notification in due:
        if notification.kind not in QUIET_EXEMPT_KINDS and quiet.contains(now):
            notification.not_before = quiet.end_after(now)
            continue
        sent += _deliver(sender, notification, now, quiet.retry_delay)
    session.flush()
    return sent
