"""The weekly review agent (SPEC §2.17): context in, a review and Inbox proposals out.

It collects two context snapshots (the week just lived and the week ahead), asks Claude through
the `WeeklyReviewer` interface, validates the answer against what Claude was shown, stores the
review (read-only, L1) and writes proposals (L2). It never writes to a domain table.

One review per planned week: the row is inserted first (`claim`) and the caller commits before
Claude is asked, so two runs of the same week cannot both write one. A failure marks the row
`failed`; the job retries up to `MAX_ATTEMPTS`. A ready review is final for its week.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbit.domain.base import utcnow
from orbit.domain.enums import ProposalKind, ProposalStatus, ReviewStatus
from orbit.domain.goals import Goal
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Promise
from orbit.domain.normalize import normalize_text
from orbit.domain.people import Person
from orbit.domain.review import WeeklyReview
from orbit.integrations.claude import (
    ClaudeError,
    ReviewMemory,
    ReviewOutput,
    ReviewRequest,
    WeeklyReviewer,
)
from orbit.integrations.ntfy import PushMessage, PushSender, view_action
from orbit.integrations.prompts_registry import prompt_version
from orbit.services import notifications
from orbit.services.context import ContextFocus, ContextSnapshot, ContextWindow, build_context
from orbit.services.context.signals import compute_signals
from orbit.services.errors import InvalidInputError, NotFoundError
from orbit.services.goals import GoalLinkPayload, GoalRerankPayload
from orbit.services.promises import FollowUpPayload, PromiseDuePayload
from orbit.settings import Settings

log = logging.getLogger(__name__)

SOURCE_TYPE = "weekly_review"
PUSH_KIND = "weekly_review"
REVIEW_FROM = time(19, 0)  # Sunday, local time: after the 18:00 digest
CATCH_UP_WEEKDAY = 2  # a missed review is still made until Wednesday (Monday is 0)
MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(minutes=15)  # a "pending" row older than this was left by a stopped server
MAX_PRIORITIES = 7
MAX_PROPOSALS = 15
CONTEXT_PENDING = 100  # pending proposals shown to Claude
CONTEXT_REJECTED = 50
REJECTED_DAYS = 30


# --- Which week, and when ---


@dataclass(frozen=True)
class Weeks:
    review_start: date  # Monday of the week reviewed
    review_end: date  # its Sunday
    plan_start: date  # Monday of the week planned
    plan_end: date


def weeks_around(day: date) -> Weeks:
    """The week `day` is in is reviewed, the next one planned."""
    monday = day - timedelta(days=day.weekday())
    return Weeks(
        monday, monday + timedelta(days=6), monday + timedelta(days=7), monday + timedelta(days=13)
    )


@dataclass(frozen=True)
class ReviewTiming:
    """When a review is due: from `weekday` at `at` (local) the next week; until `catch_up_weekday`
    the week that just began. Defaults: Sunday 19:00, caught up until Wednesday."""

    weekday: int = 6
    at: time = REVIEW_FROM
    catch_up_weekday: int = CATCH_UP_WEEKDAY


def timing_from(settings: Settings) -> ReviewTiming:
    return ReviewTiming(
        settings.weekly_review_weekday,
        settings.weekly_review_time,
        settings.weekly_review_catch_up_weekday,
    )


def due_plan_start(now: datetime, tz: ZoneInfo, timing: ReviewTiming | None = None) -> date | None:
    """The planned week that should have a review by now: from the start moment (Sunday 19:00 by
    default) the next week; until the catch-up weekday the week that just began (a review the
    server missed). None the rest of the time."""
    timing = timing or ReviewTiming()
    local = now.astimezone(tz)
    monday = local.date() - timedelta(days=local.date().weekday())
    start = datetime.combine(monday + timedelta(days=timing.weekday), timing.at, tz)
    if local >= start:
        return monday + timedelta(days=7)
    if local.date().weekday() <= timing.catch_up_weekday:
        return monday
    return None


# --- Queries ---


def get_review(session: Session, review_id: int) -> WeeklyReview:
    review = session.get(WeeklyReview, review_id)
    if review is None:
        raise NotFoundError(f"Review {review_id} not found.")
    return review


def list_reviews(session: Session, limit: int = 50) -> list[WeeklyReview]:
    return list(
        session.scalars(select(WeeklyReview).order_by(WeeklyReview.plan_start.desc()).limit(limit))
    )


def latest_ready(session: Session, before: date | None = None) -> WeeklyReview | None:
    query = select(WeeklyReview).where(WeeklyReview.status == ReviewStatus.READY)
    if before is not None:
        query = query.where(WeeklyReview.plan_start < before)
    return session.scalars(query.order_by(WeeklyReview.plan_start.desc()).limit(1)).first()


def review_proposals(session: Session, review: WeeklyReview) -> list[Proposal]:
    return list(
        session.scalars(
            select(Proposal)
            .where(Proposal.source_type == SOURCE_TYPE, Proposal.source_id == str(review.id))
            .order_by(Proposal.id)
        )
    )


# --- Claiming a week ---


def _open_for_generation(review: WeeklyReview, now: datetime, retry: bool) -> bool:
    if review.status is ReviewStatus.READY:
        return False
    if review.status is ReviewStatus.PENDING:
        return now - review.updated_at >= STALE_AFTER  # running now, or left by a stopped server
    return retry or review.attempts < MAX_ATTEMPTS


def claim(
    session: Session, plan_start: date, now: datetime | None = None, *, retry: bool = False
) -> WeeklyReview | None:
    """Take the planned week for generation, or None when it is not up for it: already ready, being
    generated right now, or failed too often (unless `retry`). The caller commits before asking
    Claude. Making the row is what stops a second run."""
    moment = now or utcnow()
    review = session.scalars(
        select(WeeklyReview).where(WeeklyReview.plan_start == plan_start)
    ).first()
    if review is None:
        weeks = weeks_around(plan_start - timedelta(days=7))
        review = WeeklyReview(
            plan_start=weeks.plan_start,
            plan_end=weeks.plan_end,
            review_start=weeks.review_start,
            review_end=weeks.review_end,
            status=ReviewStatus.PENDING,
            attempts=0,
            input_tokens=0,
            output_tokens=0,
        )
        try:
            with session.begin_nested():
                session.add(review)
        except IntegrityError:
            return None  # another run made it first
    elif not _open_for_generation(review, moment, retry):
        return None
    if retry:
        review.attempts = 0
    review.status = ReviewStatus.PENDING
    review.attempts += 1
    review.error = None
    review.updated_at = moment
    session.flush()
    return review


# --- What Claude is given ---


# What depends on the reviewed week: who he met, the timetable, the calendar. Everything else
# (goals, money, promises) is true now, so the planned week's snapshot carries it once.
LAST_WEEK_MODULES = ("people", "meetings", "school", "fitness", "health")


def _snapshot(
    session: Session,
    start: date,
    end: date,
    tz: ZoneInfo,
    budget_chars: int,
    now: datetime,
    only: tuple[str, ...] | None = None,
) -> ContextSnapshot:
    focus = ContextFocus(activity_only=True) if only is not None else None
    return build_context(
        session,
        ContextWindow(start, end, tz),
        focus,
        budget_chars=budget_chars,
        now=now,
        only=only,
    )


def _proposal_lines(
    session: Session, status: ProposalStatus, since: datetime | None, limit: int
) -> list[str]:
    query = select(Proposal.summary).where(Proposal.status == status)
    if since is not None:
        query = query.where(Proposal.resolved_at >= since)
    return [text[:300] for text in session.scalars(query.order_by(Proposal.id.desc()).limit(limit))]


def build_request(
    session: Session, review: WeeklyReview, *, tz: ZoneInfo, budget_chars: int, now: datetime
) -> tuple[ReviewRequest, dict[str, Any]]:
    """The request for Claude, and the same data as JSON to keep with the review."""
    last = _snapshot(
        session, review.review_start, review.review_end, tz, budget_chars, now, LAST_WEEK_MODULES
    )
    ahead = _snapshot(session, review.plan_start, review.plan_end, tz, budget_chars, now)
    today = now.astimezone(tz).date()
    signals = compute_signals(
        session,
        review=ContextWindow(review.review_start, review.review_end, tz),
        plan=ContextWindow(review.plan_start, review.plan_end, tz),
        today=today,
        tz=tz,
    )
    previous_row = latest_ready(session, before=review.plan_start)
    memory = None
    if previous_row is not None and previous_row.output_json:
        out = previous_row.output_json
        memory = ReviewMemory(
            week=f"{previous_row.plan_start} to {previous_row.plan_end}",
            summary=str(out.get("summary", "")),
            priorities=tuple(str(p.get("text", "")) for p in out.get("priorities", [])),
        )
    pending = _proposal_lines(session, ProposalStatus.PENDING, None, CONTEXT_PENDING)
    rejected = _proposal_lines(
        session, ProposalStatus.REJECTED, now - timedelta(days=REJECTED_DAYS), CONTEXT_REJECTED
    )
    request = ReviewRequest(
        today=now.astimezone(tz).date(),
        review_week=(review.review_start, review.review_end),
        plan_week=(review.plan_start, review.plan_end),
        last_week_json=last.prompt_json(),
        next_week_json=ahead.prompt_json(),
        signals_json=signals.model_dump_json(),
        previous=memory,
        pending=tuple(pending),
        rejected=tuple(rejected),
    )
    kept = {
        "today": request.today.isoformat(),
        "last_week": last.model_dump(mode="json"),
        "next_week": ahead.model_dump(mode="json"),
        "signals": signals.model_dump(mode="json"),
        "previous": None
        if memory is None
        else {
            "week": memory.week,
            "summary": memory.summary,
            "priorities": list(memory.priorities),
        },
        "pending": pending,
        "rejected": rejected,
    }
    return request, kept


# --- Checking the answer against what Claude was shown ---


@dataclass
class Known:
    """The ids that were in the snapshots Claude saw."""

    goals: set[int]
    active_order: list[int]  # active goals, most important first
    people: set[int]
    meetings: set[int]
    promises: set[int]  # open ones


def known_ids(*snapshots: ContextSnapshot) -> Known:
    known = Known(set(), [], set(), set(), set())
    for snap in snapshots:
        for g in snap.goals.goals:
            known.goals.add(g.id)
            known.people.update(p.id for p in g.people)
            known.promises.update(p.id for p in g.open_promises)
            known.people.update(p.person_id for p in g.open_promises)
        if not known.active_order:
            known.active_order = [g.id for g in snap.goals.goals]
        known.people.update(p.id for p in snap.people.people)
        known.people.update(d.person_id for d in snap.ledger.debts)
        for m in snap.meetings.meetings:
            known.meetings.add(m.id)
            known.people.update(p.id for p in m.people)
        known.promises.update(p.id for p in snap.promises.promises)
        known.people.update(p.person_id for p in snap.promises.promises)
    return known


def sanitize(
    output: ReviewOutput, known: Known
) -> tuple[ReviewOutput, list[tuple[ProposalKind, dict[str, Any]]]]:
    """Drop what points at nothing Claude was shown, cap priorities and proposals. Returns the
    cleaned review and the proposals (kind, payload) in the order they will be created."""

    def keep(ids: list[int], pool: set[int]) -> list[int]:
        return list(dict.fromkeys(i for i in ids if i in pool))

    for item in (*output.wins, *output.slips):
        item.goal_ids = keep(item.goal_ids, known.goals)
        item.person_ids = keep(item.person_ids, known.people)
    for conflict in output.conflicts:
        conflict.goal_ids = keep(conflict.goal_ids, known.goals)
        conflict.person_ids = keep(conflict.person_ids, known.people)
        conflict.meeting_ids = keep(conflict.meeting_ids, known.meetings)
        conflict.promise_ids = keep(conflict.promise_ids, known.promises)
    output.priorities = output.priorities[:MAX_PRIORITIES]
    for priority in output.priorities:
        if priority.goal_id is not None and priority.goal_id not in known.goals:
            priority.goal_id = None

    ideas = output.proposals
    found: list[tuple[ProposalKind, dict[str, Any]]] = []
    rerank = ideas.goal_rerank
    if (
        rerank is not None
        and len(rerank.goal_ids) == len(known.active_order)
        and set(rerank.goal_ids) == set(known.active_order)
        and rerank.goal_ids != known.active_order
    ):
        found.append(
            (
                ProposalKind.GOAL_RERANK,
                GoalRerankPayload(**rerank.model_dump()).model_dump(mode="json"),
            )
        )
    for due in ideas.promise_dues:
        if due.promise_id in known.promises:
            found.append(
                (
                    ProposalKind.PROMISE_DUE,
                    PromiseDuePayload(**due.model_dump()).model_dump(mode="json"),
                )
            )
    for follow in ideas.follow_ups:
        if follow.person_id in known.people and follow.text.strip():
            data = follow.model_dump()
            if data["goal_id"] not in known.goals:
                data["goal_id"] = None
            payload = FollowUpPayload(**data)
            found.append((ProposalKind.FOLLOW_UP, payload.model_dump(mode="json")))
    for link in ideas.goal_links:
        if link.goal_id not in known.goals:
            continue
        if (link.promise_id is None) == (link.person_id is None):
            continue
        if link.promise_id is not None and link.promise_id not in known.promises:
            continue
        if link.person_id is not None and link.person_id not in known.people:
            continue
        found.append(
            (ProposalKind.GOAL_LINK, GoalLinkPayload(**link.model_dump()).model_dump(mode="json"))
        )
    return output, found[:MAX_PROPOSALS]


# --- Writing the proposals ---


def _already_waiting(session: Session, kind: ProposalKind, payload: dict[str, Any]) -> bool:
    """A pending proposal that asks for the same thing."""
    for other in session.scalars(
        select(Proposal).where(Proposal.kind == kind, Proposal.status == ProposalStatus.PENDING)
    ):
        old = other.payload_json
        if kind is ProposalKind.GOAL_RERANK:
            return True
        if kind is ProposalKind.PROMISE_DUE and old.get("promise_id") == payload["promise_id"]:
            return True
        if (
            kind is ProposalKind.FOLLOW_UP
            and old.get("person_id") == payload["person_id"]
            and normalize_text(old.get("text", "")) == normalize_text(payload["text"])
        ):
            return True
        if kind is ProposalKind.GOAL_LINK and all(
            old.get(k) == payload.get(k) for k in ("goal_id", "promise_id", "person_id")
        ):
            return True
    return False


def _summary(session: Session, kind: ProposalKind, payload: dict[str, Any]) -> str:
    def reason() -> str:
        return f" Why: {payload['reason']}" if payload.get("reason") else ""

    def name(person_id: int) -> str:
        person = session.get(Person, person_id)
        return person.display_name if person else f"person #{person_id}"

    def title(goal_id: int) -> str:
        goal = session.get(Goal, goal_id)
        return goal.title if goal else f"goal #{goal_id}"

    if kind is ProposalKind.FOLLOW_UP:
        due = f" by {date.fromisoformat(payload['due']):%-d %b}" if payload.get("due") else ""
        goal = f" (serves “{title(payload['goal_id'])}”)" if payload.get("goal_id") else ""
        return (
            f"Follow up with {name(payload['person_id'])}: {payload['text']}{due}{goal}.{reason()}"
        )
    if kind is ProposalKind.PROMISE_DUE:
        promise = session.get(Promise, payload["promise_id"])
        what = f"“{promise.text}” ({name(promise.person_id)})" if promise else "a promise"
        return f"Move {what} to {date.fromisoformat(payload['due']):%-d %b}.{reason()}"
    if kind is ProposalKind.GOAL_RERANK:
        order = "; ".join(f"{i}. {title(g)}" for i, g in enumerate(payload["goal_ids"], start=1))
        return f"New goal order: {order}.{reason()}"
    if payload.get("promise_id") is not None:
        promise = session.get(Promise, payload["promise_id"])
        what = f"“{promise.text}”" if promise else "a promise"
        return f"{what} serves “{title(payload['goal_id'])}”.{reason()}"
    return f"Link {name(payload['person_id'])} to “{title(payload['goal_id'])}”.{reason()}"


def create_proposals(
    session: Session, review: WeeklyReview, found: list[tuple[ProposalKind, dict[str, Any]]]
) -> list[Proposal]:
    made: list[Proposal] = []
    for kind, payload in found:
        if _already_waiting(session, kind, payload):
            continue
        proposal = Proposal(
            kind=kind,
            status=ProposalStatus.PENDING,
            summary=_summary(session, kind, payload),
            payload_json=payload,
            confidence=None,
            source_type=SOURCE_TYPE,
            source_id=str(review.id),
            prompt_version=prompt_version("weekly_review"),
        )
        session.add(proposal)
        made.append(proposal)
    session.flush()
    return made


# --- Generating ---


def prepare(
    session: Session,
    review: WeeklyReview,
    *,
    tz: ZoneInfo,
    budget_chars: int,
    now: datetime | None = None,
) -> ReviewRequest:
    """Build what Claude is given and keep it on the review (so any claim can be checked, and so
    `finish` checks the answer against exactly this). The caller commits."""
    request, kept = build_request(
        session, review, tz=tz, budget_chars=budget_chars, now=now or utcnow()
    )
    review.input_json = kept
    review.input_chars = (
        len(request.last_week_json) + len(request.next_week_json) + len(request.signals_json)
    )
    session.flush()
    return request


def finish(
    session: Session,
    review: WeeklyReview,
    output: ReviewOutput,
    *,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> WeeklyReview:
    """Check Claude's answer against what it was shown, store it, write the proposals. Works from
    the input kept by `prepare`, so it also serves an answer that arrives later from outside.
    The caller commits."""
    kept = review.input_json
    if not kept:
        raise InvalidInputError("This review has no stored input to check the answer against.")
    last = ContextSnapshot.model_validate(kept["last_week"])
    ahead = ContextSnapshot.model_validate(kept["next_week"])
    cleaned, found = sanitize(output, known_ids(last, ahead))
    review.output_json = cleaned.model_dump(mode="json")
    review.model = model
    review.input_tokens = input_tokens
    review.output_tokens = output_tokens
    review.status = ReviewStatus.READY
    review.error = None
    session.flush()
    create_proposals(session, review, found)
    return review


def generate(
    session: Session,
    review: WeeklyReview,
    reviewer: WeeklyReviewer,
    *,
    tz: ZoneInfo,
    budget_chars: int,
    now: datetime | None = None,
) -> WeeklyReview:
    """Ask Claude through the API and store the answer, or mark the review failed. Never raises
    for a Claude error. The caller commits."""
    request = prepare(session, review, tz=tz, budget_chars=budget_chars, now=now)
    try:
        answer = reviewer.weekly_review(request)
    except ClaudeError as exc:
        review.status = ReviewStatus.FAILED
        review.error = str(exc)
        session.flush()
        log.warning("weekly review for %s failed: %s", review.plan_start, exc)
        return review
    return finish(
        session,
        review,
        answer.output,
        model=answer.model,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
    )


def run_now(
    session: Session,
    reviewer: WeeklyReviewer,
    sender: PushSender | None,
    *,
    tz: ZoneInfo,
    budget_chars: int,
    base_url: str,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> WeeklyReview:
    """Generate the review for the current week now (the "Generate now" button and the API).
    Unlike the other functions it commits: the claim must be stored before Claude is asked.
    Retries a failed week; refuses one that is ready or being made."""
    moment = now or utcnow()
    plan_start = weeks_around(moment.astimezone(tz).date()).plan_start
    claimed = claim(session, plan_start, moment, retry=True)
    if claimed is None:
        raise InvalidInputError("This week's review is already made, or being made right now.")
    session.commit()
    generate(session, claimed, reviewer, tz=tz, budget_chars=budget_chars, now=moment)
    session.commit()
    send_push(session, claimed, sender, base_url=base_url, quiet=quiet, now=moment)
    session.commit()
    return claimed


def fail(session: Session, review_id: int, message: str) -> None:
    """Mark a review failed after an unexpected error (a bug, not a Claude answer)."""
    review = session.get(WeeklyReview, review_id)
    if review is not None and review.status is not ReviewStatus.READY:
        review.status = ReviewStatus.FAILED
        review.error = message[:1000]
        session.flush()


# --- The push ---


def push_message(session: Session, review: WeeklyReview, base_url: str) -> PushMessage:
    summary = str((review.output_json or {}).get("summary", "")).strip()
    waiting = sum(
        1 for p in review_proposals(session, review) if p.status is ProposalStatus.PENDING
    )
    parts = [summary] if summary else []
    if waiting:
        parts.append(f"{waiting} {'proposal' if waiting == 1 else 'proposals'} in your Inbox.")
    url = f"{base_url}/reviews/{review.id}"
    return PushMessage(
        title="🗓 Weekly review ready",
        body="\n\n".join(parts) or "Your weekly review is ready.",
        click=url,
        tags=("compass",),
        actions=(view_action("Open", url),),
    )


def send_push(
    session: Session,
    review: WeeklyReview,
    sender: PushSender | None,
    *,
    base_url: str,
    quiet: notifications.QuietHours,
    now: datetime | None = None,
) -> bool:
    """One push per planned week (the dedupe key). True when it was recorded now."""
    if review.status is not ReviewStatus.READY:
        return False
    record = notifications.notify(
        session,
        sender,
        push_message(session, review, base_url),
        dedupe_key=f"weekly_review:{review.plan_start.isoformat()}",
        kind=PUSH_KIND,
        quiet=quiet,
        now=now,
    )
    if record is None:
        return False
    review.pushed_at = now or utcnow()
    session.flush()
    return True
