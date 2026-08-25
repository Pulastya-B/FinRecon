#!/usr/bin/env python3
"""
Are the evidence labels honest?

finrecon/evidence.py estimates how often a fit should happen by chance and puts
each attribution in a band. That estimate rests on an assumption that is
plainly false in detail -- that candidate amounts are spread evenly over their
range -- so the bands are a claim, not a fact, until somebody checks them
against outcomes.

Ground truth records the real cause of every injected shortfall. This compares
the band the engine assigned against whether it named the right item.

What the result means:

    STRONG near 100%          the label means what it says
    STRONG materially below   the 0.01 threshold is too loose. The fix is a
                              tighter threshold, not a quieter report -- a
                              confidence label that is wrong is worse than no
                              label, because it is acted on.
    CIRCUMSTANTIAL lower      expected and correct. That band exists to say
                              "verify before acting", and if it scored as well
                              as STRONG the split would be measuring nothing.

Run across several seeds, because one dataset yields a handful of shortfalls
and a band's accuracy cannot be read off three cases.

Run:
    python eval/evidence_calibration.py --seeds 42 7 13 21
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finrecon.evidence import BAND_MEANING, STRONG_MAX      # noqa: E402
from finrecon.pipeline import reconcile                     # noqa: E402

# The level STRONG has to clear to be worth the word. Below this the label is
# telling an operator to act on something that is wrong more than one time in
# fifty, which is not what "essentially ruled out" means.
STRONG_ACCURACY_FLOOR = 0.98

# Seed 99 is held out and is absent by construction, not by oversight.
DEFAULT_SEEDS = (42, 7, 13, 21)


@dataclass
class Band:
    name: str
    attributions: int = 0
    correct: int = 0
    wrong: int = 0
    examples_wrong: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.attributions if self.attributions else None


@dataclass
class SeedResult:
    seed: int
    shortfalls: int = 0
    attributions: int = 0
    refused: int = 0


def calibrate(seeds=DEFAULT_SEEDS):
    bands: dict[str, Band] = {}
    per_seed: list[SeedResult] = []

    for seed in seeds:
        data_dir = ROOT / f"data/seed{seed}"
        if not data_dir.is_dir():
            per_seed.append(SeedResult(seed=seed))
            continue

        result = reconcile(data_dir)
        # Ground truth is read HERE and nowhere the engine can reach.
        gt = json.loads((data_dir / "ground_truth.json").read_text(encoding="utf-8"))
        truth = {x["settlement_id"]: x for x in gt.get("settlement_shortfalls", [])}

        seed_row = SeedResult(seed=seed, shortfalls=len(truth))
        for attribution in result.get("attributions", []):
            evidence = attribution.get("evidence") or {}
            band_name = evidence.get("strength")
            if not band_name:
                continue
            if band_name == "REFUSE":
                seed_row.refused += 1

            item = attribution.get("item_id")
            if not item:
                # A band with no proposal is not an attribution to score.
                continue

            seed_row.attributions += 1
            band = bands.setdefault(band_name, Band(name=band_name))
            band.attributions += 1
            # Sets, not scalars: a compound shortfall has two causing items
            # and naming the right PAIR is the claim being checked.
            row = truth.get(attribution["settlement_id"], {})
            expected = set(row.get("item_ids") or
                           ([row["item_id"]] if row.get("item_id") else []))
            proposed = set(item.split("+")) if "+" in item else {item}
            if expected and proposed == expected:
                band.correct += 1
            else:
                band.wrong += 1
                if len(band.examples_wrong) < 5:
                    band.examples_wrong.append(
                        f"seed{seed} {attribution['settlement_id']} "
                        f"named {sorted(proposed)}, truth {sorted(expected)}"
                    )
        per_seed.append(seed_row)

    return bands, per_seed


def format_report(bands, per_seed) -> str:
    out = ["", "evidence calibration -- are the band labels honest?", "-" * 72]
    out.append(f"  {'band':<16}{'attributions':>13}{'correct':>9}{'wrong':>7}"
               f"{'accuracy':>11}   meaning")
    out.append("-" * 72)
    for name in ("STRONG", "CIRCUMSTANTIAL", "REFUSE"):
        band = bands.get(name)
        if band is None:
            out.append(f"  {name:<16}{0:>13}{'-':>9}{'-':>7}{'n/a':>11}   "
                       f"{BAND_MEANING[name]}")
            continue
        acc = "n/a" if band.accuracy is None else f"{band.accuracy * 100:.1f}%"
        out.append(f"  {name:<16}{band.attributions:>13}{band.correct:>9}"
                   f"{band.wrong:>7}{acc:>11}   {BAND_MEANING[name]}")
    out.append("-" * 72)

    out.append("\n  per seed")
    for row in per_seed:
        if row.shortfalls == 0 and row.attributions == 0:
            out.append(f"    seed{row.seed:<4} dataset absent -- not generated")
            continue
        out.append(f"    seed{row.seed:<4} shortfalls {row.shortfalls:>3}   "
                   f"attributions {row.attributions:>3}   refused {row.refused:>3}")

    for name, band in bands.items():
        for example in band.examples_wrong:
            out.append(f"\n  {name} miss: {example}")

    strong = bands.get("STRONG")
    out.append("")
    if strong and strong.attributions and strong.accuracy is not None:
        if strong.accuracy >= STRONG_ACCURACY_FLOOR:
            out.append(f"  VERDICT: STRONG is honest at "
                       f"{strong.accuracy * 100:.1f}% "
                       f"(floor {STRONG_ACCURACY_FLOOR * 100:.0f}%). "
                       f"Threshold {STRONG_MAX} holds.")
        else:
            # Say the threshold is wrong. Do not quietly move the data.
            out.append(f"  VERDICT: STRONG is only {strong.accuracy * 100:.1f}%, "
                       f"below the {STRONG_ACCURACY_FLOOR * 100:.0f}% floor. "
                       f"The {STRONG_MAX} threshold is TOO LOOSE.")
            out.append(f"           Recommend tightening it by the observed "
                       f"error rate: try {STRONG_MAX / 10}.")
    else:
        out.append("  VERDICT: no STRONG attributions to judge. "
                   "Generate more seeds or raise the defect rates.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the evidence bands honestly.")
    ap.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if 99 in args.seeds:
        raise SystemExit("seed 99 is held out; calibration must not read it")

    bands, per_seed = calibrate(args.seeds)
    if args.json:
        print(json.dumps(
            {name: {"n": b.attributions, "correct": b.correct,
                    "accuracy": b.accuracy} for name, b in bands.items()},
            indent=2))
        return 0
    print(format_report(bands, per_seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
