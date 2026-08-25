#!/usr/bin/env python3
"""
Where does subset-sum membership inference stop being trustworthy?

Part B's protection against false positives has until now rested on the search
hitting its iteration cap -- protection by accidental intractability rather than
by judgement. A cap that happens to be too small for a pool is not a reason to
trust the answers it does return, and it evaporates the moment someone narrows
a date window and makes the pool searchable.

So this measures the real boundary. Take a batch whose membership is known,
hide it, hand Part B a candidate pool of a given size, and ask three questions:

    how many subsets of that pool produce the batch's payout?
    is the TRUE subset among them?
    would the engine accept the answer, or decline it as ambiguous?

The failure mode being measured is not "the search misses the truth". It is
"the search finds the truth AND three other combinations that fit equally well",
because at that point a unique-answer rule stops being able to tell them apart
and any acceptance is a coin flip.

This script reads ground truth and therefore lives in eval/. The ceiling it
produces is a constant in finrecon/tier3_attribution.py, cited by number.

Run:
    python eval/subset_reliability.py --data data/seed42
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finrecon.normalize import load                     # noqa: E402
from finrecon import tier3_attribution as attribution   # noqa: E402

# Pool sizes to probe. The large ones are expected to be uncomputable -- that
# is a result, not a gap: a size the search cannot complete is a size the
# engine must refuse rather than attempt.
POOL_SIZES = (5, 10, 20, 40, 80)

# Generous, so the cap does not do the declining for us. The point is to see
# where AMBIGUITY arrives, which is a property of the data; the cap is a
# property of the budget and would confound the measurement.
MEASUREMENT_CAP = 40_000_000

# Payments per true set. Small and fixed, so the curve measures decoy
# pressure rather than batch size.
TRUE_SET_SIZE = 4

# Accepted answers a pool size must produce before its reliability is
# believed. Guards against reading '100%' off a sample of one.
MIN_ACCEPTED_FOR_CONFIDENCE = 5


@dataclass
class Point:
    pool_size: int
    batches: int = 0
    computable: int = 0
    truth_found: int = 0
    unique: int = 0          # exactly one subset -> engine would ACCEPT
    ambiguous: int = 0       # more than one    -> engine would DECLINE
    cap_hits: int = 0
    accepted_and_correct: int = 0
    accepted_and_wrong: int = 0
    total_subsets: int = 0

    @property
    def reliability(self) -> float | None:
        """Of the answers the engine would ACCEPT, how many are right?

        The only number that matters. An accepted wrong answer is a silent
        false attribution; a declined one costs a human two minutes.
        """
        accepted = self.accepted_and_correct + self.accepted_and_wrong
        return self.accepted_and_correct / accepted if accepted else None


def measure(data_dir: str, sizes=POOL_SIZES, n_batches: int = 8, seed: int = 42):
    ledgers = load(data_dir)
    rates = yaml.safe_load((ROOT / "config/rates.yaml").read_text())
    withholding = rates.get("withholding", {}) or {}
    wh_bps = int(withholding.get("rate_bps", 0)) if withholding.get("enabled") else 0

    by_settlement: dict[str, list] = {}
    for payment in ledgers.payments:
        sid = payment.references["settlement_id"]
        if sid:
            by_settlement.setdefault(sid, []).append(payment)

    def tup(p):
        return (
            p.entry_id,
            p.amount_paise,
            p.amounts["fee_paise"],
            p.amounts["tax_on_fee_paise"],
        )

    rng = random.Random(seed)
    # The true set is a small sample drawn from one real cycle, not the whole
    # cycle. Seed 42's batches run to ~26 payments, so using them whole would
    # make every pool below 26 unprobeable and collapse the curve to a single
    # point. Holding the true set at TRUE_SET_SIZE isolates the variable that
    # actually matters here -- how many DECOYS the search has to survive.
    chosen = []
    for sid, members in sorted(by_settlement.items()):
        if len(members) < TRUE_SET_SIZE:
            continue
        chosen.append((sid, rng.sample(members, TRUE_SET_SIZE)))
        if len(chosen) >= n_batches:
            break

    points = [Point(pool_size=n) for n in sizes]
    all_payments = [tup(p) for p in ledgers.payments]

    for point in points:
        for sid, members in chosen:
            true_set = [tup(p) for p in members]
            if len(true_set) > point.pool_size:
                continue
            target, _gross = attribution.subset_net(true_set, wh_bps)

            # Fill the rest of the pool with decoys drawn from real payments,
            # which is what an incomplete report actually leaves behind.
            true_ids = {t[0] for t in true_set}
            decoys = [t for t in all_payments if t[0] not in true_ids]
            rng.shuffle(decoys)
            pool = true_set + decoys[: point.pool_size - len(true_set)]
            rng.shuffle(pool)

            point.batches += 1
            result = attribution.infer_membership(
                f"probe_{sid}", target, pool, wh_bps,
                iteration_cap=MEASUREMENT_CAP, max_solutions_kept=64,
                # Deliberately lifted: the production refusal band is what
                # this curve exists to check, so measuring through it would
                # make the script confirm its own answer and report zero
                # subsets above the limit rather than the ambiguity that
                # justifies the limit.
                ignore_evidence_band=True,
            )
            if result.outcome == attribution.OUTCOME_UNKNOWN_CAP:
                point.cap_hits += 1
                continue

            point.computable += 1
            point.total_subsets += result.subsets_found
            found_truth = any(
                set(s.payment_ids) == true_ids for s in result.solutions
            )
            point.truth_found += bool(found_truth)

            if result.subsets_found == 1:
                point.unique += 1
                if found_truth:
                    point.accepted_and_correct += 1
                else:
                    point.accepted_and_wrong += 1
            elif result.subsets_found > 1:
                point.ambiguous += 1

    return points


def format_curve(points) -> str:
    out = ["", "subset membership reliability", "-" * 78]
    out.append(f"  {'pool':>5} {'tried':>6} {'computable':>11} {'cap':>5} "
               f"{'truth in set':>13} {'unique':>7} {'ambiguous':>10} {'reliability':>12}")
    out.append("-" * 78)
    for p in points:
        rel = "     n/a" if p.reliability is None else f"{p.reliability * 100:7.1f}%"
        out.append(
            f"  {p.pool_size:>5} {p.batches:>6} {p.computable:>11} {p.cap_hits:>5} "
            f"{p.truth_found:>13} {p.unique:>7} {p.ambiguous:>10} {rel:>12}"
        )
    out.append("-" * 78)
    out.append("  reliability = accepted answers that were RIGHT / accepted answers.")
    out.append("  'ambiguous' is the engine declining, which is a success, not a miss.")
    return "\n".join(out)


def recommend_ceiling(points) -> tuple[int, str]:
    """Largest pool size at which every accepted answer was still correct.

    Deliberately the last CLEAN size, not the first size that shows a failure:
    accepting at a size where reliability has already started to slip means
    shipping a known false-attribution rate, and a wrong attribution is silent.
    """
    ceiling = 0
    reason = "no size produced enough acceptable answers to judge"
    for p in points:
        if p.computable == 0 or p.reliability is None:
            continue
        accepted = p.accepted_and_correct + p.accepted_and_wrong
        # A size where the engine accepted once and happened to be right is
        # not evidence that the size is safe -- it is a sample of one. At
        # pool 40 on seed 42 exactly one answer in twenty was accepted, so
        # "100% reliable" there rests on a single trial. Require a real
        # sample before a size can raise the ceiling.
        if accepted < MIN_ACCEPTED_FOR_CONFIDENCE:
            break
        if p.reliability == 1.0:
            ceiling = p.pool_size
            reason = (
                f"{p.accepted_and_correct}/{accepted} accepted answers correct "
                f"at pool {p.pool_size}, from {p.batches} trials"
            )
        else:
            break
    return ceiling, reason


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure subset-search reliability.")
    ap.add_argument("--data", default="data/seed42")
    ap.add_argument("--batches", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    points = measure(args.data, n_batches=args.batches)
    ceiling, reason = recommend_ceiling(points)

    if args.json:
        print(json.dumps(
            {"points": [p.__dict__ | {"reliability": p.reliability} for p in points],
             "recommended_ceiling": ceiling, "reason": reason}, indent=2))
        return 0

    print(format_curve(points))
    print(f"\n  RECOMMENDED POOL CEILING: {ceiling}")
    print(f"  because {reason}")
    print("  above this, Part B must return UNKNOWN rather than search.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
