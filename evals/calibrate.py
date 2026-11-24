"""Agreement of a review-summary judge with the labelled samples.

    uv run python -m evals.calibrate                 # offline heuristic baseline (not an LLM)
    uv run python -m evals.calibrate --judge live    # the real judge; needs ANTHROPIC_API_KEY

The agreement functions take any `Judge`, so the arithmetic is tested without a model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from evals.judge import Judge, LiveJudge, Sample, heuristic_judge
from evals.loader import ROOT, read_jsonl

LABELS = ROOT / "calibration" / "review_judge_labels.jsonl"
MIN_AGREEMENT = 0.85  # below this the judge is not trusted for anything
MIN_KAPPA = 0.6


@dataclass(frozen=True)
class Agreement:
    n: int
    agree: int
    accuracy: float
    kappa: float
    true_good: int  # judge good, label good
    false_good: int  # judge good, label bad
    false_bad: int  # judge bad, label good
    true_bad: int  # judge bad, label bad

    @property
    def trusted(self) -> bool:
        return self.accuracy >= MIN_AGREEMENT and self.kappa >= MIN_KAPPA


def load_labels() -> list[Sample]:
    return read_jsonl(LABELS)


def agreement(samples: Sequence[Sample], judge: Judge) -> Agreement:
    tg = fg = fb = tb = 0
    for s in samples:
        label_good = s["label"] == "good"
        says_good = judge(s)
        if says_good and label_good:
            tg += 1
        elif says_good:
            fg += 1
        elif label_good:
            fb += 1
        else:
            tb += 1
    n = tg + fg + fb + tb
    observed = (tg + tb) / n if n else 0.0
    p_good = ((tg + fg) / n) * ((tg + fb) / n) if n else 0.0
    p_bad = ((fb + tb) / n) * ((fg + tb) / n) if n else 0.0
    chance = p_good + p_bad
    kappa = (observed - chance) / (1 - chance) if chance < 1 else 1.0
    return Agreement(n, tg + tb, round(observed, 3), round(kappa, 3), tg, fg, fb, tb)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.calibrate", description=__doc__)
    parser.add_argument("--judge", choices=["heuristic", "live"], default="heuristic")
    parser.add_argument("--json", help="write the result here")
    args = parser.parse_args(argv)
    samples = load_labels()
    if args.judge == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set; the live judge cannot run.", file=sys.stderr)
            return 2
        judge: Judge = LiveJudge()
    else:
        judge = heuristic_judge
    result = agreement(samples, judge)
    print(f"judge: {args.judge}, samples: {result.n}")
    print(f"agreement: {result.agree}/{result.n} = {result.accuracy:.2f}, kappa {result.kappa:.2f}")
    print(
        f"judge good/label good {result.true_good}, judge good/label bad {result.false_good}, "
        f"judge bad/label good {result.false_bad}, judge bad/label bad {result.true_bad}"
    )
    verdict = "yes" if result.trusted else "no"
    print(f"trusted (accuracy >= {MIN_AGREEMENT:.2f} and kappa >= {MIN_KAPPA:.2f}): {verdict}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {"judge": args.judge, **asdict(result), "trusted": result.trusted}, fh, indent=2
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
