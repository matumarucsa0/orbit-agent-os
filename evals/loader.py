"""Load eval cases and turn their JSON input into the real request dataclasses."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from orbit.integrations.claude import (
    CandidateCourse,
    CandidateDebt,
    CandidateGoal,
    CandidatePerson,
    CandidatePromise,
    CaptureExtraction,
    CaptureRequest,
    ReviewMemory,
    ReviewOutput,
    ReviewRequest,
    TransactionClassification,
    TransactionRow,
)

ROOT = Path(__file__).resolve().parent
SUITES = ("capture", "classify", "review")
OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "capture": CaptureExtraction,
    "classify": TransactionClassification,
    "review": ReviewOutput,
}
OWNER = "Alex"  # the fictional persona

Case = dict[str, Any]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{path.name}:{number}: not valid JSON ({exc})") from exc
    return rows


def load_cases(suite: str) -> list[Case]:
    return read_jsonl(ROOT / "cases" / f"{suite}.jsonl")


def load_negatives() -> list[dict[str, Any]]:
    return read_jsonl(ROOT / "cassettes" / "negatives.jsonl")


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def capture_request(inp: dict[str, Any]) -> CaptureRequest:
    people = tuple(
        CandidatePerson(
            id=p["id"],
            name=p["name"],
            aliases=tuple(p.get("aliases", ())),
            debts=tuple(
                CandidateDebt(
                    id=d["id"],
                    direction=d["direction"],
                    kind=d["kind"],
                    what=d["what"],
                    description=d.get("description"),
                    reference_code=d.get("reference_code"),
                    since=date.fromisoformat(d["since"]),
                    due=_date(d.get("due")),
                )
                for d in p.get("debts", ())
            ),
            promises=tuple(
                CandidatePromise(
                    id=pr["id"],
                    direction=pr["direction"],
                    text=pr["text"],
                    due=_date(pr.get("due")),
                )
                for pr in p.get("promises", ())
            ),
        )
        for p in inp["candidates"]
    )
    return CaptureRequest(
        text=inp["text"],
        today=date.fromisoformat(inp["today"]),
        candidates=people,
        default_currency=inp["default_currency"],
        context=inp.get("context"),
        goals=tuple(
            CandidateGoal(id=g["id"], rank=g["rank"], title=g["title"], why=g.get("why"))
            for g in inp.get("goals", ())
        ),
        corrections=tuple(inp.get("corrections", ())),
        rejected=tuple(inp.get("rejected", ())),
        courses=tuple(CandidateCourse(c["code"], c["name"]) for c in inp.get("courses", ())),
        owner=OWNER,
    )


def classify_rows(inp: dict[str, Any]) -> tuple[list[TransactionRow], str]:
    rows = [
        TransactionRow(
            ref=r["ref"],
            when=r["when"],
            direction=r["direction"],
            amount=r["amount"],
            kind=r["kind"],
            description=r["description"],
            meeting=r.get("meeting"),
        )
        for r in inp["rows"]
    ]
    return rows, inp["shared_min"]


def review_request(inp: dict[str, Any]) -> ReviewRequest:
    prev = inp.get("previous")
    review_week = inp["review_week"]
    plan_week = inp["plan_week"]
    return ReviewRequest(
        today=date.fromisoformat(inp["today"]),
        review_week=(date.fromisoformat(review_week[0]), date.fromisoformat(review_week[1])),
        plan_week=(date.fromisoformat(plan_week[0]), date.fromisoformat(plan_week[1])),
        last_week_json=inp["last_week_json"],
        next_week_json=inp["next_week_json"],
        previous=(
            ReviewMemory(prev["week"], prev["summary"], tuple(prev["priorities"])) if prev else None
        ),
        pending=tuple(inp.get("pending", ())),
        rejected=tuple(inp.get("rejected", ())),
        signals_json=inp.get("signals_json", ""),
        owner=OWNER,
    )


def prompt_text(case: Case) -> str:
    """Everything the model is shown as data for this case (used for grounding checks)."""
    inp = case["input"]
    if case["suite"] == "capture":
        return json.dumps(inp, ensure_ascii=False)
    if case["suite"] == "classify":
        return json.dumps(inp["rows"], ensure_ascii=False)
    return json.dumps(inp, ensure_ascii=False)
