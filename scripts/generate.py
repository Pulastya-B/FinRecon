#!/usr/bin/env python3
"""CLI for the three-way ledger generator.

Usage:
    python scripts/generate.py --seed 42 --orders 1000 --out data/seed42
    python scripts/generate.py --seed 99 --orders 1000 --out data/seed99

Seed discipline
---------------
Seed 42 is the development set. Seeds 7/13/21 are validation, run
periodically during development to catch overfitting. Seed 99 is HELD OUT:
generate it on day one, then do not run the engine against it until the
numbers are being frozen for reporting. Its value comes entirely from never
having been looked at.
"""
import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finrecon import LedgerGenerator  # noqa: E402
from finrecon.money import format_inr  # noqa: E402



def _drop_settlement_rows(out: Path, fraction: float, seed: int) -> list[str]:
    """Delete a fraction of gateway_settlements.csv, recording what was removed.

    Applied AFTER generation and after ground truth is written, so the oracle
    still knows the full truth while the ENGINE's copy of the report is
    incomplete. That asymmetry is the whole point: it is what gives membership
    inference something it genuinely must infer rather than look up.

    Uses its own RNG seeded separately from the generator's, so turning this
    flag on does not shift the generator's draw sequence and change the data
    underneath it -- the dataset stays byte-identical apart from the rows
    removed.
    """
    if fraction <= 0:
        return []

    settlements_path = out / "gateway_settlements.csv"
    rows = settlements_path.read_text(encoding="utf-8").splitlines()
    header, body = rows[0], rows[1:]

    rng = random.Random(seed ^ 0x5E77_1E)
    n_drop = int(round(len(body) * fraction))
    drop_idx = set(rng.sample(range(len(body)), min(n_drop, len(body))))
    kept = [line for i, line in enumerate(body) if i not in drop_idx]
    dropped_ids = [body[i].split(",", 1)[0] for i in sorted(drop_idx)]

    settlements_path.write_text(
        "\n".join([header] + kept) + "\n", encoding="utf-8", newline="\n"
    )

    # Record in ground truth, which eval/ reads and the matcher never does.
    gt_path = out / "ground_truth.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    gt["dropped_settlement_rows"] = dropped_ids
    gt_path.write_text(
        json.dumps(gt, indent=2), encoding="utf-8", newline="\n"
    )
    return dropped_ids


def main() -> None:
    # The summary below prints format_inr(), which contains U+20B9. On Windows
    # stdout defaults to cp1252, which cannot encode it -- so piping this
    # script's output (as tests/test_invariants.py does, via subprocess) killed
    # it with UnicodeEncodeError AFTER the data had been written correctly.
    # A generator whose exit status depends on the console codepage is not one
    # a test can call.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--orders", type=int, default=1000)
    ap.add_argument("--start", type=str, default="2026-07-01")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--out", type=str, required=True)
    ap.add_argument("--rates", type=str, default="config/rates.yaml")
    ap.add_argument("--defects", type=str, default="config/defects.yaml")
    ap.add_argument("--drop-settlement-rows", type=float, default=0.0,
                    help="fraction of gateway_settlements.csv rows to drop "
                         "AFTER generation (default 0.0)")
    args = ap.parse_args()

    y, m, d = (int(x) for x in args.start.split("-"))
    gen = LedgerGenerator(
        seed=args.seed,
        n_orders=args.orders,
        start_date=date(y, m, d),
        days=args.days,
        rates_path=args.rates,
        defects_path=args.defects,
    ).generate()

    out = gen.write(args.out)
    dropped = _drop_settlement_rows(out, args.drop_settlement_rows, args.seed)
    gt = gen._ground_truth()
    meta = gt["meta"]

    print(f"seed {args.seed} -> {out}")
    print(f"  orders      {meta['n_orders']:>6}")
    print(f"  payments    {meta['n_payments']:>6}")
    print(f"  refunds     {meta['n_refunds']:>6}")
    print(f"  chargebacks {meta['n_chargebacks']:>6}")
    print(f"  settlements {meta['n_settlements']:>6}")
    print(f"  bank rows   {meta['n_bank_rows']:>6}")
    print(f"  gross       {format_inr(meta['gross_paise'])}")
    if dropped:
        print(f"  settlement rows dropped {len(dropped):>4}"
              f"  ({args.drop_settlement_rows:.0%} of the report)")
    print("  expected outcomes:")
    for k, v in gt["expected_outcome_summary"].items():
        print(f"    {k:<32} {v:>5}")


if __name__ == "__main__":
    main()
