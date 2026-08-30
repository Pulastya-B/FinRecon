#!/usr/bin/env python3
"""
The held-out run. Seed 99, once.

Seed 99 was generated on day one and left alone. Its value comes entirely from
not having been looked at, and it is spent the moment it is run twice -- run it,
see a disappointing number, fix the cause, and it has become a development set
whose score means nothing.

So this script exists to be run once, at the end, against a frozen engine, and
to write down what it found whatever that was. It records the result to
cache/evidence/holdout.json so the number in the UI and the number in this
transcript cannot drift apart.

The comparison that matters is seed 42 beside seed 99. The GAP between a
development set and a held-out one is the overfitting measurement; publishing it
rather than quietly reporting the better one is the whole point of having kept
seed 99 sealed.

Run (once):
    python eval/run_holdout.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finrecon.pipeline import reconcile                 # noqa: E402
from eval.score import score_against_ground_truth       # noqa: E402

OUT = ROOT / "cache" / "evidence" / "holdout.json"


def measure(seed: str) -> dict:
    data_dir = ROOT / "data" / seed
    started = time.perf_counter()
    result = reconcile(data_dir)
    elapsed = time.perf_counter() - started
    rep = score_against_ground_truth(result, data_dir / "ground_truth.json")
    return {
        "seed": seed,
        "coverage": rep.coverage,
        "precision": rep.precision,
        "recall": rep.recall,
        "exception_accuracy": rep.exception_accuracy,
        "attribution_accuracy": rep.attribution_accuracy,
        "matches_made": rep.matches_made,
        "correct_matches": rep.correct_matches,
        "wrong_matches": rep.wrong_matches,
        "exceptions_raised": rep.exceptions_raised,
        "total_chains": rep.total_chains,
        "input_rows": result["input_rows"],
        "elapsed_seconds": round(elapsed, 2),
    }


def pct(v):
    return "n/a" if v is None else f"{v * 100:.2f}%"


def main() -> int:
    print("=" * 70)
    print("HELD-OUT RUN — seed 99")
    print("=" * 70)
    print("Engine frozen. This runs once and the result is published as-is.")
    print()

    dev = measure("seed42")
    held = measure("seed99")

    # The 30-seed sweep, for context. Seed 99 is not one of them.
    import csv
    rows = list(csv.DictReader(
        (ROOT / "eval/sweep_results.csv").read_text(encoding="utf-8").splitlines()))

    def sweep(col):
        vals = [float(r[col]) for r in rows if r[col] not in ("", "None")]
        return {"min": min(vals), "max": max(vals),
                "mean": statistics.fmean(vals), "sd": statistics.pstdev(vals)}

    print(f"{'metric':<24}{'seed 42 (dev)':>16}{'seed 99 (held out)':>22}{'gap':>12}")
    print("-" * 74)
    for key, label in (("coverage", "coverage"),
                       ("precision", "PRECISION"),
                       ("recall", "recall"),
                       ("exception_accuracy", "exception accuracy"),
                       ("attribution_accuracy", "attribution accuracy")):
        d, h = dev[key], held[key]
        gap = "" if (d is None or h is None) else f"{(h - d) * 100:+.2f}"
        print(f"{label:<24}{pct(d):>16}{pct(h):>22}{gap:>12}")

    print()
    print(f"{'rows in':<24}{dev['input_rows']:>16,}{held['input_rows']:>22,}")
    print(f"{'wall clock':<24}{dev['elapsed_seconds']:>15}s{held['elapsed_seconds']:>21}s")
    print(f"{'matches claimed':<24}{dev['matches_made']:>16,}{held['matches_made']:>22,}")
    print(f"{'WRONG matches':<24}{dev['wrong_matches']:>16,}{held['wrong_matches']:>22,}")
    print(f"{'exception rows':<24}{dev['exceptions_raised']:>16,}{held['exceptions_raised']:>22,}")

    print()
    print("against the 30-seed sweep (200-229), which seed 99 is not part of:")
    for col, label in (("coverage", "coverage"), ("precision", "precision"),
                       ("attribution_accuracy", "attribution accuracy")):
        s = sweep(col)
        inside = (held[col] is not None and s["min"] <= held[col] <= s["max"])
        print(f"  {label:<22} sweep {pct(s['min'])}-{pct(s['max'])} "
              f"(sd {s['sd'] * 100:.2f})   seed 99 {pct(held[col])}"
              f"   {'within range' if inside else 'OUTSIDE RANGE'}")

    payload = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Seed 99, run once against a frozen engine. Not re-run, not "
                "tuned against, published as measured.",
        "held_out": held,
        "development": dev,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"written to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
