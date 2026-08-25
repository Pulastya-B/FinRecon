#!/usr/bin/env python3
"""
Defect-rate sensitivity sweep.

The dataset's weakest point is the obvious objection: "your synthetic data is
cleaner than production." That objection is unanswerable if the claim being
defended is "my rates are realistic" -- nobody in the room knows the true
rates, and the argument is unwinnable.

So don't defend the rates. Measure sensitivity TO them. Sweep each defect
across a range, run the engine at each point, and report the degradation
curve. The claim then becomes falsifiable and modest:

    "I don't know your production defect rate. Here is my precision as a
     function of it. Pick a rate and read off the number."

What to look for in the output: coverage should fall as defects rise (more
items become genuinely unresolvable, which is correct), while precision
should stay high (the engine routes hard cases to the exception queue rather
than guessing). Precision collapsing alongside coverage is a bug, not a
property -- it means the matcher is guessing under pressure.

Usage:
    python eval/sensitivity.py --defect duplicate_capture --seeds 42 7 13
    python eval/sensitivity.py --all --out eval/sensitivity_results.csv
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from finrecon import LedgerGenerator  # noqa: E402

# Sweep grid per defect. Centred on the configured default, spanning roughly
# an order of magnitude either side where that is meaningful.
SWEEPS: dict[str, list[float]] = {
    "duplicate_capture": [0.005, 0.015, 0.030, 0.060, 0.120],
    "narration_truncated": [0.05, 0.15, 0.30, 0.55, 0.85],
    "settlement_missing_in_bank": [0.005, 0.010, 0.020, 0.050, 0.100],
    "direct_bank_credit": [0.005, 0.010, 0.020, 0.050, 0.100],
    "partial_refund": [0.02, 0.05, 0.08, 0.15, 0.30],
    "chargeback": [0.002, 0.006, 0.012, 0.030, 0.060],
    "order_never_paid": [0.01, 0.02, 0.04, 0.08, 0.16],
}


def run_point(defect: str, rate: float, seed: int, n_orders: int) -> dict:
    """Generate one dataset at one defect rate and summarise its difficulty.

    ---------------------------------------------------------------------
    MATCHER HOOK
    ---------------------------------------------------------------------
    Once the reconciliation engine exists, import it and score here:

        from finrecon.pipeline import reconcile
        from eval.score import score_against_ground_truth

        result = reconcile(tmpdir)
        metrics = score_against_ground_truth(result, tmpdir / "ground_truth.json")
        row.update(coverage=metrics.coverage, precision=metrics.precision, ...)

    Until then this reports ground-truth composition, which already shows how
    the problem's shape shifts with the rate -- and confirms the sweep plumbing
    works before there is anything to plug into it.
    ---------------------------------------------------------------------
    """
    with tempfile.TemporaryDirectory() as tmp:
        gen = LedgerGenerator(
            seed=seed,
            n_orders=n_orders,
            start_date=date(2026, 7, 1),
            days=45,
            rates_path=str(Path(__file__).resolve().parents[1] / "config/rates.yaml"),
            defects_path=str(Path(__file__).resolve().parents[1] / "config/defects.yaml"),
            defect_overrides={defect: rate},
        ).generate()
        gen.write(tmp)
        gt = gen._ground_truth()

    outcomes = gt["expected_outcome_summary"]
    total = sum(outcomes.values())
    resolvable = outcomes.get("MATCHED", 0)

    return {
        "defect": defect,
        "rate": rate,
        "seed": seed,
        "n_chains": total,
        "trivially_matchable": resolvable,
        "trivially_matchable_pct": round(100 * resolvable / total, 2) if total else 0.0,
        "exceptions_expected": total - resolvable,
        "n_payments": gt["meta"]["n_payments"],
        "n_settlements": gt["meta"]["n_settlements"],
        "n_bank_rows": gt["meta"]["n_bank_rows"],
        # coverage / precision / false_positive_rate land here once the
        # matcher is wired in above.
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--defect", type=str, help="single defect to sweep")
    ap.add_argument("--all", action="store_true", help="sweep every defect")
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 13])
    ap.add_argument("--orders", type=int, default=1000)
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()

    if args.all:
        targets = list(SWEEPS)
    elif args.defect:
        if args.defect not in SWEEPS:
            raise SystemExit(f"no sweep grid for '{args.defect}'; known: {list(SWEEPS)}")
        targets = [args.defect]
    else:
        raise SystemExit("pass --defect NAME or --all")

    rows = [
        run_point(defect, rate, seed, args.orders)
        for defect in targets
        for rate in SWEEPS[defect]
        for seed in args.seeds
    ]
    df = pd.DataFrame(rows)

    # Average across seeds: a single seed's curve is noisy, and the noise is
    # itself worth reporting rather than hiding.
    summary = (
        df.groupby(["defect", "rate"])
        .agg(
            matchable_pct_mean=("trivially_matchable_pct", "mean"),
            matchable_pct_std=("trivially_matchable_pct", "std"),
            exceptions_mean=("exceptions_expected", "mean"),
        )
        .round(2)
        .reset_index()
    )

    for defect in targets:
        print(f"\n{defect}")
        sub = summary[summary["defect"] == defect]
        print(f"  {'rate':>8} {'matchable%':>12} {'±sd':>7} {'exceptions':>12}")
        for _, r in sub.iterrows():
            sd = 0.0 if pd.isna(r['matchable_pct_std']) else r['matchable_pct_std']
            print(
                f"  {r['rate']:>8.3f} {r['matchable_pct_mean']:>12.2f} "
                f"{sd:>7.2f} {r['exceptions_mean']:>12.1f}"
            )

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.out, index=False)
        print(f"\nraw results -> {args.out}")


if __name__ == "__main__":
    main()
