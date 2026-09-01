#!/usr/bin/env python3
"""
Breadth sweep: run the whole engine across many unseen seeds and report.

Seed 42 is one draw. Every threshold, band and tolerance in this project was
chosen while looking at it, and three times already a branch that never
executed on 42 turned out to be broken or absent -- carry-forward (caught only
on seed 7), TIMING_PENDING, and the L4 pair search. A rule that has only ever
seen one dataset is a rule with an unmeasured failure rate.

So this generates thirty fresh datasets, runs the full pipeline against each,
and reports. It fixes nothing. The output is a list of findings, and the two
that matter most are the ones that would otherwise be invisible:

    DEAD PATHS  code that never ran on any of thirty datasets. It is not
                working -- it is untested, and this project has been bitten by
                exactly that three times.
    VARIANCE    a metric that holds on seed 42 and swings across seeds is a
                metric that got lucky, whatever its headline value.

Datasets are written to a temp directory and deleted after scoring, EXCEPT
where a seed crashed or precision fell below 100%. Those are kept, and their
path is reported, because a failure you cannot reproduce is a rumour. Thirty
datasets is ~30MB and does not belong in the repository.

Seeds 42, 7, 13 and 21 are excluded because they are already used for
development and calibration. Seed 99 is excluded because it is held out, and
appears nowhere in this file.

Run:
    python eval/sweep.py                  # seeds 200-229
    python eval/sweep.py --seeds 200 214  # a shorter range
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finrecon import LedgerGenerator                        # noqa: E402
from finrecon.evidence import CIRCUMSTANTIAL, REFUSE, STRONG  # noqa: E402
from finrecon.normalize import load                         # noqa: E402
from finrecon.pipeline import reconcile                     # noqa: E402
from finrecon import tier3_settlement                       # noqa: E402
from eval.score import CHAIN_OUTCOMES, score_against_ground_truth  # noqa: E402

# Same shape as data/seed42, so a difference between seeds is the draw and not
# the parameters.
N_ORDERS = 1000
START_DATE = date(2026, 7, 1)
DAYS = 45

# Development and calibration seeds. Re-running them here would measure how
# well the engine fits data it was tuned on, which is the opposite of the point.
EXCLUDED_SEEDS = frozenset({7, 13, 21, 42, 99})

# The published range. Only a sweep over exactly this range may write the
# canonical results file, because that file is the committed evidence.
DEFAULT_SEEDS = [200, 229]

ALL_LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
ALL_BANDS = (STRONG, CIRCUMSTANTIAL, REFUSE)

# Two settlement nets this close, on the same day, are a coin flip for any
# amount-based rule. Wide enough to catch near-misses the tolerance band would
# not, narrow enough that a hit is genuinely a collision risk.
COLLISION_PAISE = 100


@dataclass
class SeedResult:
    seed: int
    crashed: bool = False
    traceback_text: str = ""
    kept_path: str = ""

    coverage: float = 0.0
    precision: float | None = None
    recall: float = 0.0
    exception_accuracy: float | None = None
    attribution_accuracy: float | None = None

    levels: dict = field(default_factory=dict)
    bands: dict = field(default_factory=dict)
    exception_codes: dict = field(default_factory=dict)

    ambiguity_firings: int = 0
    refuse_count: int = 0

    shortfalls: int = 0
    settlements: int = 0
    seconds: float = 0.0
    validate_passed: bool = False
    validate_detail: str = ""

    # Structural edge cases -- counts, so "did it happen" and "how often" are
    # both answerable.
    same_day_near_collision: int = 0
    carry_forward_out_batches: int = 0
    empty_settlements: int = 0
    credit_matching_two_nets: int = 0


def _structural_edges(data_dir: Path, rates: dict) -> dict[str, int]:
    """Count the shapes that seed 42 may simply never produce.

    Reads the emitted CSVs only. These are properties of the DATA, not of the
    engine, and knowing whether a seed contains them is what makes "precision
    held" meaningful -- holding on data that never presents the hard case is
    not evidence of anything.
    """
    ledgers = load(data_dir)
    batches = tier3_settlement.build_batches(ledgers, rates)
    expected = {b.settlement_id: tier3_settlement.compute_expected_net(b)
                for b in batches}

    by_day: dict[date, list[int]] = {}
    for settlement in ledgers.settlements:
        by_day.setdefault(settlement.event_date, []).append(
            settlement.amounts["net_paise"]
        )
    collisions = 0
    for nets in by_day.values():
        nets = sorted(nets)
        for i in range(len(nets) - 1):
            if nets[i + 1] - nets[i] <= COLLISION_PAISE:
                collisions += 1

    carry_out = sum(
        1 for s in ledgers.settlements
        if s.amounts["carry_forward_out_paise"] > 0
    )

    with_payments = {
        p.references["settlement_id"] for p in ledgers.payments
        if p.references["settlement_id"]
    }
    empty = sum(1 for s in ledgers.settlements if s.entry_id not in with_payments)

    ambiguous_credits = 0
    for row in ledgers.bank:
        if row.entry_type != "bank_credit":
            continue
        credit = row.amounts["credit_paise"]
        hits = sum(1 for net in expected.values() if abs(net - credit) <= 2)
        if hits >= 2:
            ambiguous_credits += 1

    return {
        "same_day_near_collision": collisions,
        "carry_forward_out_batches": carry_out,
        "empty_settlements": empty,
        "credit_matching_two_nets": ambiguous_credits,
    }


def run_seed(seed: int, workdir: Path, rates: dict) -> SeedResult:
    """Generate, reconcile, score and validate one seed. Never raises."""
    result = SeedResult(seed=seed)
    data_dir = workdir / f"seed{seed}"
    started = time.time()
    try:
        LedgerGenerator(
            seed=seed, n_orders=N_ORDERS, start_date=START_DATE, days=DAYS,
            rates_path=str(ROOT / "config/rates.yaml"),
            defects_path=str(ROOT / "config/defects.yaml"),
        ).generate().write(data_dir)

        # No LLM anywhere: reconcile() does not import explain, so the sweep
        # cannot make a network call even by accident.
        engine = reconcile(data_dir)
        report = score_against_ground_truth(
            engine, data_dir / "ground_truth.json"
        )

        result.coverage = report.coverage
        result.precision = report.precision
        result.recall = report.recall
        result.exception_accuracy = report.exception_accuracy
        result.attribution_accuracy = report.attribution_accuracy

        attributions = engine.get("attributions", [])
        result.levels = dict(Counter(a["level"] for a in attributions))
        result.bands = dict(Counter(
            (a.get("evidence") or {}).get("strength")
            for a in attributions
            if (a.get("evidence") or {}).get("strength")
        ))
        result.refuse_count = result.bands.get(REFUSE, 0)
        result.ambiguity_firings = sum(
            1 for a in attributions
            if a["outcome"] == "AMBIGUOUS_MULTI_CANDIDATE"
        )
        result.exception_codes = dict(Counter(
            d["outcome"] for d in engine["decisions"] if d["outcome"] != "MATCHED"
        ))

        gt = json.loads((data_dir / "ground_truth.json").read_text(encoding="utf-8"))
        result.shortfalls = len(gt.get("settlement_shortfalls", []))
        result.settlements = gt["meta"]["n_settlements"]

        for key, value in _structural_edges(data_dir, rates).items():
            setattr(result, key, value)

        validate = subprocess.run(
            [sys.executable, str(ROOT / "eval/validate.py"), "--data", str(data_dir)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        passed = [
            line for line in validate.stdout.splitlines() if "checks passed" in line
        ]
        result.validate_detail = passed[0].strip() if passed else "no output"
        result.validate_passed = validate.returncode == 0 and "125/125" in result.validate_detail

    except Exception:
        result.crashed = True
        result.traceback_text = traceback.format_exc()

    result.seconds = time.time() - started

    # Keep the dataset ONLY when it failed. A failure that cannot be reproduced
    # is a rumour; a passing dataset is 1MB of noise.
    failed = result.crashed or (result.precision is not None and result.precision < 1.0)
    if failed:
        result.kept_path = str(data_dir)
    elif data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    return result


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def _pct(value: float | None) -> str:
    return "    n/a" if value is None else f"{value * 100:6.2f}%"


def _stats(values: list[float]) -> str:
    if not values:
        return "no data"
    lo, hi = min(values), max(values)
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values) if len(values) > 1 else 0.0
    return (f"min {lo * 100:6.2f}%  max {hi * 100:6.2f}%  "
            f"mean {mean * 100:6.2f}%  sd {sd * 100:5.2f}")


def format_report(results: list[SeedResult]) -> str:
    out: list[str] = []
    ok = [r for r in results if not r.crashed]

    out.append("")
    out.append(f"{'seed':>5}{'cov':>9}{'prec':>9}{'recall':>9}{'exc acc':>9}"
               f"{'L0':>4}{'L1':>4}{'L2':>4}{'L3':>4}{'L4':>4}{'L5':>4}"
               f"{'amb':>5}{'ref':>5}{'val':>6}{'secs':>7}")
    out.append("-" * 96)
    for r in results:
        if r.crashed:
            out.append(f"{r.seed:>5}   *** CRASHED ***")
            continue
        lv = r.levels
        out.append(
            f"{r.seed:>5}{_pct(r.coverage):>9}{_pct(r.precision):>9}"
            f"{_pct(r.recall):>9}{_pct(r.exception_accuracy):>9}"
            + "".join(f"{lv.get(k, 0):>4}" for k in ALL_LEVELS)
            + f"{r.ambiguity_firings:>5}{r.refuse_count:>5}"
            + f"{'OK' if r.validate_passed else 'FAIL':>6}{r.seconds:>7.1f}"
        )
    out.append("-" * 96)

    # -- 1. crashes --------------------------------------------------------
    crashes = [r for r in results if r.crashed]
    out.append(f"\n1. CRASHES  ({len(crashes)} of {len(results)})")
    if not crashes:
        out.append("   none")
    for r in crashes:
        out.append(f"   seed {r.seed}  kept at {r.kept_path}")
        out.append("   " + r.traceback_text.strip().replace("\n", "\n   "))

    # -- 2. precision ------------------------------------------------------
    below = [r for r in ok if r.precision is None or r.precision < 1.0]
    out.append(f"\n2. PRECISION below 100.00%  ({len(below)} of {len(ok)})")
    if not below:
        out.append("   none -- precision held at 100.00% on every seed")
    for r in below:
        out.append(f"   seed {r.seed}: {_pct(r.precision)}  kept at {r.kept_path}")

    # -- 3. recall ---------------------------------------------------------
    low_recall = [r for r in ok if r.recall < 1.0]
    out.append(f"\n3. RECALL below 100%  ({len(low_recall)} of {len(ok)})")
    if not low_recall:
        out.append("   none")
    for r in low_recall:
        out.append(f"   seed {r.seed}: {_pct(r.recall)}"
                   f"   (a matchable chain left undecided -- a rule is too tight)")

    # -- 4. variance -------------------------------------------------------
    out.append(f"\n4. VARIANCE across {len(ok)} seeds")
    for name, values in [
        ("coverage", [r.coverage for r in ok]),
        ("precision", [r.precision for r in ok if r.precision is not None]),
        ("recall", [r.recall for r in ok]),
        ("exception accuracy",
         [r.exception_accuracy for r in ok if r.exception_accuracy is not None]),
        ("attribution accuracy",
         [r.attribution_accuracy for r in ok if r.attribution_accuracy is not None]),
    ]:
        out.append(f"   {name:<22}{_stats(values)}")

    # -- 5. dead paths -----------------------------------------------------
    out.append(f"\n5. DEAD PATHS -- never fired on any of {len(ok)} seeds")
    fired_levels = Counter()
    fired_bands = Counter()
    fired_codes = Counter()
    for r in ok:
        fired_levels.update({k: v for k, v in r.levels.items() if v})
        fired_bands.update({k: v for k, v in r.bands.items() if v})
        fired_codes.update({k: v for k, v in r.exception_codes.items() if v})

    dead_levels = [lv for lv in ALL_LEVELS if not fired_levels.get(lv)]
    dead_bands = [b for b in ALL_BANDS if not fired_bands.get(b)]
    dead_codes = [c for c in CHAIN_OUTCOMES
                  if c != "MATCHED" and not fired_codes.get(c)]
    out.append(f"   attribution levels : {dead_levels or 'none -- all fired'}")
    out.append(f"   evidence bands     : {dead_bands or 'none -- all fired'}")
    out.append(f"   exception codes    : {dead_codes or 'none -- all fired'}")

    # -- 6. rare paths -----------------------------------------------------
    out.append(f"\n6. RARE PATHS -- fired on fewer than 3 of {len(ok)} seeds")
    def seeds_with(attr_name: str, key: str) -> int:
        return sum(1 for r in ok if getattr(r, attr_name).get(key))
    rare = []
    for level in ALL_LEVELS:
        n = seeds_with("levels", level)
        if 0 < n < 3:
            rare.append(f"   level {level:<4} on {n} seed(s)")
    for band in ALL_BANDS:
        n = seeds_with("bands", band)
        if 0 < n < 3:
            rare.append(f"   band  {band:<15} on {n} seed(s)")
    for code in CHAIN_OUTCOMES:
        if code == "MATCHED":
            continue
        n = seeds_with("exception_codes", code)
        if 0 < n < 3:
            rare.append(f"   code  {code:<28} on {n} seed(s)")
    out.extend(rare or ["   none"])

    # -- 7. structural edge cases ------------------------------------------
    out.append(f"\n7. STRUCTURAL EDGE CASES -- seeds producing them, of {len(ok)}")
    for label, attr, note in [
        ("same-day settlements within 100p", "same_day_near_collision",
         "weekend collapse; amount-based rules cannot separate these"),
        ("carry_forward_out > 0", "carry_forward_out_batches",
         "the branch that was wrong for two seeds before anyone noticed"),
        ("settlement with zero payments", "empty_settlements", ""),
        ("credit matching two settlement nets", "credit_matching_two_nets",
         "the case the contested-row rule exists for"),
    ]:
        seeds_hit = [r for r in ok if getattr(r, attr) > 0]
        total = sum(getattr(r, attr) for r in ok)
        precision_held = all(
            r.precision == 1.0 for r in seeds_hit if r.precision is not None
        )
        verdict = ("precision held on all of them" if seeds_hit and precision_held
                   else "PRECISION MOVED" if seeds_hit else "never occurred")
        out.append(f"   {label:<38} {len(seeds_hit):>3} seeds, {total:>4} cases"
                   f"   -- {verdict}")
        if note and not seeds_hit:
            out.append(f"       ({note})")

    # -- validator ---------------------------------------------------------
    bad_validate = [r for r in ok if not r.validate_passed]
    out.append(f"\n   validator: {len(ok) - len(bad_validate)}/{len(ok)} seeds at 125/125")
    for r in bad_validate:
        out.append(f"     seed {r.seed}: {r.validate_detail}")

    return "\n".join(out)


def write_csv(results: list[SeedResult], path: Path) -> None:
    """Raw results only. The datasets themselves are not committed."""
    rows = []
    for r in results:
        row = asdict(r)
        row.pop("traceback_text", None)
        for key in ("levels", "bands", "exception_codes"):
            row[key] = json.dumps(row[key], sort_keys=True)
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Breadth sweep across unseen seeds.")
    ap.add_argument("--seeds", type=int, nargs=2, default=list(DEFAULT_SEEDS),
                    metavar=("FIRST", "LAST"))
    # Default deliberately None rather than the canonical path. A partial run
    # used to overwrite eval/sweep_results.csv with however few seeds it ran --
    # and that file is the committed thirty-seed evidence the Evidence page
    # reads. Running `--seeds 205 205` to check one row silently replaced all
    # thirty with one. It happened. A subset now writes beside it instead.
    ap.add_argument("--out", default=None,
                    help="results CSV (default: the canonical file for a full "
                         "sweep, sweep_results_FIRST-LAST.csv for a subset)")
    ap.add_argument("--keep-dir", default=None,
                    help="where failing datasets are kept (default: a temp dir)")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    seeds = [s for s in range(args.seeds[0], args.seeds[1] + 1)
             if s not in EXCLUDED_SEEDS]
    skipped = sorted(set(range(args.seeds[0], args.seeds[1] + 1)) & EXCLUDED_SEEDS)
    if skipped:
        print(f"skipping reserved seeds: {skipped}")

    import yaml
    rates = yaml.safe_load((ROOT / "config/rates.yaml").read_text())

    workdir = Path(args.keep_dir) if args.keep_dir else Path(tempfile.mkdtemp(
        prefix="finrecon_sweep_"))
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"working directory: {workdir}")
    print(f"running {len(seeds)} seeds: {seeds[0]}-{seeds[-1]}\n")

    results = []
    for seed in seeds:
        row = run_seed(seed, workdir, rates)
        results.append(row)
        flag = "CRASH" if row.crashed else (
            "PREC!" if (row.precision or 0) < 1.0 else "ok")
        print(f"  seed {seed}  {flag:<6} {row.seconds:5.1f}s")

    print(format_report(results))
    if args.out:
        out_path = Path(args.out)
    elif [args.seeds[0], args.seeds[1]] == DEFAULT_SEEDS:
        out_path = ROOT / "eval/sweep_results.csv"
    else:
        out_path = ROOT / f"eval/sweep_results_{args.seeds[0]}-{args.seeds[1]}.csv"
    write_csv(results, out_path)
    print(f"\nraw results -> {out_path}")

    kept = [r for r in results if r.kept_path]
    if kept:
        print(f"\nFAILING DATASETS KEPT for reproduction:")
        for r in kept:
            print(f"  seed {r.seed}: {r.kept_path}")
    else:
        # Nothing failed, so nothing was worth 30MB on disk.
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"\nall seeds passed; working directory removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
