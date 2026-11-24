"""A Claude stand-in that answers offline, for demos, screenshots and CI (DECISIONS #35).

`FakeClaude` is a `ClaudeClient` whose model calls are replaced by keyword rules: the same request
always gives the same answer, nothing is random and nothing touches the network. Every answer is
built as the real Pydantic output type, so the rest of Orbit (the capture card, the Inbox, the
weekly review checks) runs unchanged. The text it reads is only ever data: instructions inside it
cannot change the shape of an answer, because the answer is assembled from a few regular
expressions and the lists that came with the request.

It is chosen with `ORBIT_CLAUDE_MODE=fake` (or `ORBIT_DEMO=1` when nothing else is configured).
Calls are reported through the same usage callback as the real client, with model "fake" and 0
tokens, so the usage page and `ClaudeCall` rows look the way they do in production.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any, Literal

from orbit.domain.enums import (
    Classification,
    DebtDirection,
    DebtType,
    InteractionKind,
    PromiseDirection,
)
from orbit.domain.normalize import normalize_text
from orbit.integrations.claude import (
    DEFAULT_OWNER,
    BriefLine,
    BriefRequest,
    CandidatePerson,
    CaptureExtraction,
    CaptureRequest,
    ClassifiedTransaction,
    ClaudeClient,
    DraftRequest,
    ExtractedAppointment,
    ExtractedDebt,
    ExtractedInteraction,
    ExtractedNote,
    ExtractedPromise,
    FollowUpIdea,
    GoalRerankIdea,
    MeetingLinkRequest,
    MeetingPeople,
    MessageDraft,
    ReportAnswer,
    ReportOutput,
    ReportRequest,
    ReviewAnswer,
    ReviewConflict,
    ReviewItem,
    ReviewOutput,
    ReviewPriority,
    ReviewProposals,
    ReviewRequest,
    SchoolDeadlineIdea,
    SchoolFactIdea,
    SchoolLinkIdea,
    SchoolReadOutput,
    SchoolReadRequest,
    TransactionClassification,
    TransactionRow,
    UnknownPerson,
    UsageCallback,
)

FAKE_MODEL = "fake"
FAKE_CONFIDENCE = 0.72

_MONEY = re.compile(
    r"(?P<amount>\d+(?:[.,]\d{1,2})?)\s*(?P<cur>€|eur\b|euro|kč|kc\b|czk\b|korun)", re.IGNORECASE
)
_ITEM = re.compile(
    r"\b(?:lent|borrowed)(?:\s+(?:me|him|her|them))?\s+(?:(?:his|her|their|my|a|the)\s+)?"
    r"(?P<what>[a-z]+(?:\s[a-z]+)?)",
    re.IGNORECASE,
)
_REASON = re.compile(r"\bfor\s+(?:the\s+|my\s+|a\s+)?(?P<why>[a-z]+(?:\s[a-z]+)?)", re.IGNORECASE)
_TIME = re.compile(r"\b(?:at|o|v|na)\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\b", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b(?P<d>20\d{2}-\d{2}-\d{2})\b")
_URL = re.compile(r"https?://[^\s)>\"']+")
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "pondelok": 0,
    "utorok": 1,
    "streda": 2,
    "stvrtok": 3,
    "piatok": 4,
    "sobota": 5,
    "nedela": 6,
}
_OWE_ME = ("owes me", "owe me", "dlzi mi", "paid for", "lent", "požičal", "pozical", "zaplatil som")
_I_OWE = ("i owe", "dlzim", "borrowed", "lent me", "paid for me", "zaplatil za mna")
_PROMISE_MINE = ("i promised", "i will", "i'll", "slubil som", "poslem", "pošlem")
_PROMISE_THEIRS = ("promised me", "slubil mi", "will send me", "sends me")
_APPOINTMENT_WORDS = ("haircut", "dentist", "doctor", "barber", "appointment", "strih", "zubar")
_SHARED_WORDS = (
    "restaurant",
    "grill",
    "pizza",
    "bar",
    "pub",
    "cafe",
    "kavarna",
    "kino",
    "cinema",
    "taxi",
    "bolt",
    "uber",
    "hotel",
    "hostel",
    "vstupenk",
    "bistro",
    "burger",
)
_PERSONAL_WORDS = (
    "spotify",
    "netflix",
    "gym",
    "pharmacy",
    "lekaren",
    "dm drogerie",
    "tram",
    "metro",
    "mhd",
    "apple",
    "steam",
    "billa",
    "potraviny",
    "lidl",
    "albert",
    "tesco",
    "kaufland",
)
_IGNORE_WORDS = ("top-up", "topup", "exchange", "atm", "bankomat", "fee", "poplatok", "refund")


def _norm(text: str) -> str:
    return normalize_text(text)


def _names_of(person: CandidatePerson) -> list[str]:
    """The name forms to look for in a text: full name, each name part, aliases."""
    forms = [person.name, *person.name.split(), *person.aliases]
    return [n for n in dict.fromkeys(_norm(f) for f in forms) if len(n) >= 3]


def _form_pattern(form: str) -> re.Pattern[str]:
    """A name as a whole word; longer names also match inflections ("Terke", "Terkou")."""
    if len(form) < 4 or " " in form:
        return re.compile(rf"\b{re.escape(form)}\b")
    return re.compile(rf"\b{re.escape(form[:-1])}\w{{0,3}}\b")


def _mentioned(text: str, candidates: Sequence[CandidatePerson]) -> list[CandidatePerson]:
    """Candidates whose name or alias occurs in the text, in order of first mention."""
    haystack = _norm(text)
    found: list[tuple[int, CandidatePerson]] = []
    for person in candidates:
        hits = [m.start() for f in _names_of(person) if (m := _form_pattern(f).search(haystack))]
        if hits:
            found.append((min(hits), person))
    return [person for _, person in sorted(found, key=lambda pair: pair[0])]


def _next_weekday(today: date, weekday: int) -> date:
    return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)


def _resolve_day(text: str, today: date) -> date | None:
    folded = _norm(text)
    if "tomorrow" in folded or "zajtra" in folded or "zitra" in folded:
        return today + timedelta(days=1)
    if "next week" in folded or "buduci tyzden" in folded:
        return today + timedelta(days=7)
    for word, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{word}\b", folded):
            return _next_weekday(today, weekday)
    return None


def _money_reason(text: str) -> str:
    found = _REASON.search(text)
    return found.group("why").strip().capitalize() if found else _clip(text, 40)


def _currency(raw: str, default: str) -> str:
    folded = raw.lower()
    if folded.startswith(("k", "czk")):
        return "CZK"
    if folded in ("€", "eur", "euro"):
        return "EUR"
    return default


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    return parts or [text.strip()]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- Capture ---


def fake_capture(request: CaptureRequest) -> CaptureExtraction:
    text = request.text
    folded = _norm(text)
    people = _mentioned(text, request.candidates)
    notes: list[ExtractedNote] = []
    interactions: list[ExtractedInteraction] = []
    debts: list[ExtractedDebt] = []
    promises: list[ExtractedPromise] = []
    appointments: list[ExtractedAppointment] = []
    unknown: list[UnknownPerson] = []

    # Names that look like people but are not in the list ("with Dana", "s Danou").
    for match in re.finditer(r"\b(?:with|met|called|s|so)\s+([A-Z][a-z]{2,})\b", text):
        name = match.group(1)
        if _mentioned(name, request.candidates):
            continue
        if name not in {u.name for u in unknown}:
            unknown.append(UnknownPerson(name=name, context=_clip(text, 60)))

    first_ref = str(people[0].id) if people else (f"new:{0}" if unknown else None)

    money = _MONEY.search(text)
    wants_debt = any(w in folded for w in ("owe", "dlzi", "dlzim", "paid for", "lent", "pozical"))
    if first_ref and money and wants_debt:
        direction = (
            DebtDirection.I_OWE_THEM
            if any(w in folded for w in _I_OWE) and "owes me" not in folded
            else DebtDirection.THEY_OWE_ME
        )
        amount = money.group("amount").replace(",", ".")
        debts.append(
            ExtractedDebt(
                person_ref=first_ref,
                direction=direction,
                type=DebtType.MONEY,
                amount=amount,
                currency=_currency(money.group("cur"), request.default_currency),
                description=_money_reason(text),
            )
        )
    elif first_ref and any(w in folded for w in ("lent", "borrowed", "pozical")):
        item = _ITEM.search(text)
        debts.append(
            ExtractedDebt(
                person_ref=first_ref,
                direction=DebtDirection.I_OWE_THEM
                if "lent me" in folded or "borrowed" in folded
                else DebtDirection.THEY_OWE_ME,
                type=DebtType.ITEM,
                amount=None,
                currency=None,
                description=(item.group("what").strip().capitalize() if item else _clip(text, 40)),
            )
        )

    appointment_word = next((w for w in _APPOINTMENT_WORDS if w in folded), None)
    day = _resolve_day(text, request.today)
    clock = _TIME.search(text)
    if appointment_word and day and clock:
        hour, minute = int(clock.group("h")), int(clock.group("m") or 0)
        if hour < 24 and minute < 60:
            appointments.append(
                ExtractedAppointment(
                    person_ref=str(people[0].id) if people else None,
                    title=appointment_word.capitalize(),
                    start=f"{day.isoformat()}T{hour:02d}:{minute:02d}",
                )
            )

    if first_ref and not appointments:
        theirs = any(w in folded for w in _PROMISE_THEIRS)
        mine = any(w in folded for w in _PROMISE_MINE)
        if theirs or mine:
            promises.append(
                ExtractedPromise(
                    person_ref=first_ref,
                    direction=(
                        PromiseDirection.THEY_PROMISED if theirs else PromiseDirection.I_PROMISED
                    ),
                    text=_clip(text, 120),
                    due=day,
                    goal_ref=None,
                )
            )

    if people and not (debts or promises or appointments):
        met = any(
            w in folded for w in ("met ", "lunch", "coffee", "dinner", "kafe", "obed", "beer")
        )
        if met:
            interactions.append(
                ExtractedInteraction(
                    person_refs=[str(p.id) for p in people[:3]],
                    kind=InteractionKind.MEET,
                    summary=_clip(text, 120),
                    occurred_on=None,
                )
            )
        else:
            for person in people[:2]:
                notes.append(ExtractedNote(person_ref=str(person.id), text=_clip(text, 300)))
    return CaptureExtraction(
        notes=notes,
        interactions=interactions,
        debts=debts,
        promises=promises,
        appointments=appointments,
        unknown_people=unknown,
        confidence=FAKE_CONFIDENCE,
    )


# --- Bank rows ---


def _amount_minor(text: str) -> int:
    match = re.match(r"\s*(\d+)(?:[.,](\d{1,2}))?", text)
    if not match:
        return 0
    return int(match.group(1)) * 100 + int((match.group(2) or "0").ljust(2, "0"))


def fake_classify(rows: Sequence[TransactionRow], shared_min: str) -> TransactionClassification:
    threshold = _amount_minor(shared_min) or 1500
    out: list[ClassifiedTransaction] = []
    seen: set[int] = set()
    for row in rows:
        if row.ref in seen:
            continue
        seen.add(row.ref)
        text = _norm(f"{row.description} {row.kind}")
        amount = _amount_minor(row.amount)
        kind = _norm(row.kind)
        if any(w in text for w in _IGNORE_WORDS) or kind in ("topup", "exchange", "atm", "fee"):
            label, confidence = Classification.IGNORE, 0.9
        elif row.direction == "in":
            label, confidence = Classification.INCOMING_FROM_PERSON, 0.8
        elif any(w in text for w in _SHARED_WORDS) and (amount >= threshold or "hotel" in text):
            label, confidence = Classification.LIKELY_SHARED, 0.78
        elif row.meeting and any(w in text for w in _SHARED_WORDS):
            label, confidence = Classification.LIKELY_SHARED, 0.7
        else:
            label, confidence = Classification.PERSONAL, 0.75
        out.append(ClassifiedTransaction(ref=row.ref, classification=label, confidence=confidence))
    return TransactionClassification(transactions=out)


# --- Meetings, briefs, drafts ---


def fake_link(request: MeetingLinkRequest) -> MeetingPeople:
    haystack = _norm(f"{request.title} {request.location or ''}")
    ids = []
    for person in request.candidates:
        if any(_form_pattern(form).search(haystack) for form in _names_of(person)):
            ids.append(person.id)
    # Several candidates fit the same word: that is the case a real model would also leave open.
    return MeetingPeople(person_ids=ids if len(ids) == 1 else [])


def fake_brief(request: BriefRequest) -> BriefLine:
    for person in request.people:
        if person.notes:
            return BriefLine(summary=_clip(f"{person.name}: {person.notes[0]}", 160))
    return BriefLine(summary="Nothing new in the notes.")


_GREETINGS = {
    ("birthday", "en"): "Happy birthday, {name}!{age} Hope it's a good one.",
    ("birthday", "sk"): "Všetko najlepšie k narodeninám, {name}!{age} Uži si to.",
    ("birthday", "cs"): "Všechno nejlepší k narozeninám, {name}!{age} Užij si to.",
    ("nameday", "en"): "Happy name day, {name}! Enjoy it.",
    ("nameday", "sk"): "Všetko najlepšie k menám, {name}! Pekný deň.",
    ("nameday", "cs"): "Všechno nejlepší k svátku, {name}! Hezký den.",
}


def fake_draft(request: DraftRequest) -> MessageDraft:
    template = _GREETINGS[(request.occasion, request.language)]
    age = f" {request.age}!" if request.age else ""
    text = template.format(name=request.name, age=age)
    if request.notes and request.language == "en":
        text += f" How is it going with {_clip(request.notes[0], 40).rstrip('.')}?"
    return MessageDraft(text=text)


# --- Review and report (they read the JSON snapshots the request carries) ---


def _loads(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dig(data: dict[str, Any], *path: str) -> list[dict[str, Any]]:
    cursor: Any = data
    for key in path:
        cursor = cursor.get(key) if isinstance(cursor, dict) else None
    return [x for x in cursor if isinstance(x, dict)] if isinstance(cursor, list) else []


def _snapshots(*texts: str) -> dict[str, list[dict[str, Any]]]:
    pools: dict[str, list[dict[str, Any]]] = {
        "goals": [],
        "people": [],
        "promises": [],
        "meetings": [],
        "debts": [],
    }
    for text in texts:
        data = _loads(text)
        pools["goals"] += _dig(data, "goals", "goals")
        pools["people"] += _dig(data, "people", "people")
        pools["promises"] += _dig(data, "promises", "promises")
        pools["meetings"] += _dig(data, "meetings", "meetings")
        pools["debts"] += _dig(data, "ledger", "debts")
    return pools


def _ids(items: list[dict[str, Any]], key: str = "id") -> list[int]:
    return list(dict.fromkeys(int(i[key]) for i in items if isinstance(i.get(key), int)))


def _count(pools: dict[str, list[dict[str, Any]]], key: str) -> int:
    return len(_ids(pools[key]))


def fake_review_output(request: ReviewRequest) -> ReviewOutput:
    pools = _snapshots(request.last_week_json, request.next_week_json)
    goals = sorted(
        {g["id"]: g for g in pools["goals"] if isinstance(g.get("id"), int)}.values(),
        key=lambda g: int(g.get("rank") or 0),
    )
    goal_ids = [int(g["id"]) for g in goals]
    promises = list(
        {p["id"]: p for p in pools["promises"] if isinstance(p.get("id"), int)}.values()
    )
    overdue = [p for p in promises if p.get("overdue")]
    meetings = _ids(pools["meetings"])
    people = list({p["id"]: p for p in pools["people"] if isinstance(p.get("id"), int)}.values())
    start, end = request.review_week
    summary = (
        f"Week of {start:%-d %b} to {end:%-d %b}: {_count(pools, 'meetings')} calendar events, "
        f"{len(promises)} open promises ({len(overdue)} overdue) and {len(goal_ids)} active goals. "
        "This is a canned demo review made by rules from the data, not by a model."
    )
    wins = (
        [
            ReviewItem(
                text=f"{goals[0]['title']} is still your top goal; keep its next step small.",
                goal_ids=[goal_ids[0]],
                person_ids=[],
            )
        ]
        if goals
        else []
    )
    slips = [
        ReviewItem(
            text=f"“{p.get('text', 'A promise')}” to {p.get('person', 'someone')} is overdue.",
            goal_ids=[],
            person_ids=[int(p["person_id"])] if isinstance(p.get("person_id"), int) else [],
        )
        for p in overdue[:3]
    ]
    conflicts: list[ReviewConflict] = []
    if len(meetings) >= 2 and overdue:
        conflicts.append(
            ReviewConflict(
                title="Busy calendar and overdue promises",
                detail=(
                    f"{len(meetings)} events are planned while {len(overdue)} promises are late."
                ),
                suggestion="Block one evening for the late promises before adding more plans.",
                goal_ids=[],
                person_ids=[],
                meeting_ids=meetings[:2],
                promise_ids=[int(p["id"]) for p in overdue[:2]],
            )
        )
    priorities = [
        ReviewPriority(
            text=f"Move one step on “{g['title']}”.",
            why=g.get("why") or "It ranks high.",
            goal_id=int(g["id"]),
        )
        for g in goals[:3]
    ]
    priorities += [
        ReviewPriority(
            text=f"Settle “{p.get('text', 'the promise')}”.", why="It is overdue.", goal_id=None
        )
        for p in overdue[:2]
    ]
    follow_ups = []
    for person in people:
        if person.get("last_contact") is None and isinstance(person.get("id"), int):
            follow_ups.append(
                FollowUpIdea(
                    person_id=int(person["id"]),
                    text="Write a short message to catch up",
                    due=request.plan_week[0] + timedelta(days=2),
                    goal_id=None,
                    reason="No contact is recorded yet.",
                )
            )
            break
    if not follow_ups and people and isinstance(people[-1].get("id"), int):
        follow_ups.append(
            FollowUpIdea(
                person_id=int(people[-1]["id"]),
                text="Check in this week",
                due=request.plan_week[0] + timedelta(days=3),
                goal_id=None,
                reason="They have been quiet the longest.",
            )
        )
    rerank = None
    if len(goal_ids) >= 2:
        rerank = GoalRerankIdea(
            goal_ids=[goal_ids[1], goal_ids[0], *goal_ids[2:]],
            reason="The second goal has more open steps this week.",
        )
    return ReviewOutput(
        summary=summary,
        wins=wins,
        slips=slips,
        conflicts=conflicts,
        priorities=priorities[:7],
        proposals=ReviewProposals(
            follow_ups=follow_ups[:1], promise_dues=[], goal_rerank=rerank, goal_links=[]
        ),
    )


def fake_report_output(request: ReportRequest) -> ReportOutput:
    pools = _snapshots(request.snapshot_json)
    focus = request.focus_text or request.focus_goal or request.focus_person or request.focus_course
    summary = (
        f"From {request.start:%-d %b} to {request.end:%-d %b}: {_count(pools, 'meetings')} events, "
        f"{_count(pools, 'promises')} promises and {_count(pools, 'goals')} goals in view."
        + (f" Focus: {_clip(focus, 80)}." if focus else "")
        + " Canned demo text built by rules."
    )
    goals = list({g["id"]: g for g in pools["goals"] if isinstance(g.get("id"), int)}.values())
    highlights = [
        ReviewItem(
            text=f"Goal “{g['title']}” stayed active.", goal_ids=[int(g["id"])], person_ids=[]
        )
        for g in goals[:2]
    ]
    return ReportOutput(
        summary=summary,
        highlights=highlights,
        slips=[],
        patterns=[],
        questions=["What is the one thing to finish next week?"],
    )


# --- School reader ---

_DEADLINE_WORDS = ("test", "exam", "homework", "project", "deadline", "assignment", "quiz")
_FACT_WORDS = ("points", "credit", "grade", "attendance", "required", "at least")


def fake_school(request: SchoolReadRequest) -> SchoolReadOutput:
    deadlines: list[SchoolDeadlineIdea] = []
    facts: list[SchoolFactIdea] = []
    links: list[SchoolLinkIdea] = []
    known = " ".join(request.known_deadlines)
    seen_facts = {_norm(f) for f in request.known_facts}
    for raw in request.text.splitlines():
        line = raw.lstrip("+- ").strip()
        if not line:
            continue
        folded = line.lower()
        date_match = _ISO_DATE.search(line)
        if date_match and any(w in folded for w in _DEADLINE_WORDS):
            due = date_match.group("d")
            if due in known and _clip(line, 20)[:10] in known:
                continue
            kind: Literal["exam", "test", "deadline"] = (
                "exam" if "exam" in folded else "test" if "test" in folded else "deadline"
            )
            deadlines.append(
                SchoolDeadlineIdea(
                    title=_clip(re.sub(_ISO_DATE, "", line).strip(" :-"), 80) or "Deadline",
                    kind=kind,
                    due=due,
                    all_day=True,
                    points=None,
                    quote=_clip(line, 200),
                )
            )
        elif any(w in folded for w in _FACT_WORDS) and _norm(line) not in seen_facts:
            topic: Literal["attendance", "grading"] = (
                "attendance" if "attendance" in folded else "grading"
            )
            facts.append(SchoolFactIdea(topic=topic, text=_clip(line, 200), quote=_clip(line, 200)))
    for url in dict.fromkeys(_URL.findall(request.text)):
        if "login" not in url:
            links.append(SchoolLinkIdea(url=url, why="Linked from the course page."))
    return SchoolReadOutput(deadlines=deadlines[:10], facts=facts[:10], links=links[:5])


# --- The client ---


class FakeClaude(ClaudeClient):
    """Offline answers with the `ClaudeClient` interface; used when the mode is "fake"."""

    def __init__(
        self, on_usage: UsageCallback | None = None, *, owner_name: str = DEFAULT_OWNER
    ) -> None:
        # No API client: nothing here may reach the network.
        self._owner = owner_name
        self._model = self._fast_model = self._deep_model = FAKE_MODEL
        self._deep_effort = None
        self._on_usage = on_usage

    def model_for(self, purpose: str) -> str:
        return FAKE_MODEL

    def _answer[R](self, purpose: str, make: Callable[[], R]) -> R:
        result = make()
        self._report(purpose, None, ok=True)
        return result

    def extract_capture(self, request: CaptureRequest) -> CaptureExtraction:
        return self._answer("capture", lambda: fake_capture(request))

    def classify_transactions(
        self, rows: Sequence[TransactionRow], shared_min: str
    ) -> TransactionClassification:
        return self._answer("classification", lambda: fake_classify(rows, shared_min))

    def link_meeting(self, request: MeetingLinkRequest) -> MeetingPeople:
        return self._answer("meeting_link", lambda: fake_link(request))

    def summarize_brief(self, request: BriefRequest) -> BriefLine:
        return self._answer("brief", lambda: fake_brief(request))

    def weekly_review(self, request: ReviewRequest) -> ReviewAnswer:
        output = self._answer("weekly_review", lambda: fake_review_output(request))
        return ReviewAnswer(output, FAKE_MODEL, 0, 0)

    def read_school(self, request: SchoolReadRequest) -> SchoolReadOutput:
        return self._answer("school_read", lambda: fake_school(request))

    def make_report(self, request: ReportRequest) -> ReportAnswer:
        output = self._answer("report", lambda: fake_report_output(request))
        return ReportAnswer(output, FAKE_MODEL, 0, 0)

    def draft_message(self, request: DraftRequest) -> MessageDraft:
        return self._answer("draft", lambda: fake_draft(request))

    def close(self) -> None:
        return None
