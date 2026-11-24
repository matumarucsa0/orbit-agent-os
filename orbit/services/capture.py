"""Quick capture: dictated text -> fuzzy pre-match of people -> Claude -> Inbox proposals.

Nothing is written to people, notes, debts or promises here; `apply_capture` does that when I
confirm the proposal (called by the Inbox service). Callers commit.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from orbit.domain.enums import (
    DebtDirection,
    DebtType,
    InteractionKind,
    PromiseDirection,
    PromiseStatus,
    ProposalKind,
    ProposalStatus,
)
from orbit.domain.goals import Goal
from orbit.domain.inbox import Proposal
from orbit.domain.ledger import Debt, Promise
from orbit.domain.meetings import Meeting, MeetingPerson
from orbit.domain.money import format_amount, is_currency, parse_amount
from orbit.domain.normalize import normalize_text
from orbit.domain.people import Handle, Interaction, Person, Tag, interaction_person, person_tag
from orbit.domain.school import Course
from orbit.integrations.claude import (
    CandidateCourse,
    CandidateDebt,
    CandidateGoal,
    CandidatePerson,
    CandidatePromise,
    CaptureExtraction,
    CaptureExtractor,
    CaptureRequest,
    ClaudeError,
    SchoolDeadlineIdea,
)
from orbit.integrations.prompts_registry import prompt_version
from orbit.services import (
    corrections,
    goals,
    interactions,
    ledger,
    matching,
    meetings,
    people,
    promises,
    school,
    school_reader,
)
from orbit.services.errors import InvalidInputError
from orbit.services.proposal_kinds import Applied, ApplyContext, ProposalHandler

log = logging.getLogger(__name__)

SOURCE_TYPE = "capture"
MAX_CANDIDATES = 25
RECENT_DAYS = 14  # people met or talked to this recently are offered to Claude (SPEC §2.27)
MAX_OPEN_PER_PERSON = 10  # open debts and promises shown to Claude, per person
MAX_OPEN_TOTAL = 40  # ... and in all
MAX_GOALS = 50  # active goals shown to Claude
MAX_COURSES = 40  # this semester's courses shown to Claude
# The same text again this soon (a double tap, a Shortcut retry) returns the first capture.
DUPLICATE_WINDOW = timedelta(minutes=5)
DEFAULT_TZ = ZoneInfo("Europe/Prague")

# --- Payload (stored on the proposal, editable before confirming) ---

# "12" = person 12; "new:0" = new_people[0]; None = not assigned yet (must be picked to confirm).
Ref = Annotated[str, StringConstraints(pattern=r"^(\d+|new:\d+)$")]


class _Draft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    include: bool = True


class NewPersonDraft(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    context: str | None = None
    # create: a new person on confirm; link: it's actually `person_id`; skip: drop their items.
    action: Literal["create", "link", "skip"] = "create"
    person_id: int | None = None


class NoteDraft(_Draft):
    person: Ref | None
    text: str


class InteractionDraft(_Draft):
    people: list[Ref]
    kind: InteractionKind = InteractionKind.MEET
    summary: str | None = None
    occurred_on: date | None = None  # None: the day it's confirmed
    # The interaction of the meeting the capture follows (at most one per meeting).
    meeting: bool = False


class DebtDraft(_Draft):
    person: Ref | None
    direction: DebtDirection
    type: DebtType
    amount_minor: int | None = Field(default=None, gt=0)  # None on money: "amount missing"
    currency: str | None = None
    description: str | None = None

    @property
    def amount_missing(self) -> bool:
        return self.type is DebtType.MONEY and self.amount_minor is None


class PromiseDraft(_Draft):
    person: Ref | None
    direction: PromiseDirection
    text: str
    due: date | None = None
    goal: int | None = None  # the goal it serves (an active goal's id)


class AppointmentDraft(_Draft):
    """A future appointment (SPEC §2.23). `person` is optional: "dentist Monday 10:00"."""

    person: Ref | None = None
    title: str
    start: datetime  # local (Europe/Prague) wall time, no zone
    end: datetime | None = None  # None: one hour
    location: str | None = None


class SettleDraft(_Draft):
    """An existing open debt that was paid back, returned or done (SPEC §2.27). `label` is the
    debt as it looked when the draft was made, so the card can say what it settles."""

    person: Ref | None
    debt_id: int
    amount_minor: int | None = Field(default=None, gt=0)  # money part paid; None: all of it
    currency: str | None = None
    label: str = ""


class PromiseUpdateDraft(_Draft):
    """An existing open promise that was kept or dropped (SPEC §2.27)."""

    person: Ref | None
    promise_id: int
    status: Literal["done", "dropped"] = "done"
    label: str = ""


Item = (
    NoteDraft
    | InteractionDraft
    | DebtDraft
    | PromiseDraft
    | AppointmentDraft
    | SettleDraft
    | PromiseUpdateDraft
)


class CapturePayload(BaseModel):
    text: str
    # One token per dictation, shared by its `capture` and `new_person` proposals, so the Inbox
    # can reject them together. None on older rows.
    group: str | None = None
    # The meeting this was dictated after (the post-meeting push): notes, promises and the
    # meeting's interaction are linked to it.
    meeting_id: int | None = None
    # People pre-matched from the text: offered first in the edit form's person pickers.
    candidates: list[int] = Field(default_factory=list)
    new_people: list[NewPersonDraft] = Field(default_factory=list)
    notes: list[NoteDraft] = Field(default_factory=list)
    interactions: list[InteractionDraft] = Field(default_factory=list)
    debts: list[DebtDraft] = Field(default_factory=list)
    promises: list[PromiseDraft] = Field(default_factory=list)
    appointments: list[AppointmentDraft] = Field(default_factory=list)
    settles: list[SettleDraft] = Field(default_factory=list)
    promise_updates: list[PromiseUpdateDraft] = Field(default_factory=list)
    # Lines the card shows above the items, e.g. "No open debt of 20.00 € for Paťo".
    warnings: list[str] = Field(default_factory=list)
    # A note draft that Claude has not read yet; its answer replaces the draft (SPEC §2.33).
    processing: bool = False

    def items(self) -> list[Item]:
        return [
            *self.notes,
            *self.interactions,
            *self.debts,
            *self.promises,
            *self.appointments,
            *self.settles,
            *self.promise_updates,
        ]

    def included(self) -> list[Item]:
        return [item for item in self.items() if item.include]

    def referenced_ids(self) -> set[int]:
        ids = set(self.candidates)
        for item in self.items():
            for ref in _refs(item):
                if ref.isdigit():
                    ids.add(int(ref))
        ids.update(p.person_id for p in self.new_people if p.person_id is not None)
        return ids


SOURCE_TEXT_MAX = 2000


class NewPersonPayload(BaseModel):
    """A `new_person` proposal: someone a capture mentioned without any item about them."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    context: str | None = None
    # The dictation that mentioned them (as typed), so the card says what it is about; and the
    # dictation's group token. None on rows made before these fields existed.
    source_text: str | None = Field(default=None, max_length=SOURCE_TEXT_MAX)
    group: str | None = None


def _refs(item: Item) -> list[str]:
    if isinstance(item, InteractionDraft):
        return list(item.people)
    return [item.person] if item.person is not None else []


# --- Fuzzy pre-match ---


def match_people(session: Session, text: str, limit: int = MAX_CANDIDATES) -> list[Person]:
    """People the text probably mentions, best first: names/aliases (inflection- and
    diacritics-insensitive) and @handles."""
    found = matching.words(text)
    handles = {w[1:] for w in found if w.startswith("@") and len(w) > 1}
    plain = {w.lstrip("@") for w in found if len(w.lstrip("@")) >= 2}
    scores: dict[int, int] = {}
    everyone = session.scalars(
        select(Person).where(Person.archived.is_(False)).options(selectinload(Person.aliases))
    )
    by_id: dict[int, Person] = {}
    for person in everyone:
        by_id[person.id] = person
        tokens = matching.name_tokens(person)
        best = max((matching.word_score(w, t) for w in plain for t in tokens), default=0)
        if best:
            scores[person.id] = best
    if handles:
        for handle in session.scalars(select(Handle).where(Handle.value_norm.in_(handles))):
            if handle.person_id in by_id:
                scores[handle.person_id] = 4
    ranked = sorted(scores, key=lambda pid: (-scores[pid], by_id[pid].search_name_norm, pid))
    return [by_id[pid] for pid in ranked[:limit]]


# --- Open debts and promises Claude is shown, and what it may settle (SPEC §2.27) ---


@dataclass(frozen=True)
class OpenItems:
    """The open debts and promises of the candidate people that were put in the prompt. Claude may
    only settle or close what is in here."""

    debts: dict[int, Debt]
    promises: dict[int, Promise]
    by_person: dict[int, tuple[tuple[CandidateDebt, ...], tuple[CandidatePromise, ...]]]


def _debt_what(debt: Debt) -> str:
    if debt.type is DebtType.MONEY and debt.amount_minor is not None:
        return format_amount(debt.amount_minor, debt.currency or "")
    return debt.description or debt.type.value


def open_items(session: Session, candidates: list[Person]) -> OpenItems:
    """Oldest first, `MAX_OPEN_PER_PERSON` per person and `MAX_OPEN_TOTAL` of each kind in all."""
    ids = [p.id for p in candidates]
    debts: dict[int, Debt] = {}
    promises_: dict[int, Promise] = {}
    by_person: dict[int, tuple[tuple[CandidateDebt, ...], tuple[CandidatePromise, ...]]] = {}
    if not ids:
        return OpenItems(debts, promises_, by_person)
    open_debts = session.scalars(
        select(Debt)
        .where(Debt.person_id.in_(ids), Debt.settled_at.is_(None))
        .order_by(Debt.created_at, Debt.id)
    )
    open_promises = session.scalars(
        select(Promise)
        .where(Promise.person_id.in_(ids), Promise.status == PromiseStatus.OPEN)
        .order_by(Promise.due.is_(None), Promise.due, Promise.id)
    )
    per_debts: dict[int, list[CandidateDebt]] = {}
    for debt in open_debts:
        mine = per_debts.setdefault(debt.person_id, [])
        if len(mine) >= MAX_OPEN_PER_PERSON or len(debts) >= MAX_OPEN_TOTAL:
            continue
        debts[debt.id] = debt
        mine.append(
            CandidateDebt(
                id=debt.id,
                direction=debt.direction.value,
                kind=debt.type.value,
                what=_debt_what(debt),
                description=debt.description,
                reference_code=debt.reference_code,
                since=debt.created_at.date(),
                due=debt.due,
            )
        )
    per_promises: dict[int, list[CandidatePromise]] = {}
    for promise in open_promises:
        mine_p = per_promises.setdefault(promise.person_id, [])
        if len(mine_p) >= MAX_OPEN_PER_PERSON or len(promises_) >= MAX_OPEN_TOTAL:
            continue
        promises_[promise.id] = promise
        mine_p.append(
            CandidatePromise(
                id=promise.id, direction=promise.direction.value, text=promise.text, due=promise.due
            )
        )
    for pid in ids:
        by_person[pid] = (tuple(per_debts.get(pid, ())), tuple(per_promises.get(pid, ())))
    return OpenItems(debts, promises_, by_person)


def candidate_people(candidates: list[Person], shown: OpenItems) -> tuple[CandidatePerson, ...]:
    """The people shown to Claude, each with their open debts and promises (`shown`)."""
    return tuple(
        CandidatePerson(
            p.id,
            p.display_name,
            tuple(a.value for a in p.aliases),
            *shown.by_person.get(p.id, ((), ())),
        )
        for p in candidates
    )


def _ref_id(value: str, prefix: str) -> int | None:
    head, _, tail = value.strip().partition(":")
    return int(tail) if head == prefix and tail.isdigit() else None


def _settle_drafts(
    extraction: CaptureExtraction, shown: OpenItems | None
) -> tuple[list[SettleDraft], list[str]]:
    """Claude's settles, checked against what it was shown. A ref that was not listed, a debt in
    the other direction, or a part bigger than the debt drops the draft (the last one with a
    warning). Two settles of one debt keep the first; among identical open debts the oldest wins."""
    found: list[SettleDraft] = []
    warnings: list[str] = []
    if shown is None:
        return found, warnings
    taken: set[int] = set()
    for item in extraction.settles:
        debt_id = _ref_id(item.debt_ref, "debt")
        debt = shown.debts.get(debt_id) if debt_id is not None else None
        if debt is None or debt.direction is not item.direction:
            continue
        twins = [
            d
            for d in shown.debts.values()
            if d.person_id == debt.person_id
            and (d.direction, d.type, d.currency, d.amount_minor, d.description)
            == (debt.direction, debt.type, debt.currency, debt.amount_minor, debt.description)
            and d.id not in taken
        ]
        debt = min(twins, key=lambda d: (d.created_at, d.id), default=debt)
        if debt.id in taken:
            continue
        amount_minor: int | None = None
        if debt.type is DebtType.MONEY and item.amount:
            try:
                part = parse_amount(item.amount, debt.currency or "")
            except ValueError:
                part = 0
            if part > (debt.amount_minor or 0):
                name = debt.person.display_name
                warnings.append(
                    f"No open debt of {format_amount(part, debt.currency or '')} for {name}."
                )
                continue
            if 0 < part < (debt.amount_minor or 0):
                amount_minor = part
        taken.add(debt.id)
        found.append(
            SettleDraft(
                person=str(debt.person_id),
                debt_id=debt.id,
                amount_minor=amount_minor,
                currency=debt.currency,
                label=_debt_label(debt),
            )
        )
    return found, warnings


def _promise_update_drafts(
    extraction: CaptureExtraction, shown: OpenItems | None
) -> list[PromiseUpdateDraft]:
    found: list[PromiseUpdateDraft] = []
    if shown is None:
        return found
    taken: set[int] = set()
    for item in extraction.promise_updates:
        promise_id = _ref_id(item.promise_ref, "promise")
        promise = shown.promises.get(promise_id) if promise_id is not None else None
        if promise is None or promise.id in taken:
            continue
        taken.add(promise.id)
        found.append(
            PromiseUpdateDraft(
                person=str(promise.person_id),
                promise_id=promise.id,
                status=item.status,
                label=_promise_label(promise),
            )
        )
    return found


def _debt_label(debt: Debt) -> str:
    who = debt.person.display_name
    mine = debt.direction is DebtDirection.THEY_OWE_ME
    if debt.type is DebtType.ITEM:
        if mine:
            return f"{who} has your {debt.description}"
        return f"You have {who}'s {debt.description}"
    if debt.type is DebtType.FAVOR:
        head = f"{who} owes you a favor" if mine else f"You owe {who} a favor"
        return f"{head}: {debt.description}"
    what = _debt_what(debt)
    line = f"{who} owes you {what}" if mine else f"You owe {who} {what}"
    return f"{line} ({debt.description})" if debt.description else line


def _promise_label(promise: Promise) -> str:
    who = promise.person.display_name
    head = (
        f"You promised {who}"
        if promise.direction is PromiseDirection.I_PROMISED
        else f"{who} promised you"
    )
    return f"{head}: {promise.text}"


# --- Who Claude is told about: the text's names first, then people close or recent (SPEC §2.27) ---


def recall_people(
    session: Session, now: datetime, tz: ZoneInfo, exclude: Collection[int]
) -> list[Person]:
    """People worth offering even when the text names nobody: tagged `close`, talked to in the last
    `RECENT_DAYS` days, or in a meeting today. Most recent first, then by name."""
    today = now.astimezone(tz).date()
    day_start = datetime.combine(today, time.min, tz).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    recent: dict[int, date] = {}
    rows = session.execute(
        select(interaction_person.c.person_id, func.max(Interaction.occurred_on))
        .join(Interaction, Interaction.id == interaction_person.c.interaction_id)
        .where(Interaction.occurred_on >= today - timedelta(days=RECENT_DAYS))
        .group_by(interaction_person.c.person_id)
    )
    for person_id, last in rows:
        recent[person_id] = last
    for (person_id,) in session.execute(
        select(MeetingPerson.person_id)
        .join(Meeting, Meeting.id == MeetingPerson.meeting_id)
        .where(
            Meeting.cancelled.is_(False),
            Meeting.start_utc >= day_start,
            Meeting.start_utc < day_end,
        )
    ):
        recent[person_id] = today
    close = set(
        session.scalars(
            select(person_tag.c.person_id)
            .join(Tag, Tag.id == person_tag.c.tag_id)
            .where(Tag.name_norm == "close")
        )
    )
    wanted = (set(recent) | close) - set(exclude)
    if not wanted:
        return []
    found = session.scalars(
        select(Person)
        .where(Person.id.in_(wanted), Person.archived.is_(False))
        .options(selectinload(Person.aliases))
    )
    return sorted(
        found,
        key=lambda p: (
            -recent[p.id].toordinal() if p.id in recent else 0,
            p.search_name_norm,
            p.id,
        ),
    )


def school_deadline_proposals(
    session: Session,
    extraction: CaptureExtraction,
    courses: list[Course],
    *,
    text: str,
    today: date,
    tz: ZoneInfo,
) -> list[Proposal]:
    """`school_deadline` proposals for what Claude found (SPEC §2.29). A course code that was not
    listed is dropped (the deadline then has no course); past or known deadlines are left out."""
    by_code = {c.code.upper(): c for c in courses}
    found: list[Proposal] = []
    for item in extraction.school_deadlines:
        code = (item.course_code or "").strip().upper()
        idea = SchoolDeadlineIdea(
            title=item.title,
            kind=item.kind,
            due=item.due.strip(),
            all_day="T" not in item.due,
            points=None,
            quote=text,
        )
        proposal = school_reader.propose_dictated_deadline(
            session, idea, by_code.get(code), text=text, tz=tz, today=today
        )
        if proposal is not None and not any(p.summary == proposal.summary for p in found):
            found.append(proposal)
    return found


# --- Building proposals ---


@dataclass(frozen=True)
class PendingCapture:
    """A capture saved as a note draft while Claude has not read it yet (SPEC §2.33): what is
    needed to ask Claude later and to replace the draft with its answer."""

    request: CaptureRequest
    since: datetime  # when the draft was made: `upgrade_draft` looks for it from here
    goal_ids: tuple[int, ...]
    default_currency: str
    tz: ZoneInfo


@dataclass(frozen=True)
class CaptureResult:
    proposals: list[Proposal]
    summary: str  # short text for the Shortcut's notification
    pending: PendingCapture | None = None  # set when Claude was not asked yet (`defer=True`)

    @property
    def item_count(self) -> int:
        return sum(_proposal_items(p) for p in self.proposals)


def _proposal_items(proposal: Proposal) -> int:
    if proposal.kind is ProposalKind.CAPTURE:
        return len(CapturePayload.model_validate(proposal.payload_json).included())
    return 1


def _check_ref(ref: str, known: set[int], new_count: int) -> str | None:
    ref = ref.strip()
    if ref.isdigit() and int(ref) in known:
        return ref
    if ref.startswith("new:") and ref[4:].isdigit() and int(ref[4:]) < new_count:
        return ref
    return None


def _local_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def payload_from_extraction(
    extraction: CaptureExtraction,
    *,
    text: str,
    candidates: list[Person],
    default_currency: str,
    meeting_id: int | None = None,
    goal_ids: Collection[int] = (),
    shown: OpenItems | None = None,
) -> tuple[CapturePayload, list[NewPersonPayload]]:
    """Validate Claude's answer against what it was shown. Unknown refs become unassigned;
    unknown people nobody's item refers to become separate `new_person` proposals."""
    known = {p.id for p in candidates}
    count = len(extraction.unknown_people)

    def ref(value: str) -> str | None:
        return _check_ref(value, known, count)

    notes = [NoteDraft(person=ref(n.person_ref), text=n.text) for n in extraction.notes if n.text]
    found_interactions = []
    for i in extraction.interactions:
        refs = [r for r in (ref(v) for v in i.person_refs) if r is not None]
        found_interactions.append(
            InteractionDraft(
                people=list(dict.fromkeys(refs)),
                kind=i.kind,
                summary=i.summary or None,
                occurred_on=i.occurred_on,
            )
        )
    debts = []
    for d in extraction.debts:
        amount_minor = currency = None
        if d.type is DebtType.MONEY:
            currency = (d.currency or "").strip().upper()
            currency = currency if is_currency(currency) else default_currency
            try:
                amount_minor = parse_amount(d.amount, currency) if d.amount else None
            except ValueError:
                amount_minor = None
            if amount_minor is not None and amount_minor <= 0:
                amount_minor = None
        debts.append(
            DebtDraft(
                person=ref(d.person_ref),
                direction=d.direction,
                type=d.type,
                amount_minor=amount_minor,
                currency=currency,
                description=d.description or None,
            )
        )

    def goal(value: str | None) -> int | None:
        """Only an id from the list Claude was shown counts."""
        found = int(value) if value and value.strip().isdigit() else None
        return found if found in goal_ids else None

    found_promises = [
        PromiseDraft(
            person=ref(p.person_ref),
            direction=p.direction,
            text=p.text,
            due=p.due,
            goal=goal(p.goal_ref),
        )
        for p in extraction.promises
        if p.text
    ]
    found_appointments = []
    for a in extraction.appointments:
        start, end = _local_time(a.start), _local_time(a.end) if a.end else None
        if start is None or not a.title.strip():
            continue  # no day and time: the text stays a note
        found_appointments.append(
            AppointmentDraft(
                person=ref(a.person_ref) if a.person_ref else None,
                title=a.title,
                start=start,
                end=end if end is not None and end > start else None,
                location=a.location or None,
            )
        )
    found_settles, warnings = _settle_drafts(extraction, shown)
    found_updates = _promise_update_drafts(extraction, shown)
    if warnings and not notes:
        # A settle that matched nothing must not lose what was said: keep the text as a note.
        owner = next((s.person for s in found_settles if s.person), None)
        only = str(candidates[0].id) if len(candidates) == 1 else owner
        notes = [NoteDraft(person=only, text=text)]
    payload = CapturePayload(
        text=text,
        meeting_id=meeting_id,
        candidates=[p.id for p in candidates],
        notes=notes,
        interactions=found_interactions,
        debts=debts,
        promises=found_promises,
        appointments=found_appointments,
        settles=found_settles,
        promise_updates=found_updates,
        warnings=warnings,
    )

    # Keep only the unknown people some item points at; renumber their refs.
    used = sorted(
        {int(r[4:]) for item in payload.items() for r in _refs(item) if r.startswith("new:")}
    )
    renumber = {f"new:{old}": f"new:{new}" for new, old in enumerate(used)}
    payload.new_people = [
        NewPersonDraft(name=u.name, context=u.context or None)
        for u in (extraction.unknown_people[i] for i in used)
        if u.name.strip()
    ]
    for item in payload.items():
        if isinstance(item, InteractionDraft):
            item.people = [renumber.get(r, r) for r in item.people]
        elif item.person is not None:
            item.person = renumber.get(item.person, item.person)
    standalone = [
        NewPersonPayload(name=u.name, context=u.context or None)
        for i, u in enumerate(extraction.unknown_people)
        if i not in used and u.name.strip()
    ]
    return payload, standalone


def fallback_payload(
    text: str,
    candidates: list[Person],
    meeting_id: int | None = None,
    owner: Person | None = None,
) -> CapturePayload:
    """When Claude is unavailable or finds nothing: keep the text as a note draft, so nothing
    dictated is lost. The note is assigned to `owner` (the one person at the meeting it follows),
    else to the only matched person, if there's exactly one."""
    if owner is None and len(candidates) == 1:
        owner = candidates[0]
    person = str(owner.id) if owner is not None else None
    return CapturePayload(
        text=text,
        meeting_id=meeting_id,
        candidates=[p.id for p in candidates],
        notes=[NoteDraft(person=person, text=text)],
    )


def _names(session: Session, ids: Iterable[int]) -> dict[int, str]:
    wanted = set(ids)
    if not wanted:
        return {}
    rows = session.execute(select(Person.id, Person.display_name).where(Person.id.in_(wanted)))
    return {pid: name for pid, name in rows}


def ref_name(ref: str | None, payload: CapturePayload, names: dict[int, str]) -> str:
    """Display name for a person reference (`"<id>"` or `"new:<index>"`) on a capture card; None is
    "someone (pick a person)"."""
    if ref is None:
        return "someone (pick a person)"
    if ref.startswith("new:"):
        index = int(ref[4:])
        if index < len(payload.new_people):
            draft = payload.new_people[index]
            if draft.action == "link" and draft.person_id is not None:
                return names.get(draft.person_id, draft.name)
            return draft.name
        return "someone new"
    return names.get(int(ref), f"#{ref}")


def describe_item(
    item: Item,
    payload: CapturePayload,
    names: dict[int, str],
) -> str:
    """One readable line: "Paťo owes you 12.00 € (pizza)"."""
    if isinstance(item, NoteDraft):
        return f"Note on {ref_name(item.person, payload, names)}: {item.text}"
    if isinstance(item, InteractionDraft):
        who = ", ".join(ref_name(r, payload, names) for r in item.people) or "someone"
        summary = f": {item.summary}" if item.summary else ""
        return f"{item.kind.value.capitalize()} with {who}{summary}"
    if isinstance(item, SettleDraft):
        label = item.label or f"Debt #{item.debt_id}"
        if item.amount_minor is not None:
            part = format_amount(item.amount_minor, item.currency or "")
            return f"Settle: {label} · {part} paid, the rest stays open"
        return f"Settle: {label} · in full"
    if isinstance(item, PromiseUpdateDraft):
        label = item.label or f"Promise #{item.promise_id}"
        return f"Promise {item.status}: {label}"
    if isinstance(item, AppointmentDraft):
        when = f"{item.start:%a %-d %b %H:%M}"
        with_ = f" with {ref_name(item.person, payload, names)}" if item.person else ""
        return f"Appointment: {item.title}{with_}, {when}"
    if isinstance(item, PromiseDraft):
        who = ref_name(item.person, payload, names)
        head = (
            f"You promised {who}"
            if item.direction is PromiseDirection.I_PROMISED
            else f"{who} promised you"
        )
        return f"{head}: {item.text}"
    who = ref_name(item.person, payload, names)
    if item.type is DebtType.ITEM:
        if item.direction is DebtDirection.THEY_OWE_ME:
            return f"{who} has your {item.description}"
        return f"You have {who}'s {item.description}"
    if item.type is DebtType.MONEY:
        what = (
            format_amount(item.amount_minor, item.currency or "")
            if item.amount_minor is not None
            else "an amount (missing)"
        )
    else:
        what = "a favor"
    line = (
        f"{who} owes you {what}"
        if item.direction is DebtDirection.THEY_OWE_ME
        else f"You owe {who} {what}"
    )
    return f"{line} ({item.description})" if item.description else line


def describe(session: Session, payload: CapturePayload) -> list[str]:
    """One human-readable line per included item of a capture payload."""
    names = _names(session, payload.referenced_ids())
    return [describe_item(item, payload, names) for item in payload.included()]


def capture_summary(session: Session, payload: CapturePayload) -> str:
    """The one-line summary stored on the capture proposal."""
    return " · ".join(describe(session, payload)) or "Nothing selected"


def _shortcut_summary(session: Session, proposals: list[Proposal], note: str | None) -> str:
    lines: list[str] = []
    count = 0
    for proposal in proposals:
        if proposal.kind is ProposalKind.CAPTURE:
            payload = CapturePayload.model_validate(proposal.payload_json)
            lines += describe(session, payload)
            count += len(payload.included())
        elif proposal.kind is ProposalKind.NEW_PERSON:
            lines.append(f"New person? {proposal.payload_json['name']}")
            count += 1
        else:
            lines.append(proposal.summary)
            count += 1
    head = f"{count} {'item' if count == 1 else 'items'} waiting in Inbox"
    if note:
        head = f"{note} {head}"
    shown = "; ".join(lines[:3]) + ("; …" if len(lines) > 3 else "")
    return f"{head}: {shown}" if shown else head


def meeting_interaction(session: Session, meeting_id: int) -> Interaction | None:
    """The interaction already logged for a meeting, if any."""
    return session.scalars(
        select(Interaction).where(Interaction.meeting_id == meeting_id).limit(1)
    ).first()


def add_meeting_interaction(
    session: Session, payload: CapturePayload, meeting: Meeting, tz: ZoneInfo
) -> None:
    """Give the payload the meeting's `meet` interaction with everyone linked to it: Claude's own
    meet interaction for that day becomes it, else a new one titled like the meeting. Nothing is
    added when the meeting has nobody linked or already has its interaction."""
    met = [str(p.id) for p in meeting.people]
    if not met or meeting_interaction(session, meeting.id) is not None:
        return
    day = meeting.start_utc.astimezone(tz).date()
    for item in payload.interactions:
        if item.kind is InteractionKind.MEET and item.occurred_on in (None, day):
            item.people = list(dict.fromkeys([*met, *item.people]))
            item.occurred_on = day
            item.summary = item.summary or meeting.title
            item.meeting = True
            return
    payload.interactions.insert(
        0,
        InteractionDraft(
            people=met,
            kind=InteractionKind.MEET,
            summary=meeting.title,
            occurred_on=day,
            meeting=True,
        ),
    )


def _recent_duplicate(
    session: Session, text: str, meeting_id: int | None, now: datetime
) -> Proposal | None:
    """A pending capture of exactly this text created within DUPLICATE_WINDOW, if any."""
    source_id = str(meeting_id) if meeting_id is not None else None
    recent = session.scalars(
        select(Proposal)
        .where(
            Proposal.kind == ProposalKind.CAPTURE,
            Proposal.status == ProposalStatus.PENDING,
            Proposal.source_type == SOURCE_TYPE,
            Proposal.created_at >= now - DUPLICATE_WINDOW,
        )
        .order_by(Proposal.id.desc())
    )
    return next(
        (
            p
            for p in recent
            if p.source_id == source_id and (p.payload_json or {}).get("text") == text
        ),
        None,
    )


def capture(
    session: Session,
    extractor: CaptureExtractor | None,
    text: str,
    *,
    today: date,
    default_currency: str,
    meeting_id: int | None = None,
    tz: ZoneInfo = DEFAULT_TZ,
    now: datetime | None = None,
    defer: bool = False,
) -> CaptureResult:
    """Dictated text -> a `capture` proposal (+ `new_person` proposals). Never applies anything.

    With `defer=True` Claude is not asked here: the text is saved at once as a note draft marked
    "processing" and the result carries a `PendingCapture` for the caller to ask later (SPEC
    §2.33). Without a Claude client nothing is pending: it is the note draft as ever.

    Idempotent for a short while: the same text (for the same meeting) still pending from the last
    few minutes is returned as it is, without asking Claude again.

    After a meeting (`meeting_id`, from the post-meeting push), its people are candidates even when
    the text doesn't name them, Claude is told about the meeting, and the proposal also holds the
    meeting's `meet` interaction."""
    text = text.strip()
    if not text:
        raise InvalidInputError("Nothing to capture: the text is empty.")
    duplicate = _recent_duplicate(session, text, meeting_id, now or datetime.now(UTC))
    if duplicate is not None:
        return CaptureResult([duplicate], f"Already in Inbox: {duplicate.summary}")
    meeting = session.get(Meeting, meeting_id) if meeting_id is not None else None
    if meeting is None and meeting_id is not None:
        log.warning("capture for unknown meeting %s: kept without it", meeting_id)
        meeting_id = None
    candidates = match_people(session, text)
    active = goals.active_goals(session)
    owner: Person | None = None
    if meeting is not None:
        met = meeting.people
        candidates = [*met, *(p for p in candidates if p not in met)][:MAX_CANDIDATES]
        owner = met[0] if len(met) == 1 else None
    asked = [
        *candidates,
        *recall_people(session, now or datetime.now(UTC), tz, {p.id for p in candidates}),
    ][:MAX_CANDIDATES]
    shown = open_items(session, asked)
    courses = school.current_courses(session)
    note: str | None = None
    extraction: CaptureExtraction | None = None
    pending: PendingCapture | None = None
    if extractor is None:
        note = "Claude isn't configured; saved your text as a note draft."
    else:
        request = CaptureRequest(
            text=text,
            today=today,
            candidates=candidate_people(asked, shown),
            default_currency=default_currency,
            corrections=tuple(corrections.recent_corrections(session)),
            rejected=tuple(corrections.recent_rejections(session)),
            context=meetings.context_text(meeting, tz) if meeting is not None else None,
            goals=tuple(
                CandidateGoal(g.id, g.rank or 0, g.title, g.why) for g in active[:MAX_GOALS]
            ),
            courses=tuple(CandidateCourse(c.code, c.name) for c in courses[:MAX_COURSES]),
        )
        if defer:
            note = "Saved; Claude is reading it and you get a push when it is ready."
            pending = PendingCapture(
                request=request,
                since=now or datetime.now(UTC),
                goal_ids=tuple(g.id for g in active[:MAX_GOALS]),
                default_currency=default_currency,
                tz=tz,
            )
        else:
            try:
                extraction = extractor.extract_capture(request)
            except ClaudeError as exc:
                log.warning("capture fell back to a note draft: %s", exc)
                note = "Claude couldn't process it; saved your text as a note draft."

    standalone: list[NewPersonPayload] = []
    deadlines: list[Proposal] = []
    confidence: float | None = None
    if extraction is not None:
        deadlines = school_deadline_proposals(
            session, extraction, courses, text=text, today=today, tz=tz
        )
        payload, standalone = payload_from_extraction(
            extraction,
            text=text,
            candidates=asked,
            default_currency=default_currency,
            meeting_id=meeting_id,
            goal_ids={g.id for g in active[:MAX_GOALS]},
            shown=shown,
        )
        confidence = min(max(extraction.confidence, 0.0), 1.0)
        if not payload.items() and not standalone and not deadlines:
            payload = fallback_payload(text, candidates, meeting_id, owner)
            note = "Found nothing specific; saved your text as a note draft."
    else:
        payload = fallback_payload(text, candidates, meeting_id, owner)
    if meeting is not None:
        add_meeting_interaction(session, payload, meeting, tz)

    group = uuid.uuid4().hex
    payload.group = group
    payload.processing = pending is not None
    for person in standalone:
        person.source_text = text[:SOURCE_TEXT_MAX]
        person.group = group

    proposals: list[Proposal] = []
    source_id = str(meeting_id) if meeting_id is not None else None
    if payload.items():
        proposal = Proposal(
            kind=ProposalKind.CAPTURE,
            status=ProposalStatus.PENDING,
            summary=capture_summary(session, payload),
            payload_json=payload.model_dump(mode="json"),
            original_json=payload.model_dump(mode="json"),
            confidence=confidence,
            source_type=SOURCE_TYPE,
            source_id=source_id,
            prompt_version=prompt_version("capture") if extraction is not None else None,
        )
        proposals.append(proposal)
    for person in standalone:
        context = f" ({person.context})" if person.context else ""
        proposals.append(
            Proposal(
                kind=ProposalKind.NEW_PERSON,
                status=ProposalStatus.PENDING,
                summary=f"New person? {person.name}{context}",
                payload_json=person.model_dump(mode="json"),
                original_json=person.model_dump(mode="json"),
                confidence=confidence,
                source_type=SOURCE_TYPE,
                source_id=source_id,
                prompt_version=prompt_version("capture"),
            )
        )
    proposals.extend(deadlines)
    session.add_all(proposals)
    session.flush()
    return CaptureResult(proposals, _shortcut_summary(session, proposals, note), pending)


def _is_untouched_draft(payload: CapturePayload, text: str) -> bool:
    """The note draft `fallback_payload` makes, with nothing changed since."""
    only_meeting = payload.meeting_id is not None and len(payload.interactions) <= 1
    return (
        len(payload.notes) == 1
        and payload.notes[0].include
        and payload.notes[0].text == text
        and not payload.debts
        and not payload.promises
        and not payload.appointments
        and not payload.settles
        and not payload.promise_updates
        and not payload.new_people
        and (not payload.interactions or only_meeting)
    )


@dataclass(frozen=True)
class LateCapture:
    """What came of an answer that arrived after the request had given up (SPEC §2.19)."""

    outcome: str  # "applied" or why it was not
    summary: str = ""  # for the push, when applied


def _stop_processing(proposal: Proposal, payload: CapturePayload) -> None:
    if payload.processing:
        payload.processing = False
        proposal.payload_json = payload.model_dump(mode="json")


def give_up(session: Session, *, text: str, since: datetime) -> bool:
    """Claude could not answer (failed, expired, declined): the draft stays the note it is, and
    stops saying "processing". Returns whether a draft was found. Callers commit."""
    drafts = session.scalars(
        select(Proposal)
        .where(
            Proposal.kind == ProposalKind.CAPTURE,
            Proposal.source_type == SOURCE_TYPE,
            Proposal.created_at >= since - timedelta(minutes=1),
        )
        .order_by(Proposal.id.desc())
    )
    proposal = next((p for p in drafts if (p.payload_json or {}).get("text") == text), None)
    if proposal is None:
        return False
    _stop_processing(proposal, CapturePayload.model_validate(proposal.payload_json))
    session.flush()
    return True


def upgrade_draft(
    session: Session,
    extraction: CaptureExtraction,
    *,
    text: str,
    since: datetime,
    goal_ids: Collection[int],
    default_currency: str,
    tz: ZoneInfo = DEFAULT_TZ,
) -> LateCapture | None:
    """Replace the note draft of a capture whose Claude answer came late with what Claude found.

    Only a draft that is still pending and untouched is replaced; a confirmed, rejected or edited
    one is left alone. Returns None when no draft was found yet (the caller may still be saving
    it: try again shortly). Callers commit."""
    drafts = session.scalars(
        select(Proposal)
        .where(
            Proposal.kind == ProposalKind.CAPTURE,
            Proposal.source_type == SOURCE_TYPE,
            Proposal.created_at >= since - timedelta(minutes=1),
        )
        .order_by(Proposal.id.desc())
    )
    proposal = next((p for p in drafts if (p.payload_json or {}).get("text") == text), None)
    if proposal is None:
        return None
    if proposal.status is not ProposalStatus.PENDING:
        return LateCapture(f"already {proposal.status.value}")
    payload = CapturePayload.model_validate(proposal.payload_json)
    if not _is_untouched_draft(payload, text):
        _stop_processing(proposal, payload)
        return LateCapture("the draft was edited")
    candidates = [p for pid in payload.candidates if (p := session.get(Person, pid)) is not None]
    meeting = session.get(Meeting, payload.meeting_id) if payload.meeting_id is not None else None
    new_payload, standalone = payload_from_extraction(
        extraction,
        text=text,
        candidates=candidates,
        default_currency=default_currency,
        meeting_id=payload.meeting_id,
        goal_ids=set(goal_ids),
        shown=open_items(session, candidates),
    )
    deadlines = school_deadline_proposals(
        session,
        extraction,
        school.current_courses(session),
        text=text,
        today=datetime.now(tz).date(),
        tz=tz,
    )
    if not new_payload.items() and not standalone and not deadlines:
        _stop_processing(proposal, payload)
        return LateCapture("Claude found nothing specific")
    if meeting is not None:
        add_meeting_interaction(session, new_payload, meeting, tz)
    group = payload.group or uuid.uuid4().hex
    new_payload.group = group
    for person in standalone:
        person.source_text = text[:SOURCE_TEXT_MAX]
        person.group = group
    confidence = min(max(extraction.confidence, 0.0), 1.0)
    source_id = proposal.source_id
    if new_payload.items():
        proposal.payload_json = new_payload.model_dump(mode="json")
        proposal.original_json = new_payload.model_dump(mode="json")
        proposal.summary = capture_summary(session, new_payload)
        proposal.confidence = confidence
        proposal.prompt_version = prompt_version("capture")
    else:
        session.delete(proposal)  # only people or deadlines came of it: the note draft goes
    for person in standalone:
        context = f" ({person.context})" if person.context else ""
        session.add(
            Proposal(
                kind=ProposalKind.NEW_PERSON,
                status=ProposalStatus.PENDING,
                summary=f"New person? {person.name}{context}",
                payload_json=person.model_dump(mode="json"),
                original_json=person.model_dump(mode="json"),
                confidence=confidence,
                source_type=SOURCE_TYPE,
                source_id=source_id,
                prompt_version=prompt_version("capture"),
            )
        )
    session.add_all(deadlines)
    session.flush()
    summary = capture_summary(session, new_payload) if new_payload.items() else ""
    summary = summary or "; ".join(p.summary for p in deadlines)
    return LateCapture("applied", summary)


# --- Applying (on confirm) ---


@dataclass(frozen=True)
class AppliedCapture:
    people: int = 0
    notes: int = 0
    interactions: int = 0
    debts: int = 0
    promises: int = 0
    appointments: int = 0
    settled: int = 0
    closed: int = 0
    skipped: tuple[str, ...] = ()  # items that no longer applied, with why

    def __str__(self) -> str:
        parts = [
            (self.people, "new person", "new people"),
            (self.notes, "note", "notes"),
            (self.interactions, "interaction", "interactions"),
            (self.debts, "debt", "debts"),
            (self.promises, "promise", "promises"),
            (self.appointments, "appointment", "appointments"),
            (self.settled, "debt settled", "debts settled"),
            (self.closed, "promise closed", "promises closed"),
        ]
        shown = [f"{n} {one if n == 1 else many}" for n, one, many in parts if n]
        text = ", ".join(shown) or "nothing"
        return f"{text}. Skipped: {'; '.join(self.skipped)}" if self.skipped else text


def _split_name(name: str) -> tuple[str, str | None]:
    first, _, rest = " ".join(name.split()).partition(" ")
    return first, rest or None


def apply_capture(
    session: Session,
    payload: CapturePayload,
    *,
    today: date,
    default_currency: str,
    tz: ZoneInfo = DEFAULT_TZ,
) -> AppliedCapture:
    """Create everything the confirmed payload describes. Everything is checked before anything
    is written; on an error the caller rolls back, so it's all or nothing."""
    names = _names(session, payload.referenced_ids())

    def label(item: Item) -> str:
        return describe_item(item, payload, names)

    # 1. Which items count, and is everything they need there?
    skipped = {f"new:{i}" for i, p in enumerate(payload.new_people) if p.action == "skip"}
    items: list[Item] = []
    for item in payload.included():
        refs = [r for r in _refs(item) if r not in skipped]
        if isinstance(item, InteractionDraft):
            if not refs:
                if item.people:
                    continue  # everyone on it was skipped
                raise InvalidInputError(f"Pick who this was with: {label(item)}")
        elif isinstance(item, AppointmentDraft):
            if item.person in skipped:
                continue
            if not item.title.strip():
                raise InvalidInputError(f"Empty title: {label(item)}")
            if item.start.date() < today:
                raise InvalidInputError(f"Pick a date that isn't past: {label(item)}")
            if item.end is not None and item.end <= item.start:
                raise InvalidInputError(f"The end is before the start: {label(item)}")
        elif item.person is None:
            raise InvalidInputError(f"Pick a person for: {label(item)}")
        elif item.person in skipped:
            continue
        for ref in refs:
            if ref.isdigit() and int(ref) not in names:
                raise InvalidInputError(f"Person #{ref} no longer exists: {label(item)}")
            if ref.startswith("new:") and int(ref[4:]) >= len(payload.new_people):
                raise InvalidInputError(f"Unknown new person {ref}.")
        if isinstance(item, SettleDraft | PromiseUpdateDraft):
            items.append(item)  # still open? checked when applied; if not, it is skipped
            continue
        if isinstance(item, DebtDraft) and item.amount_missing:
            raise InvalidInputError(f"Amount missing: {label(item)}")
        if isinstance(item, DebtDraft) and item.type is not DebtType.MONEY and not item.description:
            raise InvalidInputError(f"Describe the {item.type.value}: {label(item)}")
        if isinstance(item, NoteDraft | PromiseDraft) and not item.text.strip():
            raise InvalidInputError(f"Empty text: {label(item)}")
        if (
            isinstance(item, PromiseDraft)
            and item.goal is not None
            and session.get(Goal, item.goal) is None
        ):
            raise InvalidInputError(f"Goal #{item.goal} no longer exists: {label(item)}")
        items.append(item)
    new_refs = {r for item in items for r in _refs(item) if r.startswith("new:")} - skipped
    used_new = {int(r[4:]) for r in new_refs}
    for index in used_new:
        draft = payload.new_people[index]
        if draft.action == "link":
            if draft.person_id is None:
                raise InvalidInputError(f"Pick the existing person for {draft.name}.")
            people.get_person(session, draft.person_id)

    # 2. Resolve new people, then write.
    resolved: dict[str, int] = {}
    created = 0
    for index in sorted(used_new):
        draft = payload.new_people[index]
        if draft.action == "link" and draft.person_id is not None:
            resolved[f"new:{index}"] = draft.person_id
            continue
        first, last = _split_name(draft.name)
        person = people.create_person(
            session,
            people.PersonCreate(display_name=draft.name, first_name=first, last_name=last),
        )
        resolved[f"new:{index}"] = person.id
        created += 1

    def pid(ref: str) -> int:
        return resolved[ref] if ref.startswith("new:") else int(ref)

    meeting_id = payload.meeting_id
    if meeting_id is not None and session.get(Meeting, meeting_id) is None:
        meeting_id = None
    counts = {"notes": 0, "interactions": 0, "debts": 0, "promises": 0, "appointments": 0}
    settled = closed = 0
    skipped_lines: list[str] = []
    for item in items:
        if isinstance(item, NoteDraft):
            assert item.person is not None
            note = people.add_note(session, people.get_person(session, pid(item.person)), item.text)
            note.meeting_id = meeting_id
            counts["notes"] += 1
        elif isinstance(item, InteractionDraft):
            if item.meeting and (
                meeting_id is None or meeting_interaction(session, meeting_id) is not None
            ):
                continue  # a meeting yields at most one interaction
            interaction = interactions.create_interaction(
                session,
                interactions.InteractionCreate(
                    occurred_on=item.occurred_on or today,
                    kind=item.kind,
                    summary=item.summary,
                    person_ids=[pid(r) for r in item.people if r not in skipped],
                ),
            )
            if item.meeting:
                interaction.meeting_id = meeting_id
            counts["interactions"] += 1
        elif isinstance(item, DebtDraft):
            assert item.person is not None
            ledger.create_debt(
                session,
                ledger.DebtCreate(
                    person_id=pid(item.person),
                    direction=item.direction,
                    type=item.type,
                    amount_minor=item.amount_minor,
                    currency=item.currency if item.type is DebtType.MONEY else None,
                    description=item.description,
                ),
                default_currency,
            )
            counts["debts"] += 1
        elif isinstance(item, SettleDraft):
            debt = session.get(Debt, item.debt_id)
            name = item.label or f"debt #{item.debt_id}"
            if debt is None or not debt.is_open:
                skipped_lines.append(f"{name} was already settled")
                continue
            part = item.amount_minor if debt.type is DebtType.MONEY else None
            if part is not None and part > (debt.amount_minor or 0):
                skipped_lines.append(f"{name}: only {_debt_what(debt)} is still open")
                continue
            ledger.settle(session, debt, part if part != debt.amount_minor else None)
            settled += 1
        elif isinstance(item, PromiseUpdateDraft):
            promise = session.get(Promise, item.promise_id)
            name = item.label or f"promise #{item.promise_id}"
            if promise is None or promise.status is not PromiseStatus.OPEN:
                skipped_lines.append(f"{name} was already closed")
                continue
            status = PromiseStatus.DONE if item.status == "done" else PromiseStatus.DROPPED
            promises.set_status(promise, status)
            closed += 1
        elif isinstance(item, AppointmentDraft):
            who = pid(item.person) if item.person is not None else None
            start = item.start.replace(tzinfo=tz).astimezone(UTC)
            end = item.end.replace(tzinfo=tz).astimezone(UTC) if item.end else None
            if meetings.appointment_exists(session, start, who, item.title.strip()):
                continue  # the same appointment dictated twice
            meetings.create_appointment(
                session,
                title=item.title.strip(),
                start_utc=start,
                end_utc=end,
                location=item.location,
                person_id=who,
            )
            counts["appointments"] += 1
        else:
            assert item.person is not None
            promise = promises.create_promise(
                session,
                promises.PromiseCreate(
                    person_id=pid(item.person),
                    direction=item.direction,
                    text=item.text,
                    due=item.due,
                    goal_id=item.goal,
                ),
                today,
            )
            promise.meeting_id = meeting_id
            counts["promises"] += 1
    session.flush()
    return AppliedCapture(
        people=created, settled=settled, closed=closed, skipped=tuple(skipped_lines), **counts
    )


def apply_new_person(session: Session, payload: NewPersonPayload) -> Person:
    """Create the person a confirmed `new_person` proposal describes (name split into first and
    last) and return it."""
    first, last = _split_name(payload.name)
    person = people.create_person(
        session, people.PersonCreate(display_name=payload.name, first_name=first, last_name=last)
    )
    if payload.context:
        people.add_note(session, person, payload.context)
    return person


def propose_aliases(session: Session, payload: CapturePayload) -> int:
    """For each new name the card linked to an existing person, propose "add it as a name of
    theirs" (SPEC §2.28): so the next dictation recognizes it. Skips names the person already
    answers to, ones already proposed and ones that were rejected before."""
    made = 0
    for draft in payload.new_people:
        if draft.action != "link" or draft.person_id is None:
            continue
        person = session.get(Person, draft.person_id)
        name = " ".join(draft.name.split())
        if person is None or not name:
            continue
        known = {normalize_text(person.display_name), *(a.value_norm for a in person.aliases)}
        if normalize_text(name) in known:
            continue
        same = {"person_id": person.id, "alias": name}
        before = session.scalars(
            select(Proposal).where(
                Proposal.kind == ProposalKind.ADD_ALIAS,
                Proposal.status.in_((ProposalStatus.PENDING, ProposalStatus.REJECTED)),
            )
        )
        if any(p.payload_json == same for p in before):
            continue
        session.add(
            Proposal(
                kind=ProposalKind.ADD_ALIAS,
                status=ProposalStatus.PENDING,
                summary=f"Add “{name}” as another name of {person.display_name}?",
                payload_json=same,
                confidence=None,
                source_type=SOURCE_TYPE,
            )
        )
        made += 1
    session.flush()
    return made


def _confirm_capture(session: Session, payload: CapturePayload, ctx: ApplyContext) -> Applied:
    applied = apply_capture(
        session, payload, today=ctx.today, default_currency=ctx.default_currency
    )
    proposed = propose_aliases(session, payload)
    more = (
        f" {proposed} name suggestion{'s' if proposed != 1 else ''} in Inbox." if proposed else ""
    )
    return Applied(f"Saved: {applied}.{more}")


def _confirm_new_person(session: Session, payload: NewPersonPayload, ctx: ApplyContext) -> Applied:
    person = apply_new_person(session, payload)
    return Applied(f"Added {person.display_name}.", person.id)


CAPTURE_HANDLER = ProposalHandler(
    ProposalKind.CAPTURE, CapturePayload, _confirm_capture, summary=capture_summary
)
NEW_PERSON_HANDLER = ProposalHandler(ProposalKind.NEW_PERSON, NewPersonPayload, _confirm_new_person)


def pending_in_group(session: Session, group: str | None) -> list[Proposal]:
    """The pending `capture` and `new_person` proposals of one dictation, oldest first."""
    if not group:
        return []
    found = session.scalars(
        select(Proposal)
        .where(
            Proposal.kind.in_((ProposalKind.CAPTURE, ProposalKind.NEW_PERSON)),
            Proposal.status == ProposalStatus.PENDING,
            Proposal.source_type == SOURCE_TYPE,
        )
        .order_by(Proposal.id)
    )
    return [p for p in found if (p.payload_json or {}).get("group") == group]
