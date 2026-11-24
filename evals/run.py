"""Run the eval suites.

    uv run python -m evals.run --mode recorded                  # CI: replay the cassettes, no key
    uv run python -m evals.run --mode fake                      # the offline FakeClaude
    uv run python -m evals.run --mode live --k 3 --max-eur 2    # real model; needs a key

Modes
- recorded: every case carries a hand-written `recorded` answer. This checks the graders, the case
  files and the pipeline. It does NOT measure a model.
- fake: the keyword-rule FakeClaude used for demos. Deterministic; shows how far rules get.
- live: the real ClaudeClient, k trials per case. The only mode that measures a model.

pass rate = share of (case, trial) pairs where every grader passed.
pass^k    = share of cases where all k trials passed (live only; in the other modes the replay is
            deterministic, so pass^k equals the pass rate and is marked trivial).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.graders import Grade, case_passed, grade, grader_names
from evals.loader import (
    ROOT,
    SUITES,
    Case,
    capture_request,
    classify_rows,
    load_cases,
    review_request,
)

Responder = Callable[[Case], Any]

DEFAULT_MAX_EUR = 2.0
# Conservative EUR per million tokens when no price is given; set EVALS_EUR_PER_MTOK_IN / _OUT.
DEFAULT_PRICE_IN = 15.0
DEFAULT_PRICE_OUT = 75.0


class BudgetExceeded(Exception):
    """The live run reached its cost cap; remaining trials are not run."""


# --- responders ---


def recorded_responder(case: Case) -> Any:
    return case["recorded"]


def fake_responder(case: Case) -> Any:
    from orbit.integrations.fake_claude import FakeClaude

    fake = FakeClaude(owner_name="Alex")
    suite = case["suite"]
    if suite == "capture":
        return fake.extract_capture(capture_request(case["input"]))
    if suite == "classify":
        rows, shared_min = classify_rows(case["input"])
        return fake.classify_transactions(rows, shared_min)
    return fake.weekly_review(review_request(case["input"])).output


@dataclass
class CostMeter:
    max_eur: float
    price_in: float = DEFAULT_PRICE_IN
    price_out: float = DEFAULT_PRICE_OUT
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def eur(self) -> float:
        return (self.input_tokens * self.price_in + self.output_tokens * self.price_out) / 1e6

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

    def check(self) -> None:
        if self.eur >= self.max_eur:
            raise BudgetExceeded(f"cost cap of {self.max_eur:.2f} EUR reached ({self.eur:.2f})")


def live_responder(meter: CostMeter, client: Any | None = None) -> Responder:
    """A responder backed by the real ClaudeClient; `client` is injectable for tests."""
    if client is None:
        from orbit.integrations.claude import ClaudeClient

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        model = os.environ.get("ANTHROPIC_MODEL", "")
        if not api_key or not model:
            raise RuntimeError("live mode needs ANTHROPIC_API_KEY and ANTHROPIC_MODEL")

        def on_usage(usage: Any) -> None:
            meter.add(usage.input_tokens, usage.output_tokens)

        deep_effort = os.environ.get("ANTHROPIC_DEEP_EFFORT") or None
        client = ClaudeClient(
            api_key,
            model,
            on_usage=on_usage,
            fast_model=os.environ.get("ANTHROPIC_MODEL_FAST") or None,
            deep_model=os.environ.get("ANTHROPIC_MODEL_DEEP") or None,
            deep_effort=None if deep_effort in (None, "off") else deep_effort,
            owner_name="Alex",
        )

    def respond(case: Case) -> Any:
        meter.check()
        suite = case["suite"]
        if suite == "capture":
            return client.extract_capture(capture_request(case["input"]))
        if suite == "classify":
            rows, shared_min = classify_rows(case["input"])
            return client.classify_transactions(rows, shared_min)
        return client.weekly_review(review_request(case["input"])).output

    return respond


# --- running and reporting ---


@dataclass
class CaseResult:
    case_id: str
    trials: list[list[Grade]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def passes(self) -> list[bool]:
        return [case_passed(t) for t in self.trials]


def run_case(case: Case, responder: Responder, k: int) -> CaseResult:
    result = CaseResult(case["id"])
    for _ in range(k):
        try:
            response = responder(case)
        except BudgetExceeded:
            raise
        except Exception as exc:  # a failed call is a failed trial, not a crash
            result.errors.append(f"{type(exc).__name__}: {exc}"[:200])
            result.trials.append([Grade("call_succeeded", False, str(exc)[:200])])
            continue
        result.trials.append(grade(case, response))
    return result


def run_suite(
    suite: str, responder: Responder, k: int = 1, cases: list[Case] | None = None
) -> dict[str, Any]:
    cases = cases if cases is not None else load_cases(suite)
    results: list[CaseResult] = []
    truncated = False
    try:
        for case in cases:
            results.append(run_case(case, responder, k))
    except BudgetExceeded:
        truncated = True
    trials = [t for r in results for t in r.trials]
    passed_trials = sum(case_passed(t) for t in trials)
    per_grader: dict[str, dict[str, int]] = {
        n: {"passed": 0, "total": 0} for n in grader_names(suite)
    }
    for t in trials:
        for g in t:
            slot = per_grader.setdefault(g.name, {"passed": 0, "total": 0})
            slot["total"] += 1
            slot["passed"] += int(g.passed)
    all_k = sum(1 for r in results if r.passes and all(r.passes))
    failed = [
        {
            "case": r.case_id,
            "failed_graders": sorted({g.name for t in r.trials for g in t if not g.passed}),
        }
        for r in results
        if not all(r.passes)
    ]
    n_cases = len(results)
    return {
        "cases": n_cases,
        "cases_planned": len(cases),
        "trials": len(trials),
        "passed_trials": passed_trials,
        "pass_rate": round(passed_trials / len(trials), 4) if trials else 0.0,
        "pass_hat_k": round(all_k / n_cases, 4) if n_cases else 0.0,
        "k": k,
        "truncated_by_budget": truncated,
        "graders": per_grader,
        "failed_cases": failed,
    }


def run_all(suites: Sequence[str], mode: str, responder: Responder, k: int) -> dict[str, Any]:
    trials = k if mode == "live" else 1
    report: dict[str, Any] = {
        "mode": mode,
        "k": trials,
        "pass_hat_k_note": (
            "k independent live trials per case"
            if mode == "live"
            else "deterministic replay: pass^k equals the pass rate (trivial, not reliability)"
        ),
        "suites": {s: run_suite(s, responder, trials) for s in suites},
    }
    return report


def render_markdown(report: dict[str, Any], extra: dict[str, Any] | None = None) -> str:
    lines = [
        f"# Orbit eval report ({report['mode']})",
        "",
        f"k = {report['k']}. {report['pass_hat_k_note']}.",
        "",
        "| suite | cases | trials | pass rate | pass^k |",
        "|---|---|---|---|---|",
    ]
    for name, s in report["suites"].items():
        lines.append(
            f"| {name} | {s['cases']} | {s['trials']} | "
            f"{s['pass_rate']:.1%} | {s['pass_hat_k']:.1%} |"
        )
    for name, s in report["suites"].items():
        lines += ["", f"## {name}: graders", "", "| grader | passed | total |", "|---|---|---|"]
        for gname, g in s["graders"].items():
            lines.append(f"| {gname} | {g['passed']} | {g['total']} |")
        if s["failed_cases"]:
            lines += ["", "Failed cases:"]
            lines += [f"- {f['case']}: {', '.join(f['failed_graders'])}" for f in s["failed_cases"]]
        if s["truncated_by_budget"]:
            lines += ["", "**Stopped early: the cost cap was reached.**"]
    for key, value in (extra or {}).items():
        lines += ["", f"## {key}", "", "```json", json.dumps(value, indent=2), "```"]
    return "\n".join(lines) + "\n"


def parse_suites(value: str) -> list[str]:
    names = list(SUITES) if value == "all" else [v.strip() for v in value.split(",") if v.strip()]
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown suite {unknown}; choose from {list(SUITES)}")
    return names


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--mode", choices=["recorded", "fake", "live"], required=True)
    parser.add_argument(
        "--suite", type=parse_suites, default="all", help="capture,classify,review or all"
    )
    parser.add_argument("--k", type=int, default=3, help="live trials per case (default 3)")
    parser.add_argument("--json", metavar="PATH", help="write the JSON report here")
    parser.add_argument("--max-eur", type=float, default=DEFAULT_MAX_EUR, help="live cost cap")
    parser.add_argument("--min-pass-rate", type=float, default=None, help="exit 1 below this")
    parser.add_argument("--judge", action="store_true", help="live only: run the LLM summary judge")
    parser.add_argument("--no-report-files", action="store_true", help="do not write evals/results")
    args = parser.parse_args(argv)
    suites: list[str] = args.suite if isinstance(args.suite, list) else parse_suites(args.suite)

    extra: dict[str, Any] = {}
    summaries: list[tuple[Case, str]] = []
    meter: CostMeter | None = None
    if args.mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("live mode needs ANTHROPIC_API_KEY (not set). Nothing was run.", file=sys.stderr)
            return 2
        meter = CostMeter(
            args.max_eur,
            float(os.environ.get("EVALS_EUR_PER_MTOK_IN", DEFAULT_PRICE_IN)),
            float(os.environ.get("EVALS_EUR_PER_MTOK_OUT", DEFAULT_PRICE_OUT)),
        )
        responder = live_responder(meter)
        if args.judge:
            inner = responder

            def collecting(case: Case) -> Any:
                response = inner(case)
                if case["suite"] == "review":
                    summaries.append((case, response.summary))
                return response

            responder = collecting
    elif args.mode == "fake":
        responder = fake_responder
    else:
        responder = recorded_responder

    report = run_all(suites, args.mode, responder, args.k)
    if meter is not None:
        report["cost"] = {
            "calls": meter.calls,
            "input_tokens": meter.input_tokens,
            "output_tokens": meter.output_tokens,
            "estimated_eur": round(meter.eur, 4),
            "cap_eur": meter.max_eur,
            "note": "estimate from token counts and EVALS_EUR_PER_MTOK_IN/OUT",
        }
        if args.judge and "review" in suites:
            extra["judge"] = run_live_judge(summaries)
    text = render_markdown(report, extra)
    print(text)

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if not args.no_report_files and not args.json:
        out_dir = ROOT / "results"
        out_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (out_dir / f"{args.mode}-{stamp}.md").write_text(text, encoding="utf-8")
        (out_dir / f"{args.mode}-{stamp}.json").write_text(
            json.dumps({**report, **extra}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    if args.min_pass_rate is not None:
        low = [s for s, v in report["suites"].items() if v["pass_rate"] < args.min_pass_rate]
        if low:
            print(f"pass rate below {args.min_pass_rate:.2f} in: {', '.join(low)}", file=sys.stderr)
            return 1
    return 0


def facts_for(case: Case) -> list[str]:
    """Short facts for the judge: what the snapshots and signals contained."""
    inp = case["input"]
    facts: list[str] = []
    for lines in json.loads(inp["signals_json"]).values():
        facts += lines["lines"] if isinstance(lines, dict) else lines
    nxt = json.loads(inp["next_week_json"])
    facts += [f"goal {g['id']} (rank {g['rank']}): {g['title']}" for g in nxt["goals"]["goals"]]
    facts += [
        f"promise {p['id']}: {p['text']}, due {p['due']}" for p in nxt["promises"]["promises"]
    ]
    facts += [f"meeting {m['id']}: {m['title']} {m['start']}" for m in nxt["meetings"]["meetings"]]
    facts += [
        f"debt {d['id']}: {d['direction']} {d['amount']} {d['currency']}"
        for d in nxt["ledger"]["debts"]
    ]
    return facts


def run_live_judge(summaries: list[tuple[Case, str]]) -> dict[str, Any]:
    """Calibrate the judge on the labelled samples first, then grade the live summaries."""
    from evals.calibrate import agreement, load_labels
    from evals.judge import LiveJudge

    judge = LiveJudge()
    result = agreement(load_labels(), judge)
    out: dict[str, Any] = {
        "agreement_with_labels": result.accuracy,
        "kappa": result.kappa,
        "trusted": result.trusted,
    }
    if result.trusted and summaries:
        good = sum(judge({"facts": facts_for(c), "summary": s}) for c, s in summaries)
        out["summaries_judged_good"] = f"{good}/{len(summaries)}"
    else:
        out["summaries_judged_good"] = (
            "not run: judge below the agreement threshold or no summaries"
        )
    out["tokens"] = {"input": judge.input_tokens, "output": judge.output_tokens}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
