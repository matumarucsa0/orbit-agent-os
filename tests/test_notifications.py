from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from orbit.domain.inbox import Notification
from orbit.integrations.ntfy import NtfyError, PushMessage, http_action
from orbit.services.notifications import (
    MAX_ATTEMPTS,
    QuietHours,
    flush_due,
    notify,
)

PRAGUE = ZoneInfo("Europe/Prague")
QUIET = QuietHours(time(22, 0), time(8, 0), PRAGUE)
# 2026-09-23 is CEST (UTC+2): 12:00 local = 10:00 UTC, 23:30 local = 21:30 UTC.
NOON = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
LATE = datetime(2026, 9, 23, 21, 30, tzinfo=UTC)
MORNING = datetime(2026, 9, 24, 6, 0, tzinfo=UTC)  # 08:00 local


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[PushMessage] = []
        self.fail = fail

    def send(self, message: PushMessage) -> None:
        if self.fail:
            raise NtfyError("ntfy answered 500")
        self.sent.append(message)


def push(title: str = "🎂 Pato's birthday") -> PushMessage:
    return PushMessage(title, "Today", actions=(http_action("Done", "https://x/a/t"),))


# --- Quiet hours ---


@pytest.mark.parametrize(
    ("local", "quiet"),
    [
        (time(21, 59), False),
        (time(22, 0), True),
        (time(23, 30), True),
        (time(0, 0), True),
        (time(7, 59), True),
        (time(8, 0), False),
        (time(12, 0), False),
    ],
)
def test_quiet_hours_wrap_past_midnight(local: time, quiet: bool) -> None:
    moment = datetime.combine(datetime(2026, 9, 23).date(), local, tzinfo=PRAGUE)
    assert QUIET.contains(moment) is quiet


def test_quiet_hours_within_one_day_and_disabled() -> None:
    siesta = QuietHours(time(13, 0), time(15, 0), PRAGUE)
    assert siesta.contains(datetime(2026, 9, 23, 14, 0, tzinfo=PRAGUE))
    assert not siesta.contains(datetime(2026, 9, 23, 15, 0, tzinfo=PRAGUE))
    off = QuietHours(time(22, 0), time(22, 0), PRAGUE)
    assert not off.contains(LATE)


def test_quiet_hours_end_is_the_next_morning() -> None:
    assert QUIET.end_after(LATE) == MORNING
    after_midnight = datetime(2026, 9, 23, 23, 30, tzinfo=UTC)  # 01:30 local on the 24th
    assert QUIET.end_after(after_midnight) == MORNING


# --- notify ---


def test_notify_sends_and_records(db: Session) -> None:
    sender = FakeSender()
    notification = notify(
        db, sender, push(), dedupe_key="bday:1:2026", kind="birthday", quiet=QUIET, now=NOON
    )
    assert notification is not None
    assert notification.delivered and notification.sent_at == NOON
    assert notification.attempts == 1
    assert [m.title for m in sender.sent] == ["🎂 Pato's birthday"]
    assert notification.payload_json["actions"][0]["url"] == "https://x/a/t"


def test_same_dedupe_key_is_never_sent_twice(db: Session) -> None:
    sender = FakeSender()
    notify(db, sender, push(), dedupe_key="k", kind="birthday", quiet=QUIET, now=NOON)
    assert (
        notify(db, sender, push("again"), dedupe_key="k", kind="birthday", quiet=QUIET, now=NOON)
        is None
    )
    assert len(sender.sent) == 1
    assert len(db.scalars(select(Notification)).all()) == 1


def test_quiet_hours_queue_until_morning(db: Session) -> None:
    sender = FakeSender()
    notification = notify(
        db, sender, push(), dedupe_key="k", kind="birthday", quiet=QUIET, now=LATE
    )
    assert notification is not None
    assert not notification.delivered and notification.not_before == MORNING
    assert sender.sent == []

    assert flush_due(db, sender, quiet=QUIET, now=LATE + timedelta(hours=2)) == 0
    assert sender.sent == []
    assert flush_due(db, sender, quiet=QUIET, now=MORNING) == 1
    assert notification.delivered and notification.sent_at == MORNING
    assert flush_due(db, sender, quiet=QUIET, now=MORNING + timedelta(minutes=1)) == 0
    assert len(sender.sent) == 1


@pytest.mark.parametrize("kind", ["meeting_brief", "meeting_capture"])
def test_briefs_and_capture_prompts_ignore_quiet_hours(db: Session, kind: str) -> None:
    sender = FakeSender()
    notification = notify(db, sender, push(), dedupe_key="m", kind=kind, quiet=QUIET, now=LATE)
    assert notification is not None and notification.delivered
    assert len(sender.sent) == 1


def test_failed_send_is_retried_later(db: Session) -> None:
    sender = FakeSender(fail=True)
    notification = notify(
        db, sender, push(), dedupe_key="k", kind="birthday", quiet=QUIET, now=NOON
    )
    assert notification is not None
    assert not notification.delivered
    assert notification.last_error == "ntfy answered 500"
    assert notification.not_before > NOON

    sender.fail = False
    assert flush_due(db, sender, quiet=QUIET, now=NOON + timedelta(minutes=1)) == 0  # not yet
    assert flush_due(db, sender, quiet=QUIET, now=NOON + timedelta(minutes=10)) == 1
    assert notification.delivered and notification.attempts == 2
    assert notification.last_error is None


def test_gives_up_after_max_attempts(db: Session) -> None:
    sender = FakeSender(fail=True)
    notify(db, sender, push(), dedupe_key="k", kind="birthday", quiet=QUIET, now=NOON)
    now = NOON
    for _ in range(MAX_ATTEMPTS + 3):
        now += timedelta(minutes=10)
        flush_due(db, sender, quiet=QUIET, now=now)
    notification = db.scalars(select(Notification)).one()
    assert notification.attempts == MAX_ATTEMPTS


def test_stale_pushes_are_dropped(db: Session) -> None:
    notify(db, None, push(), dedupe_key="k", kind="birthday", quiet=QUIET, now=NOON)
    sender = FakeSender()
    assert flush_due(db, sender, quiet=QUIET, now=NOON + timedelta(days=2)) == 0
    assert sender.sent == []


def test_without_ntfy_pushes_are_recorded_and_sent_once_configured(db: Session) -> None:
    notification = notify(db, None, push(), dedupe_key="k", kind="birthday", quiet=QUIET, now=NOON)
    assert notification is not None and not notification.delivered
    assert notification.last_error is not None and "NTFY_TOPIC" in notification.last_error
    assert flush_due(db, None, quiet=QUIET, now=NOON) == 0
    sender = FakeSender()
    assert flush_due(db, sender, quiet=QUIET, now=NOON + timedelta(minutes=1)) == 1
