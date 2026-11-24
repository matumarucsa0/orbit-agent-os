"""Ask Claude about a capture after the request has already answered (SPEC §2.33).

`capture(defer=True)` saves the text as a note draft at once. This module then gets Claude's
answer and replaces the draft: through the worker's job queue (`JobClaudeClient.defer_capture`,
applied by `apply_late`), or, with the API client, in a background task that asks and replaces.
Either way a push "📥 Capture ready" says when.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from orbit.integrations.claude import CaptureExtractor, ClaudeError
from orbit.integrations.ntfy import PushMessage, PushSender
from orbit.services import capture, notifications
from orbit.services.capture import PendingCapture
from orbit.services.claude_jobs import JobClaudeClient

log = logging.getLogger(__name__)


def answer_in_background(
    sessions: sessionmaker[Session],
    extractor: CaptureExtractor,
    pending: PendingCapture,
    *,
    sender: PushSender | None,
    base_url: str,
    quiet: notifications.QuietHours,
) -> None:
    """Ask Claude (this can take a while), then replace the draft and push. Never raises."""
    text = pending.request.text
    try:
        extraction = extractor.extract_capture(pending.request)
    except ClaudeError as exc:
        log.warning("deferred capture got no answer: %s", exc)
        _give_up(sessions, pending)
        return
    except Exception:  # a background task has nobody to tell; the draft just stays a note
        log.exception("deferred capture failed")
        _give_up(sessions, pending)
        return
    with sessions() as session:
        outcome = capture.upgrade_draft(
            session,
            extraction,
            text=text,
            since=pending.since,
            goal_ids=pending.goal_ids,
            default_currency=pending.default_currency,
            tz=pending.tz,
        )
        if outcome is not None and outcome.outcome == "applied":
            notifications.notify(
                session,
                sender,
                PushMessage(
                    title="📥 Capture ready",
                    body=outcome.summary or "Your dictation is in the Inbox.",
                    click=f"{base_url}/inbox",
                    tags=("inbox_tray",),
                ),
                dedupe_key=f"late_capture:api:{pending.since.isoformat()}:{hashlib.sha1(text.encode()).hexdigest()[:12]}",
                kind="late_capture",
                quiet=quiet,
            )
        elif outcome is None:
            capture.give_up(session, text=text, since=pending.since)
        session.commit()


def _give_up(sessions: sessionmaker[Session], pending: PendingCapture) -> None:
    with sessions() as session:
        capture.give_up(session, text=pending.request.text, since=pending.since)
        session.commit()


def dispatch(
    extractor: CaptureExtractor,
    pending: PendingCapture,
    *,
    sessions: sessionmaker[Session],
    sender: PushSender | None,
    base_url: str,
    quiet: notifications.QuietHours,
    run_later: Callable[..., Any],
) -> None:
    """Start the answer. The worker queue takes it at once; the API client answers through
    `run_later` (a background task: it runs after the response has gone out)."""
    if isinstance(extractor, JobClaudeClient):
        try:
            extractor.defer_capture(pending.request)
        except ClaudeError as exc:
            log.warning("deferred capture was not queued: %s", exc)
            _give_up(sessions, pending)
        return
    run_later(
        answer_in_background,
        sessions,
        extractor,
        pending,
        sender=sender,
        base_url=base_url,
        quiet=quiet,
    )
