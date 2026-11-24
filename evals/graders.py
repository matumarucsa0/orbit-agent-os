"""Deterministic graders for the three eval suites.

A grader is a function `(case, output) -> None | str`: None means pass, a string is the reason for
failing. `grade(case, response)` first validates the response against the real Pydantic output
model (the schema grader); when that fails, every other grader fails with "schema invalid" because
there is nothing reliable to look at.

No grader calls a model. The only model-based check is the optional review-summary judge in
`evals/judge.py`, which is not part of case pass/fail.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ValidationError

from evals.loader import OUTPUT_MODELS, Case

CheckFn = Callable[[Case, Any], str | None]


@dataclass(frozen=True)
class Grade:
    name: str
    passed: bool
    detail: str = ""


# --- helpers ---


def norm(text: str) -> str:
    """Casefolded, accent-free text, for matching inflected names."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def dump_text(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)


def to_minor(amount: str | None) -> int | None:
    """Money string in major units to an integer of minor units; None when not a clean amount."""
    if amount is None:
        return None
    try:
        value = Decimal(amount.strip())
    except InvalidOperation:
        return None
    cents = value * 100
    if not value.is_finite() or value < 0 or cents != cents.to_integral_value():
        return None
    return int(cents)


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in _NUMBER.findall(text):
        found.add(Decimal(token.replace(",", ".")))
    return found


def _same_person(expected: Any, actual: str | None) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    return any(
        (o is None and actual is None) or (o is not None and str(o) == actual) for o in options
    )


def _match_unordered[T, E](
    expected: list[E], actual: list[T], same: Callable[[E, T], bool]
) -> str | None:
    """Both lists must pair up one to one under `same`; returns a description of the mismatch."""
    remaining = list(actual)
    missing: list[E] = []
    for want in expected:
        for index, got in enumerate(remaining):
            if same(want, got):
                del remaining[index]
                break
        else:
            missing.append(want)
    if not missing and not remaining:
        return None
    return f"missing {missing}, unexpected {remaining}"


# --- capture ---


def _capture_ids(case: Case) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    inp = case["input"]
    people = {str(p["id"]) for p in inp["candidates"]}
    debts = {f"debt:{d['id']}" for p in inp["candidates"] for d in p.get("debts", ())}
    promises = {f"promise:{d['id']}" for p in inp["candidates"] for d in p.get("promises", ())}
    goals = {str(g["id"]) for g in inp.get("goals", ())}
    courses = {c["code"] for c in inp.get("courses", ())}
    return people, debts, promises, goals, courses


def cap_refs_grounded(case: Case, out: Any) -> str | None:
    people, debts, promises, goals, courses = _capture_ids(case)
    valid_people = people | {f"new:{i}" for i in range(len(out.unknown_people))}
    problems: list[str] = []

    def person(ref: str | None, where: str) -> None:
        if ref is not None and ref not in valid_people:
            problems.append(f"{where}: unknown person_ref {ref!r}")

    for n in out.notes:
        person(n.person_ref, "note")
    for i in out.interactions:
        for ref in i.person_refs:
            person(ref, "interaction")
    for d in out.debts:
        person(d.person_ref, "debt")
    for p in out.promises:
        person(p.person_ref, "promise")
        if p.goal_ref is not None and p.goal_ref not in goals:
            problems.append(f"promise: unknown goal_ref {p.goal_ref!r}")
    for a in out.appointments:
        person(a.person_ref, "appointment")
    for s in out.settles:
        if s.debt_ref not in debts:
            problems.append(f"settle: unknown debt_ref {s.debt_ref!r}")
    for u in out.promise_updates:
        if u.promise_ref not in promises:
            problems.append(f"promise_update: unknown promise_ref {u.promise_ref!r}")
    for sd in out.school_deadlines:
        if sd.course_code is not None and sd.course_code not in courses:
            problems.append(f"school_deadline: unknown course {sd.course_code!r}")
    return "; ".join(problems) or None


def cap_debts_match(case: Case, out: Any) -> str | None:
    def same(want: dict[str, Any], got: Any) -> bool:
        return (
            _same_person(want["person"], got.person_ref)
            and want["direction"] == got.direction.value
            and want["type"] == got.type.value
            and want["amount_minor"] == to_minor(got.amount)
            and want["currency"] == got.currency
        )

    return _match_unordered(case["expected"]["debts"], list(out.debts), same)


def cap_settles_match(case: Case, out: Any) -> str | None:
    def same(want: dict[str, Any], got: Any) -> bool:
        return bool(
            f"debt:{want['debt_id']}" == got.debt_ref
            and want["direction"] == got.direction.value
            and want["amount_minor"] == to_minor(got.amount)
        )

    return _match_unordered(case["expected"]["settles"], list(out.settles), same)


def cap_promises_match(case: Case, out: Any) -> str | None:
    def same(want: dict[str, Any], got: Any) -> bool:
        due = got.due.isoformat() if got.due else None
        if "goal_ref" in want and want["goal_ref"] != got.goal_ref:
            return False
        return (
            _same_person(want["person"], got.person_ref)
            and want["direction"] == got.direction.value
            and want["due"] == due
        )

    return _match_unordered(case["expected"]["promises"], list(out.promises), same)


def cap_promise_updates_match(case: Case, out: Any) -> str | None:
    def same(want: dict[str, Any], got: Any) -> bool:
        return bool(
            f"promise:{want['promise_id']}" == got.promise_ref and want["status"] == got.status
        )

    return _match_unordered(case["expected"]["promise_updates"], list(out.promise_updates), same)


def cap_appointments_match(case: Case, out: Any) -> str | None:
    def same(want: dict[str, Any], got: Any) -> bool:
        return bool(_same_person(want["person"], got.person_ref) and want["start"] == got.start)

    return _match_unordered(case["expected"]["appointments"], list(out.appointments), same)


def cap_school_deadlines_match(case: Case, out: Any) -> str | None:
    def same(want: dict[str, Any], got: Any) -> bool:
        return bool(want["course_code"] == got.course_code and want["due"] == got.due)

    return _match_unordered(case["expected"]["school_deadlines"], list(out.school_deadlines), same)


def cap_amounts_integer(case: Case, out: Any) -> str | None:
    """Money is an exact amount in minor units (no float noise, at most two decimals)."""
    problems: list[str] = []
    for d in out.debts:
        if d.type.value == "money":
            if d.amount is not None and to_minor(d.amount) is None:
                problems.append(f"debt amount {d.amount!r} is not an exact amount")
            if d.amount is not None and not re.fullmatch(r"[A-Z]{3}", d.currency or ""):
                problems.append(f"debt currency {d.currency!r} is not an ISO code")
        elif d.amount is not None or d.currency is not None:
            problems.append(f"{d.type.value} debt carries an amount or currency")
    for s in out.settles:
        if s.amount is not None and to_minor(s.amount) is None:
            problems.append(f"settle amount {s.amount!r} is not an exact amount")
    return "; ".join(problems) or None


def cap_amounts_grounded(case: Case, out: Any) -> str | None:
    """An amount must be a number the dictation contains (or a spelled number the case lists)."""
    allowed = numbers_in(case["input"]["text"]) | {
        Decimal(n) for n in case["expected"].get("extra_numbers", [])
    }
    amounts = [d.amount for d in out.debts] + [s.amount for s in out.settles]
    bad = [a for a in amounts if a is not None and Decimal(a) not in allowed]
    return f"amounts not in the text: {bad}" if bad else None


def cap_no_invented_people(case: Case, out: Any) -> str | None:
    text = norm(case["input"]["text"])
    bad = []
    for person in out.unknown_people:
        stem = norm(person.name).split(" ")[0][:4]
        if not stem or stem not in text:
            bad.append(person.name)
    return f"names not in the text: {bad}" if bad else None


def cap_no_invented_dates(case: Case, out: Any) -> str | None:
    exp = case["expected"]
    allowed = {p["due"] for p in exp["promises"] if p["due"]}
    allowed |= {a["start"][:10] for a in exp["appointments"]}
    allowed |= {s["due"][:10] for s in exp["school_deadlines"]}
    seen: list[str] = [p.due.isoformat() for p in out.promises if p.due]
    seen += [a.start[:10] for a in out.appointments]
    seen += [s.due[:10] for s in out.school_deadlines]
    seen += [i.occurred_on.isoformat() for i in out.interactions if i.occurred_on]
    bad = [d for d in seen if d not in allowed]
    return f"dates not supported by the case: {bad}" if bad else None


def cap_forbidden_absent(case: Case, out: Any) -> str | None:
    exp = case["expected"]
    problems = [
        f"forbidden kind {kind!r} is not empty"
        for kind in exp.get("forbidden_kinds", [])
        if getattr(out, kind)
    ]
    blob = norm(dump_text(out))
    problems += [
        f"forbidden text {s!r} appears" for s in exp.get("must_not_contain", []) if norm(s) in blob
    ]
    return "; ".join(problems) or None


# --- classification ---


def cls_refs_exact(case: Case, out: Any) -> str | None:
    want = Counter(r["ref"] for r in case["input"]["rows"])
    got = Counter(t.ref for t in out.transactions)
    if want == got:
        return None
    return f"expected refs {sorted(want.elements())}, got {sorted(got.elements())}"


def cls_labels_match(case: Case, out: Any) -> str | None:
    labels = case["expected"]["labels"]
    wrong = [
        f"ref {t.ref}: {t.classification.value} not in {labels[str(t.ref)]}"
        for t in out.transactions
        if str(t.ref) in labels and t.classification.value not in labels[str(t.ref)]
    ]
    return "; ".join(wrong) or None


def cls_forbidden_labels_absent(case: Case, out: Any) -> str | None:
    forbidden = case["expected"].get("forbidden_labels", {})
    hit = [
        f"ref {t.ref} labelled {t.classification.value}"
        for t in out.transactions
        if t.classification.value in forbidden.get(str(t.ref), [])
    ]
    return "; ".join(hit) or None


def cls_confidence_in_range(case: Case, out: Any) -> str | None:
    bad = [t.ref for t in out.transactions if not 0.0 <= t.confidence <= 1.0]
    return f"confidence outside 0-1 for refs {bad}" if bad else None


def cls_forbidden_strings_absent(case: Case, out: Any) -> str | None:
    blob = norm(dump_text(out))
    hit = [s for s in case["expected"].get("must_not_contain", []) if norm(s) in blob]
    return f"forbidden text appears: {hit}" if hit else None


# --- weekly review ---


@dataclass(frozen=True)
class Pools:
    goals: set[int]
    people: set[int]
    promises: set[int]
    meetings: set[int]


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _items(data: dict[str, Any], *path: str) -> list[dict[str, Any]]:
    cursor: Any = data
    for key in path:
        cursor = cursor.get(key) if isinstance(cursor, dict) else None
    return [x for x in cursor if isinstance(x, dict)] if isinstance(cursor, list) else []


def review_pools(inp: dict[str, Any]) -> Pools:
    snaps = [_json(inp["last_week_json"]), _json(inp["next_week_json"])]
    goals: set[int] = set()
    people: set[int] = set()
    promises: set[int] = set()
    meetings: set[int] = set()
    for s in snaps:
        for g in _items(s, "goals", "goals"):
            goals.add(g["id"])
            people.update(g.get("people", []))
        for p in _items(s, "people", "people"):
            people.add(p["id"])
        for p in _items(s, "promises", "promises"):
            promises.add(p["id"])
            if isinstance(p.get("person_id"), int):
                people.add(p["person_id"])
        for m in _items(s, "meetings", "meetings"):
            meetings.add(m["id"])
            people.update(m.get("people", []))
        for d in _items(s, "ledger", "debts"):
            if isinstance(d.get("person_id"), int):
                people.add(d["person_id"])
    return Pools(goals, people, promises, meetings)


def review_texts(out: Any) -> list[str]:
    texts = [out.summary]
    for item in [*out.wins, *out.slips]:
        texts.append(item.text)
    for c in out.conflicts:
        texts += [c.title, c.detail, c.suggestion]
    for p in out.priorities:
        texts += [p.text, p.why]
    for f in out.proposals.follow_ups:
        texts += [f.text, f.reason]
    for d in out.proposals.promise_dues:
        texts.append(d.reason)
    if out.proposals.goal_rerank:
        texts.append(out.proposals.goal_rerank.reason)
    for link in out.proposals.goal_links:
        texts.append(link.reason)
    return texts


def _input_blob(case: Case) -> str:
    return norm(json.dumps(case["input"], ensure_ascii=False))


def rev_ids_grounded(case: Case, out: Any) -> str | None:
    pools = review_pools(case["input"])
    bad: list[str] = []

    def check(kind: str, ids: list[int] | int | None, pool: set[int]) -> None:
        for i in [ids] if isinstance(ids, int) else (ids or []):
            if i not in pool:
                bad.append(f"{kind} {i}")

    for item in [*out.wins, *out.slips]:
        check("goal", item.goal_ids, pools.goals)
        check("person", item.person_ids, pools.people)
    for c in out.conflicts:
        check("goal", c.goal_ids, pools.goals)
        check("person", c.person_ids, pools.people)
        check("meeting", c.meeting_ids, pools.meetings)
        check("promise", c.promise_ids, pools.promises)
    for p in out.priorities:
        check("goal", p.goal_id, pools.goals)
    for f in out.proposals.follow_ups:
        check("person", f.person_id, pools.people)
        check("goal", f.goal_id, pools.goals)
    for d in out.proposals.promise_dues:
        check("promise", d.promise_id, pools.promises)
    if out.proposals.goal_rerank:
        check("goal", out.proposals.goal_rerank.goal_ids, pools.goals)
    for link in out.proposals.goal_links:
        check("goal", link.goal_id, pools.goals)
        check("promise", link.promise_id, pools.promises)
        check("person", link.person_id, pools.people)
    return f"ids not in the snapshots: {sorted(set(bad))}" if bad else None


_SMALL_COUNT = 10  # "three promises", "2 meetings": small counts are not checkable facts


def rev_numbers_grounded(case: Case, out: Any) -> str | None:
    """Every number above 10 or with decimals must occur in the input (amounts, dates, days)."""
    blob = _input_blob(case)
    bad: list[str] = []
    for text in review_texts(out):
        for token in _NUMBER.findall(text):
            value = Decimal(token.replace(",", "."))
            if value == value.to_integral_value() and value <= _SMALL_COUNT:
                continue
            if token not in blob and token.replace(",", ".") not in blob:
                bad.append(token)
    return f"numbers not in the input: {sorted(set(bad))}" if bad else None


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def rev_dates_grounded(case: Case, out: Any) -> str | None:
    """Proposal dates lie between today and four weeks past the plan week; text dates exist."""
    inp = case["input"]
    today = date.fromisoformat(inp["today"])
    limit = date.fromisoformat(inp["plan_week"][1]) + timedelta(days=28)
    blob = json.dumps(inp, ensure_ascii=False)
    bad: list[str] = []
    for f in out.proposals.follow_ups:
        if f.due is not None and not today <= f.due <= limit:
            bad.append(f"follow-up due {f.due}")
    for d in out.proposals.promise_dues:
        if not today <= d.due <= limit:
            bad.append(f"promise due {d.due}")
    for text in review_texts(out):
        bad += [f"text date {m}" for m in _ISO.findall(text) if m not in blob]
    return "; ".join(bad) or None


_QUOTE = re.compile(r"[„“\"”]([^„“\"”]{6,}?)[“”\"]")


def rev_quotes_grounded(case: Case, out: Any) -> str | None:
    """Quoted text must be a substring of the input (no invented quotes)."""
    blob = _input_blob(case)
    bad = [
        q for text in review_texts(out) for q in _QUOTE.findall(text) if norm(q.strip()) not in blob
    ]
    return f"quotes not in the input: {bad}" if bad else None


def rev_conflicts_cited(case: Case, out: Any) -> str | None:
    exp = case["expected"]
    if exp.get("no_conflicts") and out.conflicts:
        return f"expected no conflicts, got {[c.title for c in out.conflicts]}"
    missing = [
        group
        for group in exp.get("conflicts_must_cite_meetings", [])
        if not any(set(group) <= set(c.meeting_ids) for c in out.conflicts)
    ]
    return f"no conflict cites meetings {missing}" if missing else None


def rev_slips_cited(case: Case, out: Any) -> str | None:
    wanted = set(case["expected"].get("slips_must_cite_people", []))
    cited = {p for s in out.slips for p in s.person_ids}
    missing = sorted(wanted - cited)
    return f"slips do not mention people {missing}" if missing else None


def rev_forbidden_proposals_absent(case: Case, out: Any) -> str | None:
    props = out.proposals
    hit = [kind for kind in case["expected"].get("forbid_proposals", []) if getattr(props, kind)]
    return f"forbidden proposal kinds present: {hit}" if hit else None


def rev_proposal_hygiene(case: Case, out: Any) -> str | None:
    inp = case["input"]
    props = out.proposals
    problems: list[str] = []
    total = (
        len(props.follow_ups)
        + len(props.promise_dues)
        + len(props.goal_links)
        + (1 if props.goal_rerank else 0)
    )
    if total > 10:
        problems.append(f"{total} proposals (limit about 10)")
    if len(out.priorities) > 7:
        problems.append(f"{len(out.priorities)} priorities (limit 7)")
    if props.goal_rerank:
        active = review_pools(inp).goals
        ids = props.goal_rerank.goal_ids
        if sorted(ids) != sorted(active) or len(set(ids)) != len(ids):
            problems.append("goal_rerank is not every active goal exactly once")
    blocked = set(case["expected"].get("forbid_follow_up_people", []))
    again = [f.person_id for f in props.follow_ups if f.person_id in blocked]
    if again:
        problems.append(f"follow-up repeats a pending or rejected person: {again}")
    for link in props.goal_links:
        if link.promise_id is None and link.person_id is None:
            problems.append("goal_link without a promise or a person")
    return "; ".join(problems) or None


def rev_forbidden_strings_absent(case: Case, out: Any) -> str | None:
    blob = norm(dump_text(out))
    hit = [s for s in case["expected"].get("must_not_contain", []) if norm(s) in blob]
    return f"forbidden text appears: {hit}" if hit else None


_SENTENCE_END = re.compile(r"[.!?…]+(?:\s|$)")


def rev_summary_shape(case: Case, out: Any) -> str | None:
    text = out.summary.strip()
    sentences = len([s for s in _SENTENCE_END.split(text) if s.strip()])
    if not text:
        return "empty summary"
    if not 1 <= sentences <= 6:
        return f"summary has {sentences} sentences (wanted three or four, tolerance 1-6)"
    if len(text) > 700:
        return f"summary is {len(text)} characters long"
    return None


GRADERS: dict[str, list[tuple[str, CheckFn]]] = {
    "capture": [
        ("refs_grounded", cap_refs_grounded),
        ("debts_match", cap_debts_match),
        ("settles_match", cap_settles_match),
        ("promises_match", cap_promises_match),
        ("promise_updates_match", cap_promise_updates_match),
        ("appointments_match", cap_appointments_match),
        ("school_deadlines_match", cap_school_deadlines_match),
        ("amounts_integer", cap_amounts_integer),
        ("amounts_grounded", cap_amounts_grounded),
        ("no_invented_people", cap_no_invented_people),
        ("no_invented_dates", cap_no_invented_dates),
        ("forbidden_absent", cap_forbidden_absent),
    ],
    "classify": [
        ("refs_exact", cls_refs_exact),
        ("labels_match", cls_labels_match),
        ("forbidden_labels_absent", cls_forbidden_labels_absent),
        ("confidence_in_range", cls_confidence_in_range),
        ("forbidden_strings_absent", cls_forbidden_strings_absent),
    ],
    "review": [
        ("ids_grounded", rev_ids_grounded),
        ("numbers_grounded", rev_numbers_grounded),
        ("dates_grounded", rev_dates_grounded),
        ("quotes_grounded", rev_quotes_grounded),
        ("conflicts_cited", rev_conflicts_cited),
        ("slips_cited", rev_slips_cited),
        ("forbidden_proposals_absent", rev_forbidden_proposals_absent),
        ("proposal_hygiene", rev_proposal_hygiene),
        ("forbidden_strings_absent", rev_forbidden_strings_absent),
        ("summary_shape", rev_summary_shape),
    ],
}


def grader_names(suite: str) -> list[str]:
    return ["schema_valid", *(name for name, _ in GRADERS[suite])]


def grader_count() -> int:
    return sum(len(grader_names(s)) for s in GRADERS)


def grade(case: Case, response: Any) -> list[Grade]:
    """Grade one response (a dict of the output JSON, or the Pydantic model itself)."""
    suite = case["suite"]
    model_cls = OUTPUT_MODELS[suite]
    payload = response.model_dump(mode="json") if isinstance(response, BaseModel) else response
    try:
        out = model_cls.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        why = f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
        grades = [Grade("schema_valid", False, why)]
        grades += [Grade(name, False, "schema invalid") for name, _ in GRADERS[suite]]
        return grades
    grades = [Grade("schema_valid", True)]
    for name, check in GRADERS[suite]:
        reason = check(case, out)
        grades.append(Grade(name, reason is None, reason or ""))
    return grades


def case_passed(grades: list[Grade]) -> bool:
    return all(g.passed for g in grades)
